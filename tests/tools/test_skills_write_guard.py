"""Tests for tools/skills_write_guard.py — the un-ledgered curator write guard.

Issue #96962: the curator re-homed a skill's ``references/`` into an umbrella
with terminal ``mv`` and then archived the stripped original. The archive
ledger entry faithfully captured what remained (``files: 1``) and
``hermes curator rollback`` restored a hollow skill. Terminal writes under a
skills root are invisible to the ledger, so they must not happen from the
headless curator at all.
"""

from pathlib import Path

import pytest

from tools.skills_write_guard import detect_skills_tree_mutation, guard_active


@pytest.fixture
def skills_root(tmp_path):
    root = tmp_path / "skills"
    (root / "narrow-skill" / "references").mkdir(parents=True)
    (root / "narrow-skill" / "SKILL.md").write_text("# narrow\n", encoding="utf-8")
    (root / "narrow-skill" / "references" / "extra.md").write_text(
        "detail\n", encoding="utf-8"
    )
    (root / "umbrella").mkdir()
    return root.resolve()


def _detect(command, cwd, root):
    return detect_skills_tree_mutation(command, str(cwd), roots=[root])


class TestBlocksUnLedgeredSkillWrites:
    @pytest.mark.parametrize(
        "command",
        [
            # The exact shape from the incident report.
            "mkdir -p {root}/umbrella/references && "
            "mv {root}/narrow-skill/references/extra.md "
            "{root}/umbrella/references/extra.md",
            "mv {root}/narrow-skill {root}/.archive/narrow-skill",
            "rm -rf {root}/narrow-skill",
            "rm {root}/narrow-skill/references/extra.md",
            "cp /tmp/notes.md {root}/umbrella/references/notes.md",
            "rsync -a /tmp/pack/ {root}/umbrella/",
            "touch {root}/umbrella/references/new.md",
            "mkdir -p {root}/umbrella/scripts",
            "sed -i s/old/new/ {root}/umbrella/SKILL.md",
            "tee {root}/umbrella/SKILL.md",
            "ln -s /tmp/x {root}/umbrella/references/x.md",
            "chmod 600 {root}/umbrella/SKILL.md",
            "dd if=/dev/zero of={root}/umbrella/SKILL.md",
            "truncate -s 0 {root}/umbrella/SKILL.md",
        ],
    )
    def test_blocked(self, command, skills_root, tmp_path):
        hit, message = _detect(
            command.format(root=skills_root), tmp_path, skills_root
        )
        assert hit is True
        assert "96962" in message
        assert "write_file" in message

    def test_shell_redirect_is_blocked(self, skills_root, tmp_path):
        hit, _ = _detect(
            f"echo hi > {skills_root}/umbrella/references/x.md",
            tmp_path,
            skills_root,
        )
        assert hit is True

    def test_append_redirect_is_blocked(self, skills_root, tmp_path):
        hit, _ = _detect(
            f"cat notes >> {skills_root}/umbrella/SKILL.md", tmp_path, skills_root
        )
        assert hit is True

    def test_relative_path_after_cd_is_blocked(self, skills_root, tmp_path):
        hit, _ = _detect(
            f"cd {skills_root}/narrow-skill && rm -rf references",
            tmp_path,
            skills_root,
        )
        assert hit is True

    def test_cwd_relative_path_is_blocked(self, skills_root):
        hit, _ = _detect("rm -rf narrow-skill/references", skills_root, skills_root)
        assert hit is True

    def test_nested_shell_is_blocked(self, skills_root, tmp_path):
        hit, _ = _detect(
            f"bash -c 'mv {skills_root}/narrow-skill/SKILL.md /tmp/x.md'",
            tmp_path,
            skills_root,
        )
        assert hit is True

    def test_heredoc_payload_is_blocked(self, skills_root, tmp_path):
        command = (
            "bash <<'EOF'\n"
            f"rm -rf {skills_root}/narrow-skill\n"
            "EOF"
        )
        hit, _ = _detect(command, tmp_path, skills_root)
        assert hit is True

    def test_inline_interpreter_is_blocked(self, skills_root, tmp_path):
        hit, _ = _detect(
            f'python -c "import shutil; shutil.rmtree(\'{skills_root}/umbrella\')"',
            tmp_path,
            skills_root,
        )
        assert hit is True

    def test_mv_out_of_the_tree_is_blocked(self, skills_root, tmp_path):
        """A move that only *sources* the tree still strips the package."""
        hit, _ = _detect(
            f"mv {skills_root}/narrow-skill/references/extra.md /tmp/extra.md",
            tmp_path,
            skills_root,
        )
        assert hit is True


class TestAllowsReads:
    @pytest.mark.parametrize(
        "command",
        [
            "cat {root}/narrow-skill/SKILL.md",
            "ls -la {root}",
            "find {root} -name SKILL.md",
            "grep -r references {root}",
            "wc -l {root}/narrow-skill/SKILL.md",
            "head -50 {root}/narrow-skill/references/extra.md",
            "diff {root}/narrow-skill/SKILL.md {root}/umbrella/SKILL.md",
            # sed without -i only prints.
            "sed s/old/new/ {root}/umbrella/SKILL.md",
            # Copying content OUT of the tree writes nothing inside it.
            "cp {root}/narrow-skill/references/extra.md /tmp/extra.md",
            # Descriptor duplication is not a file write.
            "cat {root}/narrow-skill/SKILL.md 2>&1",
        ],
    )
    def test_allowed(self, command, skills_root, tmp_path):
        hit, message = _detect(
            command.format(root=skills_root), tmp_path, skills_root
        )
        assert hit is False
        assert message is None

    def test_writes_outside_the_tree_are_allowed(self, skills_root, tmp_path):
        hit, _ = _detect(
            f"mv {tmp_path}/a.md {tmp_path}/b.md", tmp_path, skills_root
        )
        assert hit is False

    def test_empty_command_is_allowed(self, skills_root, tmp_path):
        assert _detect("", tmp_path, skills_root) == (False, None)

    def test_no_roots_means_no_guard(self, tmp_path):
        assert detect_skills_tree_mutation(
            "rm -rf /anything", str(tmp_path), roots=[]
        ) == (False, None)


class TestGuardActivation:
    def test_inactive_for_foreground(self):
        from tools.skill_provenance import (
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin("foreground")
        try:
            assert guard_active() is False
        finally:
            reset_current_write_origin(token)

    def test_active_for_background_review(self):
        from tools.skill_provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert guard_active() is True
        finally:
            reset_current_write_origin(token)


class TestDefaultRoots:
    def test_local_skills_dir_is_protected_by_default(self, monkeypatch, tmp_path):
        """With no explicit roots the guard resolves the live skills dirs."""
        from tools import skills_write_guard

        root = (tmp_path / "skills").resolve()
        root.mkdir()
        monkeypatch.setattr(
            skills_write_guard, "_protected_roots", lambda: [root]
        )

        hit, _ = detect_skills_tree_mutation(
            f"rm -rf {root}/some-skill", str(tmp_path)
        )
        assert hit is True

    def test_real_roots_resolve_without_error(self):
        from tools.skills_write_guard import _protected_roots

        roots = _protected_roots()
        assert all(isinstance(entry, Path) for entry in roots)

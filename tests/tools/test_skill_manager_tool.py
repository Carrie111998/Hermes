"""Tests for tools/skill_manager_tool.py — skill creation, editing, and deletion."""

import json
import multiprocessing
import os
import re
import stat
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.skill_manager_tool as skill_manager_module
from tools.skill_manager_tool import (
    _validate_name,
    _validate_category,
    _validate_frontmatter,
    _validate_file_path,
    _create_skill as _create_skill_impl,
    _edit_skill,
    _patch_skill,
    _delete_skill,
    _write_file,
    _remove_file,
    skill_manage,
)
from agent.skill_utils import (
    extract_skill_description,
    parse_frontmatter,
    SKILL_PROMPT_DESC_LIMIT,
)


@contextmanager
def _skill_dir(tmp_path):
    """Patch both SKILLS_DIR and get_all_skills_dirs so _find_skill searches
    only the temp directory — not the real ~/.hermes/skills/."""
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        yield


VALID_SKILL_CONTENT = """\
---
name: test-skill
description: A test skill for unit testing.
---

# Test Skill

Step 1: Do the thing.
"""

VALID_SKILL_CONTENT_2 = """\
---
name: test-skill
description: Updated description.
---

# Test Skill v2

Step 1: Do the new thing.
"""


def test_windows_security_scan_gives_nt_backend_a_new_snapshot_path():
    fake_os = MagicMock()
    fake_os.name = "nt"
    observed = {}

    def fake_copy(skill_handle, destination):
        observed["skill_handle"] = skill_handle
        observed["destination_existed"] = destination.exists()
        destination.mkdir()
        (destination / "SKILL.md").write_text(
            VALID_SKILL_CONTENT,
            encoding="utf-8",
        )

    def fake_scan(destination):
        assert (destination / "SKILL.md").is_file()
        return None

    held_skill = object()
    with (
        patch.object(skill_manager_module, "os", fake_os),
        patch(
            "tools.nt_secure_fs_optional.copy_tree_no_reparse",
            side_effect=fake_copy,
        ),
        patch.object(
            skill_manager_module,
            "_security_scan_skill",
            side_effect=fake_scan,
        ),
    ):
        assert (
            skill_manager_module._security_scan_held_skill_impl(held_skill)
            is None
        )

    assert observed == {
        "skill_handle": held_skill,
        "destination_existed": False,
    }


LONG_DESC_CONTENT = """\
---
name: long-desc
description: Use when deploying multi-region Kubernetes clusters with custom CNI plugins and service mesh.
---

# Long Desc Skill

Step 1.
"""


def _content_for_name(content: str, name: str) -> str:
    """Keep generic CRUD fixtures valid under the create identity contract."""
    return re.sub(
        r"(?m)^name:\s*.*$",
        f"name: {name}",
        content,
        count=1,
    )


def _create_skill(name: str, content: str, category: str = None):
    return _create_skill_impl(name, _content_for_name(content, name), category)


def _process_create_same_skill(
    skills_dir: str,
    category: str,
    start,
    results,
) -> None:
    root = Path(skills_dir)
    content = _content_for_name(VALID_SKILL_CONTENT, "shared-process-name")
    with patch("tools.skill_manager_tool.SKILLS_DIR", root), patch(
        "agent.skill_utils.get_all_skills_dirs",
        return_value=[root],
    ):
        start.wait()
        results.put(
            _create_skill_impl(
                "shared-process-name",
                content,
                category,
            )["success"]
        )


def _process_edit_with_controlled_scan(
    skills_dir: str,
    content: str,
    should_fail: bool,
    entered_scan,
    release_scan,
    done,
    results,
) -> None:
    """Process worker used to prove rollback/commit transaction ordering."""
    root = Path(skills_dir)

    def controlled_scan(_skill_fd):
        entered_scan.set()
        if should_fail:
            release_scan.wait(timeout=10)
            return "blocked by deterministic scan"
        return None

    with patch("tools.skill_manager_tool.SKILLS_DIR", root), patch(
        "agent.skill_utils.get_all_skills_dirs",
        return_value=[root],
    ), patch(
        "tools.skill_manager_tool._security_scan_held_skill",
        side_effect=controlled_scan,
    ):
        result = _edit_skill("transaction-skill", content)
        results.put(result)
        done.set()


def _process_write_alias_with_controlled_scan(
    skills_dir: str,
    alias: str,
    content: str,
    should_fail: bool,
    entered_scan,
    release_scan,
    done,
    results,
) -> None:
    """Mutate one supporting file through either physical-skill alias."""
    root = Path(skills_dir)
    scan_snapshot = None

    def controlled_scan(skill_fd):
        nonlocal scan_snapshot
        file_fd = os.open(
            "references/state.txt", os.O_RDONLY, dir_fd=skill_fd
        )
        try:
            scan_snapshot = os.read(file_fd, 100).decode("utf-8")
        finally:
            os.close(file_fd)
        entered_scan.set()
        if should_fail:
            release_scan.wait(timeout=10)
            return "blocked by deterministic cross-process alias scan"
        return None

    with patch("tools.skill_manager_tool.SKILLS_DIR", root), patch(
        "agent.skill_utils.get_all_skills_dirs",
        return_value=[root],
    ), patch(
        "tools.skill_manager_tool._security_scan_held_skill",
        side_effect=controlled_scan,
    ):
        result = _write_file(alias, "references/state.txt", content)
        results.put((result, scan_snapshot))
        done.set()


def _process_edit_alias_with_controlled_scan(
    skills_dir: str,
    alias: str,
    content: str,
    entered_scan,
    release_scan,
    done,
    results,
) -> None:
    """Pause a canonical edit while its physical skill lock is held."""
    root = Path(skills_dir)

    def controlled_scan(_skill_fd):
        entered_scan.set()
        release_scan.wait(timeout=10)
        return None

    with patch("tools.skill_manager_tool.SKILLS_DIR", root), patch(
        "agent.skill_utils.get_all_skills_dirs",
        return_value=[root],
    ), patch(
        "tools.skill_manager_tool._security_scan_held_skill",
        side_effect=controlled_scan,
    ):
        results.put(_edit_skill(alias, content))
        done.set()


def _process_delete_alias(
    skills_dir: str,
    alias: str,
    done,
    results,
) -> None:
    """Delete a skill through one of its aliases."""
    root = Path(skills_dir)
    with patch("tools.skill_manager_tool.SKILLS_DIR", root), patch(
        "agent.skill_utils.get_all_skills_dirs",
        return_value=[root],
    ):
        results.put(_delete_skill(alias))
        done.set()


# ---------------------------------------------------------------------------
# _validate_name
# ---------------------------------------------------------------------------


class TestValidateName:
    def test_valid_names(self):
        assert _validate_name("my-skill") is None
        assert _validate_name("skill123") is None
        assert _validate_name("my_skill.v2") is None
        assert _validate_name("a") is None

    def test_special_chars_rejected(self):
        err = _validate_name("skill/name")
        assert "Invalid skill name 'skill/name'" in err
        err = _validate_name("skill name")
        assert "Invalid skill name 'skill name'" in err
        err = _validate_name("skill@name")
        assert "Invalid skill name 'skill@name'" in err


class TestValidateCategory:
    def test_path_traversal_rejected(self):
        err = _validate_category("../escape")
        assert "Invalid category '../escape'" in err

    def test_absolute_path_rejected(self):
        err = _validate_category("/tmp/escape")
        assert "Invalid category '/tmp/escape'" in err


# ---------------------------------------------------------------------------
# _validate_frontmatter
# ---------------------------------------------------------------------------


class TestValidateFrontmatter:
    def test_no_frontmatter(self):
        err = _validate_frontmatter("# Just a heading\nSome content.\n")
        assert err == "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    def test_noncanonical_frontmatter_opener_is_rejected(self):
        content = (
            "---oops: ignored\n"
            "name: demo\n"
            "description: demo routing.\n"
            "platforms: [windows]\n"
            "---\nBody.\n"
        )
        err = _validate_frontmatter(
            content, new_skill=True, expected_name="demo"
        )
        assert err == (
            "SKILL.md must start with YAML frontmatter (---). "
            "See existing skills for format."
        )

    def test_unclosed_frontmatter(self):
        content = "---\nname: test\ndescription: desc\nBody content.\n"
        assert _validate_frontmatter(content) == "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    def test_missing_name_field(self):
        content = "---\ndescription: desc\n---\n\nBody.\n"
        assert _validate_frontmatter(content) == "Frontmatter must include 'name' field."

    def test_missing_description_field(self):
        content = "---\nname: test\n---\n\nBody.\n"
        assert _validate_frontmatter(content) == "Frontmatter must include 'description' field."

    def test_no_body_after_frontmatter(self):
        content = "---\nname: test\ndescription: desc\n---\n"
        assert _validate_frontmatter(content) == "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    def test_invalid_yaml(self):
        content = "---\n: invalid: yaml: {{{\n---\n\nBody.\n"
        assert "YAML frontmatter parse error" in _validate_frontmatter(content)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("name", ""),
            ("name", "[]"),
            ("description", ""),
            ("description", "[]"),
        ],
    )
    def test_required_metadata_must_be_nonempty_strings(self, field, value):
        content = (
            "---\n"
            f"name: {'valid-name' if field != 'name' else value}\n"
            f"description: {'Valid description.' if field != 'description' else value}\n"
            "---\n\nBody.\n"
        )
        error = _validate_frontmatter(content)
        assert error is not None
        assert field in error


# ---------------------------------------------------------------------------
# _validate_file_path — path traversal prevention
# ---------------------------------------------------------------------------


class TestValidateFilePath:
    def test_valid_paths(self):
        assert _validate_file_path("references/api.md") is None
        assert _validate_file_path("templates/config.yaml") is None
        assert _validate_file_path("scripts/train.py") is None
        assert _validate_file_path("assets/image.png") is None

    def test_path_traversal_blocked(self):
        err = _validate_file_path("references/../../../etc/passwd")
        assert err == "Path traversal ('..') is not allowed."

    def test_disallowed_subdirectory(self):
        err = _validate_file_path("secret/hidden.txt")
        assert "File must be under one of:" in err
        assert "'secret/hidden.txt'" in err

    def test_directory_only_rejected(self):
        err = _validate_file_path("references")
        assert "Provide a file path, not just a directory" in err
        assert "'references/myfile.md'" in err

    def test_root_level_file_rejected(self):
        err = _validate_file_path("malicious.py")
        assert "File must be under one of:" in err
        assert "'malicious.py'" in err

    def test_skill_md_accepted_at_root(self):
        # SKILL.md is the canonical skill file and must be accepted even
        # though it does not live under an allowed subdirectory.
        assert _validate_file_path("SKILL.md") is None

    def test_skill_md_accepted_name_prefixed(self):
        assert (
            _validate_file_path("my-skill/SKILL.md", skill_name="my-skill")
            is None
        )
        assert _validate_file_path("other/SKILL.md", skill_name="my-skill")

    def test_skill_md_traversal_still_rejected(self):
        # The SKILL.md exception must not weaken the traversal guard.
        err = _validate_file_path("../SKILL.md")
        assert err == "Path traversal ('..') is not allowed."

    def test_other_root_md_still_rejected(self):
        # Only SKILL.md gets the root-level exception, not arbitrary files.
        err = _validate_file_path("README.md")
        assert "File must be under one of:" in err


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


class TestCreateSkill:
    def test_create_skill(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT)
        assert result["success"] is True
        assert (tmp_path / "my-skill" / "SKILL.md").exists()

    def test_create_with_category(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT, category="devops")
        assert result["success"] is True
        assert (tmp_path / "devops" / "my-skill" / "SKILL.md").exists()
        assert result["category"] == "devops"

    def test_create_normalizes_category_whitespace(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill(
                "my-skill", VALID_SKILL_CONTENT, category=" devops "
            )
        assert result["success"] is True
        assert result["category"] == "devops"
        assert (tmp_path / "devops" / "my-skill" / "SKILL.md").exists()
        assert not (tmp_path / " devops ").exists()

    def test_create_duplicate_blocked(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _create_skill("my-skill", VALID_SKILL_CONTENT)
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_create_rejects_frontmatter_name_mismatch(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill_impl("requested", VALID_SKILL_CONTENT)
        assert result["success"] is False
        assert "must match" in result["error"]
        assert not (tmp_path / "requested").exists()

    def test_create_invalid_name(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill("Invalid Name!", VALID_SKILL_CONTENT)
        assert result["success"] is False

    def test_create_invalid_content(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill("my-skill", "no frontmatter here")
        assert result["success"] is False

    def test_create_rejects_category_traversal(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        with patch("tools.skill_manager_tool.SKILLS_DIR", skills_dir), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir]):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT, category="../escape")

        assert result["success"] is False
        assert "Invalid category '../escape'" in result["error"]
        assert not (tmp_path / "escape").exists()

    def test_create_rejects_absolute_category(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        outside = tmp_path / "outside"

        with patch("tools.skill_manager_tool.SKILLS_DIR", skills_dir), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir]):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT, category=str(outside))

        assert result["success"] is False
        assert f"Invalid category '{outside}'" in result["error"]
        assert not (outside / "my-skill" / "SKILL.md").exists()

    def test_create_rejects_redirected_category(self, tmp_path):
        skills_dir = tmp_path / "skills"
        outside = tmp_path / "outside"
        skills_dir.mkdir()
        outside.mkdir()
        category = skills_dir / "redirect"
        try:
            category.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

        with patch("tools.skill_manager_tool.SKILLS_DIR", skills_dir), patch(
            "agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir]
        ):
            result = _create_skill(
                "my-skill", VALID_SKILL_CONTENT, category="redirect"
            )

        assert result["success"] is False
        assert "redirected category" in result["error"]
        assert not (outside / "my-skill").exists()

    def test_create_cannot_be_redirected_by_category_swap(self, tmp_path):
        skills_dir = tmp_path / "skills"
        outside = tmp_path / "outside"
        skills_dir.mkdir()
        outside.mkdir()
        (outside / "my-skill").mkdir()
        real_replace = __import__("os").replace
        swapped = False

        def swap_category_before_replace(src, dst, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                (skills_dir / "devops").rename(skills_dir / "moved-devops")
                (skills_dir / "devops").symlink_to(
                    outside, target_is_directory=True
                )
            return real_replace(src, dst, **kwargs)

        with patch("tools.skill_manager_tool.SKILLS_DIR", skills_dir), patch(
            "agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir]
        ), patch(
            "tools.skill_manager_tool.os.replace",
            side_effect=swap_category_before_replace,
        ):
            result = _create_skill(
                "my-skill", VALID_SKILL_CONTENT, category="devops"
            )

        assert result["success"] is False
        assert "path changed" in result["error"].lower()
        assert not (outside / "my-skill" / "SKILL.md").exists()

    def test_create_root_retarget_scans_held_new_skill_and_rolls_back(
        self, tmp_path
    ):
        outside_a = tmp_path / "outside-a"
        outside_b = tmp_path / "outside-b"
        root_link = tmp_path / "skills"
        outside_a.mkdir()
        victim_dir = outside_b / "devops" / "my-skill"
        victim_dir.mkdir(parents=True)
        victim_content = "B-VICTIM-MUST-REMAIN-UNCHANGED"
        (victim_dir / "SKILL.md").write_text(
            victim_content,
            encoding="utf-8",
        )
        root_link.symlink_to(outside_a, target_is_directory=True)
        scanned = False

        def inspect_held_snapshot_then_retarget(snapshot):
            nonlocal scanned
            scanned = True
            snapshot_content = (snapshot / "SKILL.md").read_text(
                encoding="utf-8"
            )
            assert "A test skill for unit testing." in snapshot_content
            assert "B-VICTIM-MUST-REMAIN-UNCHANGED" not in snapshot_content
            root_link.unlink()
            root_link.symlink_to(outside_b, target_is_directory=True)
            return None

        with _skill_dir(root_link), patch(
            "tools.skill_manager_tool._agent_created_security_scan_enabled",
            return_value=True,
        ), patch(
            "tools.skill_manager_tool._security_scan_skill",
            side_effect=inspect_held_snapshot_then_retarget,
        ):
            result = _create_skill(
                "my-skill",
                VALID_SKILL_CONTENT,
                category="devops",
            )

        assert scanned is True
        assert result["success"] is False
        assert "path changed" in result["error"].lower()
        assert not (outside_a / "devops").exists()
        assert (victim_dir / "SKILL.md").read_text(
            encoding="utf-8"
        ) == victim_content

    def test_create_fails_closed_without_a_secure_platform_backend(
        self, tmp_path
    ):
        with _skill_dir(tmp_path), patch(
            "tools.skill_manager_tool._secure_directory_create_supported",
            return_value=False,
        ):
            result = _create_skill(
                "my-skill", VALID_SKILL_CONTENT, category="devops"
            )

        assert result["success"] is False
        assert "secure skill creation" in result["error"].lower()
        assert not (tmp_path / "devops").exists()

    def test_windows_backend_dispatches_categorized_creation(self):
        expected = (
            Path("C:/skills/devops/my-skill"),
            None,
        )
        with patch(
            "tools.skill_manager_tool._secure_directory_create_supported",
            return_value=True,
        ), patch(
            "tools.skill_manager_tool.os.name",
            "nt",
        ), patch(
            "tools.skill_manager_tool._secure_create_and_write_skill_windows",
            return_value=expected,
        ) as windows_create:
            result = skill_manager_module._secure_create_and_write_skill(
                "my-skill",
                "devops",
                _content_for_name(VALID_SKILL_CONTENT, "my-skill"),
            )

        assert result == expected
        windows_create.assert_called_once_with(
            "my-skill",
            "devops",
            _content_for_name(VALID_SKILL_CONTENT, "my-skill"),
        )

    def test_windows_backend_probe_failure_fails_closed(self):
        with patch(
            "tools.skill_manager_tool._secure_directory_create_supported",
            return_value=False,
        ), patch(
            "tools.skill_manager_tool.os.name",
            "nt",
        ):
            result = skill_manager_module._secure_create_and_write_skill(
                "my-skill",
                "devops",
                _content_for_name(VALID_SKILL_CONTENT, "my-skill"),
            )

        assert result[0] is None
        assert "unavailable on Windows" in result[1]

    def test_windows_opened_handle_reparse_tag_is_rejected(self):
        """A post-attributes junction swap is caught on the opened handle."""
        import ctypes
        from types import SimpleNamespace

        create_file = MagicMock(return_value=123)
        get_attributes = MagicMock(return_value=0)
        close_handle = MagicMock(return_value=True)

        def report_reparse_tag(_handle, info_class, info_ptr, _size):
            assert info_class == 9
            fields = ctypes.cast(
                info_ptr,
                ctypes.POINTER(ctypes.c_uint32 * 2),
            ).contents
            fields[0] = 0x00000400
            fields[1] = 0xA0000003
            return True

        get_information = MagicMock(side_effect=report_reparse_tag)
        kernel32 = SimpleNamespace(
            CreateFileW=create_file,
            GetFileAttributesW=get_attributes,
            GetFileInformationByHandleEx=get_information,
            CloseHandle=close_handle,
        )
        with patch(
            "ctypes.windll",
            SimpleNamespace(kernel32=kernel32),
            create=True,
        ):
            with pytest.raises(OSError, match="redirected directory"):
                skill_manager_module._open_windows_directory_guard(
                    Path("C:/skills"),
                    Path("C:/skills"),
                )

        get_information.assert_called_once()
        close_handle.assert_called_once_with(123)

    def test_windows_canonical_mutation_uses_held_backend_handle(
        self,
    ):
        from contextlib import contextmanager

        existing = {
            "path": Path("C:/skills/example"),
            "_resolved_path": Path("C:/skills/example"),
            "_dir_identity": (1, 2),
        }
        held = MagicMock()
        held.identity = (1, 2)

        @contextmanager
        def open_held(_existing):
            yield held, existing["_resolved_path"]

        with patch(
            "tools.skill_manager_tool._secure_directory_create_supported",
            return_value=True,
        ), patch(
            "tools.skill_manager_tool.os.name", "nt"
        ), patch(
            "tools.skill_manager_tool._open_existing_skill_directory",
            side_effect=open_held,
        ), patch(
            "tools.skill_manager_tool._replace_canonical_skill_md",
        ) as replace:
            error = skill_manager_module._secure_replace_existing_skill_md(
                existing,
                _content_for_name(VALID_SKILL_CONTENT_2, "example"),
            )

        assert error is None
        replace.assert_called_once_with(
            held,
            _content_for_name(VALID_SKILL_CONTENT_2, "example"),
        )

    def test_windows_supporting_mutation_walks_held_backend_handles(
        self,
    ):
        from contextlib import contextmanager
        from pathlib import PureWindowsPath

        existing = {
            "path": Path("C:/skills/example"),
            "_resolved_path": Path("C:/skills/example"),
            "_dir_identity": (1, 2),
        }
        skill_handle = MagicMock()
        skill_handle.identity = (1, 2)
        references_handle = MagicMock()
        references_handle.identity = (1, 3)
        skill_handle.open_dir.return_value = references_handle
        skill_handle.entry_identity.return_value = references_handle.identity

        @contextmanager
        def open_held(_existing):
            yield skill_handle, existing["_resolved_path"]

        with patch(
            "tools.skill_manager_tool._secure_directory_create_supported",
            return_value=True,
        ), patch(
            "tools.skill_manager_tool.os.name", "nt"
        ), patch(
            "tools.skill_manager_tool._open_existing_skill_directory",
            side_effect=open_held,
        ), patch(
            "tools.skill_manager_tool.Path",
            PureWindowsPath,
        ):
            with skill_manager_module._open_supporting_file_parent(
                existing,
                "references/example.md",
                create_parents=False,
            ) as opened:
                assert opened[:4] == (
                    skill_handle,
                    references_handle,
                    "example.md",
                    existing["_resolved_path"],
                )
                assert opened[4]() is True

        skill_handle.open_dir.assert_called_once_with(
            "references", writable=True
        )
        references_handle.close.assert_called_once()

    def test_windows_native_entrypoint_refuses_non_windows_host_without_writes(
        self, tmp_path
    ):
        content = _content_for_name(VALID_SKILL_CONTENT, "windows-skill")
        with _skill_dir(tmp_path):
            result = (
                skill_manager_module._secure_create_and_write_skill_windows(
                    "windows-skill",
                    "devops",
                    content,
                )
            )

        assert result[0] is None
        assert "unavailable on Windows" in result[1]
        assert not (tmp_path / "devops" / "windows-skill").exists()
        assert not (tmp_path / "devops").exists()

    def test_concurrent_same_name_across_categories_has_one_winner(
        self, tmp_path
    ):
        content = _content_for_name(VALID_SKILL_CONTENT, "shared-name")

        def create(category):
            return _create_skill_impl("shared-name", content, category)

        with _skill_dir(tmp_path), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ("first", "second")))

        assert sum(result["success"] for result in results) == 1
        assert len(list(tmp_path.rglob("shared-name/SKILL.md"))) == 1

    def test_cross_process_same_name_has_one_winner(self, tmp_path):
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            pytest.skip("fork multiprocessing context unavailable")

        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_process_create_same_skill,
                args=(str(tmp_path), category, start, results),
            )
            for category in ("first", "second")
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        assert sum(outcomes) == 1
        assert len(list(tmp_path.rglob("shared-process-name/SKILL.md"))) == 1

    def test_create_does_not_reuse_preexisting_non_skill_directory(self, tmp_path):
        existing = tmp_path / "my-skill"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with _skill_dir(tmp_path):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT)

        assert result["success"] is False
        assert marker.read_text(encoding="utf-8") == "keep"
        assert not (existing / "SKILL.md").exists()

    def test_scan_rejection_rolls_back_only_this_create(self, tmp_path):
        with _skill_dir(tmp_path), patch(
            "tools.skill_manager_tool._agent_created_security_scan_enabled",
            return_value=True,
        ), patch(
            "tools.skill_manager_tool._security_scan_skill",
            return_value="blocked",
        ):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT)

        assert result == {"success": False, "error": "blocked"}
        assert not (tmp_path / "my-skill").exists()

    def test_encoding_failure_rolls_back_temporary_create(self, tmp_path):
        content = _content_for_name(VALID_SKILL_CONTENT, "surrogate-skill")
        content += "\n\ud800"
        with _skill_dir(tmp_path):
            result = _create_skill_impl("surrogate-skill", content)

        assert result["success"] is False
        assert not (tmp_path / "surrogate-skill").exists()
        assert not list(tmp_path.rglob(".tmp_SKILL_*"))

    def test_create_normalizes_single_bom_before_persisting(self, tmp_path):
        content = "\ufeff" + _content_for_name(VALID_SKILL_CONTENT, "bom-skill")
        with _skill_dir(tmp_path):
            result = _create_skill_impl("bom-skill", content)

        assert result["success"] is True
        saved = (tmp_path / "bom-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert not saved.startswith("\ufeff")
        assert parse_frontmatter(saved)[0]["name"] == "bom-skill"

    def test_create_long_desc_rejected(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill("long-desc", LONG_DESC_CONTENT)
        assert result["success"] is False
        assert "system-prompt budget" in result["error"]

    def test_create_short_desc_no_prompt_preview(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _create_skill("my-skill", VALID_SKILL_CONTENT)
        assert result["success"] is True
        assert "system_prompt_preview" not in result

    def test_create_boundary_at_limit_accepted_no_preview(self, tmp_path):
        desc = "U" * SKILL_PROMPT_DESC_LIMIT
        content = f"---\nname: boundary-at\ndescription: {desc}\n---\n\n# Boundary\n\nStep 1.\n"
        with _skill_dir(tmp_path):
            result = _create_skill("boundary-at", content)
        assert result["success"] is True
        assert "system_prompt_preview" not in result

    def test_create_boundary_over_limit_rejected(self, tmp_path):
        desc = "U" * (SKILL_PROMPT_DESC_LIMIT + 1)
        content = f"---\nname: boundary-over\ndescription: {desc}\n---\n\n# Boundary\n\nStep 1.\n"
        with _skill_dir(tmp_path):
            result = _create_skill("boundary-over", content)
        assert result["success"] is False
        assert "system-prompt budget" in result["error"]

    def test_edit_long_desc_still_allowed_with_preview(self, tmp_path):
        """Edit/patch paths stay permissive so existing over-limit skills
        remain maintainable — they warn via system_prompt_preview instead."""
        content = _content_for_name(LONG_DESC_CONTENT, "my-skill")
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _edit_skill("my-skill", content)
        assert result["success"] is True
        assert "system_prompt_preview" in result
        assert "System prompt will show" in result["system_prompt_preview"]
        fm, _ = parse_frontmatter(content)
        assert extract_skill_description(fm) in result["system_prompt_preview"]


class TestEditSkill:
    @pytest.mark.parametrize("action", ["edit", "patch", "write_file"])
    def test_canonical_skill_symlink_never_writes_outside(
        self, tmp_path, action
    ):
        outside = tmp_path / "outside.md"
        outside.write_text(
            _content_for_name(VALID_SKILL_CONTENT, "evil"),
            encoding="utf-8",
        )
        skill_dir = tmp_path / "evil"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").symlink_to(outside)
        replacement = _content_for_name(VALID_SKILL_CONTENT_2, "evil")

        with _skill_dir(tmp_path):
            if action == "edit":
                result = _edit_skill("evil", replacement)
            elif action == "patch":
                result = _patch_skill(
                    "evil",
                    "Do the thing.",
                    "Do something else.",
                )
            else:
                result = _write_file("evil", "SKILL.md", replacement)

        assert result["success"] is False
        assert outside.read_text(encoding="utf-8") == _content_for_name(
            VALID_SKILL_CONTENT, "evil"
        )

    def test_canonical_symlink_swap_is_replaced_not_followed(self, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("outside stays unchanged", encoding="utf-8")
        replacement = _content_for_name(VALID_SKILL_CONTENT_2, "my-skill")

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            skill_md = tmp_path / "my-skill" / "SKILL.md"
            real_replace = os.replace
            planted = False

            def plant_symlink_before_replace(src, dst, **kwargs):
                nonlocal planted
                if (
                    not planted
                    and dst == "SKILL.md"
                    and kwargs.get("dst_dir_fd") is not None
                ):
                    planted = True
                    skill_md.unlink()
                    skill_md.symlink_to(outside)
                return real_replace(src, dst, **kwargs)

            with patch(
                "tools.skill_manager_tool.os.replace",
                side_effect=plant_symlink_before_replace,
            ):
                result = _edit_skill("my-skill", replacement)

        assert result["success"] is True
        assert outside.read_text(encoding="utf-8") == "outside stays unchanged"
        assert not skill_md.is_symlink()
        assert "Updated description." in skill_md.read_text(encoding="utf-8")

    @pytest.mark.parametrize("action", ["edit", "patch", "write_file"])
    def test_skill_root_retarget_keeps_canonical_transaction_on_one_directory(
        self, tmp_path, action
    ):
        """Read/write/scan/rollback must not mix A and B through a root link."""
        outside_a = tmp_path / "outside-a"
        outside_b = tmp_path / "outside-b"
        root_link = tmp_path / "skills"
        for root in (outside_a, outside_b):
            # Deliberately use a legacy directory name so lookup must inspect
            # frontmatter through its held candidate directory descriptor.
            skill_dir = root / "legacy-dir"
            skill_dir.mkdir(parents=True)
        (outside_a / "legacy-dir" / "SKILL.md").write_text(
            _content_for_name(VALID_SKILL_CONTENT, "shared"),
            encoding="utf-8",
        )
        (outside_b / "legacy-dir" / "SKILL.md").write_text(
            _content_for_name(VALID_SKILL_CONTENT, "shared")
            + "\nB-VICTIM-MUST-NEVER-BE-COPIED\n",
            encoding="utf-8",
        )
        original_a = (outside_a / "legacy-dir" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        original_b = (outside_b / "legacy-dir" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        root_link.symlink_to(outside_a, target_is_directory=True)
        replacement = _content_for_name(VALID_SKILL_CONTENT_2, "shared")
        retargeted = False
        real_read = skill_manager_module._read_canonical_skill_md

        def retarget_during_candidate_read(skill_fd):
            nonlocal retargeted
            if not retargeted:
                retargeted = True
                root_link.unlink()
                root_link.symlink_to(outside_b, target_is_directory=True)
            return real_read(skill_fd)

        def reject_anchored_snapshot(snapshot):
            scanned = (snapshot / "SKILL.md").read_text(encoding="utf-8")
            assert "B-VICTIM-MUST-NEVER-BE-COPIED" not in scanned
            if action == "patch":
                assert "Do something else." in scanned
            else:
                assert "Updated description." in scanned
            return "blocked after anchored scan"

        with _skill_dir(root_link), patch(
            "tools.skill_manager_tool._read_canonical_skill_md",
            side_effect=retarget_during_candidate_read,
        ), patch(
            "tools.skill_manager_tool._agent_created_security_scan_enabled",
            return_value=True,
        ), patch(
            "tools.skill_manager_tool._security_scan_skill",
            side_effect=reject_anchored_snapshot,
        ):
            if action == "edit":
                result = _edit_skill("shared", replacement)
            elif action == "patch":
                result = _patch_skill(
                    "shared",
                    "Do the thing.",
                    "Do something else.",
                )
            else:
                result = _write_file("shared", "SKILL.md", replacement)

        assert retargeted is True
        assert result["success"] is False
        assert "blocked after anchored scan" in result["error"]
        assert (outside_a / "legacy-dir" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == original_a
        assert (outside_b / "legacy-dir" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == original_b

    def test_edit_existing_skill(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _edit_skill(
                "my-skill", _content_for_name(VALID_SKILL_CONTENT_2, "my-skill")
            )
        assert result["success"] is True
        content = (tmp_path / "my-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "Updated description" in content

    def test_failed_rollback_cannot_overwrite_concurrent_success(
        self, tmp_path
    ):
        failed_content = _content_for_name(
            VALID_SKILL_CONTENT_2.replace(
                "# Test Skill v2", "# Failed transaction"
            ),
            "transaction-skill",
        )
        successful_content = _content_for_name(
            VALID_SKILL_CONTENT_2.replace(
                "# Test Skill v2", "# Successful transaction"
            ),
            "transaction-skill",
        )
        failed_scan_entered = threading.Event()
        release_failed_scan = threading.Event()
        successful_done = threading.Event()

        def controlled_scan(skill_fd):
            current = skill_manager_module._read_canonical_skill_md(skill_fd)
            if "# Failed transaction" in current:
                failed_scan_entered.set()
                assert release_failed_scan.wait(timeout=10)
                return "blocked by deterministic scan"
            return None

        with _skill_dir(tmp_path):
            _create_skill("transaction-skill", VALID_SKILL_CONTENT)
            with patch(
                "tools.skill_manager_tool._security_scan_held_skill",
                side_effect=controlled_scan,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                failed_future = pool.submit(
                    _edit_skill, "transaction-skill", failed_content
                )
                assert failed_scan_entered.wait(timeout=10)

                def successful_edit():
                    result = _edit_skill(
                        "transaction-skill", successful_content
                    )
                    successful_done.set()
                    return result

                successful_future = pool.submit(successful_edit)
                assert not successful_done.wait(timeout=0.2)
                release_failed_scan.set()
                failed_result = failed_future.result(timeout=10)
                successful_result = successful_future.result(timeout=10)

        assert failed_result["success"] is False
        assert successful_result["success"] is True
        assert (
            tmp_path / "transaction-skill" / "SKILL.md"
        ).read_text(encoding="utf-8") == successful_content

    def test_directory_and_frontmatter_aliases_share_physical_transaction_lock(
        self, tmp_path
    ):
        """Rollback through one alias cannot clobber a commit via the other."""
        skill_dir = tmp_path / "legacy-dir"
        references = skill_dir / "references"
        references.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            _content_for_name(VALID_SKILL_CONTENT, "shared"),
            encoding="utf-8",
        )
        target = references / "state.txt"
        target.write_text("original", encoding="utf-8")
        failed_scan_entered = threading.Event()
        release_failed_scan = threading.Event()
        successful_done = threading.Event()

        def controlled_scan(skill_fd):
            file_fd = os.open(
                "references/state.txt",
                os.O_RDONLY,
                dir_fd=skill_fd,
            )
            try:
                current = os.read(file_fd, 100).decode("utf-8")
            finally:
                os.close(file_fd)
            if current == "failed":
                failed_scan_entered.set()
                assert release_failed_scan.wait(timeout=10)
                return "blocked by deterministic alias scan"
            return None

        with _skill_dir(tmp_path), patch(
            "tools.skill_manager_tool._security_scan_held_skill",
            side_effect=controlled_scan,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            failed_future = pool.submit(
                _write_file,
                "legacy-dir",
                "references/state.txt",
                "failed",
            )
            assert failed_scan_entered.wait(timeout=10)

            def successful_write():
                result = _write_file(
                    "shared",
                    "references/state.txt",
                    "successful",
                )
                successful_done.set()
                return result

            successful_future = pool.submit(successful_write)
            assert not successful_done.wait(timeout=0.2)
            release_failed_scan.set()
            failed_result = failed_future.result(timeout=10)
            successful_result = successful_future.result(timeout=10)

        assert failed_result["success"] is False
        assert successful_result["success"] is True
        assert target.read_text(encoding="utf-8") == "successful"

    def test_threaded_alias_write_rollback_serializes_remove(self, tmp_path):
        """A remove through one alias cannot race a rollback through another."""
        skill_dir = tmp_path / "legacy-directory"
        target = skill_dir / "references" / "state.txt"
        target.parent.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            _content_for_name(VALID_SKILL_CONTENT, "frontmatter-alias"),
            encoding="utf-8",
        )
        target.write_text("original", encoding="utf-8")
        failed_scan_entered = threading.Event()
        release_failed_scan = threading.Event()
        remove_done = threading.Event()

        def controlled_scan(skill_fd):
            file_fd = os.open("references/state.txt", os.O_RDONLY, dir_fd=skill_fd)
            try:
                current = os.read(file_fd, 100).decode("utf-8")
            finally:
                os.close(file_fd)
            if current == "failed":
                failed_scan_entered.set()
                assert release_failed_scan.wait(timeout=10)
                return "blocked by deterministic write/remove scan"
            return None

        with _skill_dir(tmp_path), patch(
            "tools.skill_manager_tool._security_scan_held_skill",
            side_effect=controlled_scan,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            write_future = pool.submit(
                _write_file,
                "legacy-directory",
                "references/state.txt",
                "failed",
            )
            assert failed_scan_entered.wait(timeout=10)

            def remove_file():
                result = _remove_file(
                    "frontmatter-alias", "references/state.txt"
                )
                remove_done.set()
                return result

            remove_future = pool.submit(remove_file)
            assert not remove_done.wait(timeout=0.2)
            release_failed_scan.set()
            write_result = write_future.result(timeout=10)
            remove_result = remove_future.result(timeout=10)

        assert write_result["success"] is False
        assert remove_result["success"] is True
        assert not target.exists()

    def test_failed_rollback_cannot_overwrite_cross_process_success(
        self, tmp_path
    ):
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            pytest.skip("fork multiprocessing context unavailable")

        failed_content = _content_for_name(
            VALID_SKILL_CONTENT_2.replace(
                "# Test Skill v2", "# Failed process transaction"
            ),
            "transaction-skill",
        )
        successful_content = _content_for_name(
            VALID_SKILL_CONTENT_2.replace(
                "# Test Skill v2", "# Successful process transaction"
            ),
            "transaction-skill",
        )
        with _skill_dir(tmp_path):
            _create_skill("transaction-skill", VALID_SKILL_CONTENT)

        failed_entered = context.Event()
        successful_entered = context.Event()
        release_failed = context.Event()
        failed_done = context.Event()
        successful_done = context.Event()
        results = context.Queue()
        failed_process = context.Process(
            target=_process_edit_with_controlled_scan,
            args=(
                str(tmp_path),
                failed_content,
                True,
                failed_entered,
                release_failed,
                failed_done,
                results,
            ),
        )
        successful_process = context.Process(
            target=_process_edit_with_controlled_scan,
            args=(
                str(tmp_path),
                successful_content,
                False,
                successful_entered,
                release_failed,
                successful_done,
                results,
            ),
        )
        failed_process.start()
        assert failed_entered.wait(timeout=10)
        successful_process.start()
        assert not successful_done.wait(timeout=0.2)
        assert not successful_entered.is_set()
        release_failed.set()

        outcomes = [results.get(timeout=10) for _ in range(2)]
        for process in (failed_process, successful_process):
            process.join(timeout=10)
            assert process.exitcode == 0

        assert sorted(result["success"] for result in outcomes) == [False, True]
        assert (
            tmp_path / "transaction-skill" / "SKILL.md"
        ).read_text(encoding="utf-8") == successful_content

    def test_cross_process_directory_and_frontmatter_aliases_serialize_transaction(
        self, tmp_path
    ):
        """Alias transactions share one physical lock across processes.

        The first process writes through the legacy directory basename and
        pauses after its held-directory scan rejects the write.  A second
        process addresses the same directory via its frontmatter name.  It
        must neither scan the failed content nor commit before the first
        process has rolled back.
        """
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            pytest.skip("fork multiprocessing context unavailable")

        skill_dir = tmp_path / "legacy-directory"
        target = skill_dir / "references" / "state.txt"
        target.parent.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            _content_for_name(VALID_SKILL_CONTENT, "frontmatter-alias"),
            encoding="utf-8",
        )
        target.write_text("original", encoding="utf-8")

        failed_entered = context.Event()
        successful_entered = context.Event()
        release_failed = context.Event()
        failed_done = context.Event()
        successful_done = context.Event()
        results = context.Queue()
        failed_process = context.Process(
            target=_process_write_alias_with_controlled_scan,
            args=(
                str(tmp_path),
                "legacy-directory",
                "failed",
                True,
                failed_entered,
                release_failed,
                failed_done,
                results,
            ),
        )
        successful_process = context.Process(
            target=_process_write_alias_with_controlled_scan,
            args=(
                str(tmp_path),
                "frontmatter-alias",
                "successful",
                False,
                successful_entered,
                release_failed,
                successful_done,
                results,
            ),
        )

        failed_process.start()
        assert failed_entered.wait(timeout=10)
        successful_process.start()
        assert not successful_done.wait(timeout=0.2)
        assert not successful_entered.is_set()
        release_failed.set()

        outcomes = [results.get(timeout=10) for _ in range(2)]
        for process in (failed_process, successful_process):
            process.join(timeout=10)
            assert process.exitcode == 0

        result_by_success = {result["success"]: (result, snapshot) for result, snapshot in outcomes}
        failed_result, failed_snapshot = result_by_success[False]
        successful_result, successful_snapshot = result_by_success[True]
        assert "blocked by deterministic cross-process alias scan" in failed_result["error"]
        assert successful_result["success"] is True
        assert failed_snapshot == "failed"
        assert successful_snapshot == "successful"
        assert target.read_text(encoding="utf-8") == "successful"

    def test_cross_process_alias_edit_serializes_delete(self, tmp_path):
        """A delete cannot remove a skill during an edit's held-dir scan."""
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            pytest.skip("fork multiprocessing context unavailable")

        skill_dir = tmp_path / "legacy-directory"
        skill_dir.mkdir()
        updated = _content_for_name(
            VALID_SKILL_CONTENT_2, "frontmatter-alias"
        )
        (skill_dir / "SKILL.md").write_text(
            _content_for_name(VALID_SKILL_CONTENT, "frontmatter-alias"),
            encoding="utf-8",
        )
        edit_entered = context.Event()
        release_edit = context.Event()
        edit_done = context.Event()
        delete_done = context.Event()
        results = context.Queue()
        edit_process = context.Process(
            target=_process_edit_alias_with_controlled_scan,
            args=(
                str(tmp_path),
                "frontmatter-alias",
                updated,
                edit_entered,
                release_edit,
                edit_done,
                results,
            ),
        )
        delete_process = context.Process(
            target=_process_delete_alias,
            args=(str(tmp_path), "legacy-directory", delete_done, results),
        )

        edit_process.start()
        assert edit_entered.wait(timeout=10)
        delete_process.start()
        assert not delete_done.wait(timeout=0.2)
        release_edit.set()

        outcomes = [results.get(timeout=10) for _ in range(2)]
        for process in (edit_process, delete_process):
            process.join(timeout=10)
            assert process.exitcode == 0

        assert all(result["success"] for result in outcomes)
        assert not skill_dir.exists()

    def test_edit_nonexistent_skill(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _edit_skill(
                "nonexistent",
                _content_for_name(VALID_SKILL_CONTENT, "nonexistent"),
            )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_edit_invalid_content_rejected(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _edit_skill("my-skill", "no frontmatter")
        assert result["success"] is False
        # Original content should be preserved
        content = (tmp_path / "my-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "A test skill" in content

    def test_edit_rejects_frontmatter_identity_change(self, tmp_path):
        original = _content_for_name(VALID_SKILL_CONTENT, "my-skill")
        mismatched = _content_for_name(VALID_SKILL_CONTENT_2, "other-skill")
        with _skill_dir(tmp_path):
            _create_skill("my-skill", original)
            result = _edit_skill("my-skill", mismatched)
        assert result["success"] is False
        assert "must match" in result["error"]
        assert (tmp_path / "my-skill" / "SKILL.md").read_text() == original

    def test_edit_rejects_ambiguous_skill_identity(self, tmp_path):
        original = _content_for_name(VALID_SKILL_CONTENT, "same-skill")
        for category in ("a", "b"):
            skill_dir = tmp_path / category / "same-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(original, encoding="utf-8")

        updated = _content_for_name(VALID_SKILL_CONTENT_2, "same-skill")
        with _skill_dir(tmp_path):
            result = _edit_skill("same-skill", updated)

        assert result["success"] is False
        assert "ambiguous" in result["error"]
        for category in ("a", "b"):
            assert (
                tmp_path / category / "same-skill" / "SKILL.md"
            ).read_text(encoding="utf-8") == original

    def test_unavailable_external_root_fails_closed_before_local_edit(
        self, tmp_path
    ):
        local = tmp_path / "local"
        missing_external = tmp_path / "configured-but-missing"
        local.mkdir()
        with patch(
            "tools.skill_manager_tool.SKILLS_DIR", local
        ), patch(
            "agent.skill_utils.get_all_skills_dirs",
            return_value=[local, missing_external],
        ):
            created = _create_skill(
                "my-skill",
                VALID_SKILL_CONTENT,
            )
            assert created["success"] is False

            skill_dir = local / "my-skill"
            skill_dir.mkdir()
            original = _content_for_name(
                VALID_SKILL_CONTENT, "my-skill"
            )
            (skill_dir / "SKILL.md").write_text(
                original, encoding="utf-8"
            )
            result = _edit_skill(
                "my-skill",
                _content_for_name(VALID_SKILL_CONTENT_2, "my-skill"),
            )

        assert result["success"] is False
        assert "lookup is incomplete" in result["error"].lower()
        assert (skill_dir / "SKILL.md").read_text(
            encoding="utf-8"
        ) == original

    def test_unreadable_candidate_metadata_fails_closed_before_local_edit(
        self, tmp_path
    ):
        local = tmp_path / "local"
        external = tmp_path / "external"
        local_skill = local / "my-skill"
        hidden_candidate = external / "legacy-directory"
        local_skill.mkdir(parents=True)
        hidden_candidate.mkdir(parents=True)
        original = _content_for_name(VALID_SKILL_CONTENT, "my-skill")
        (local_skill / "SKILL.md").write_text(original, encoding="utf-8")
        (hidden_candidate / "SKILL.md").write_text(
            original, encoding="utf-8"
        )

        with patch(
            "tools.skill_manager_tool.SKILLS_DIR", local
        ), patch(
            "agent.skill_utils.get_all_skills_dirs",
            return_value=[local, external],
        ), patch(
            "tools.skill_manager_tool._read_canonical_skill_md",
            side_effect=UnicodeError("cannot decode candidate"),
        ):
            result = _edit_skill(
                "my-skill",
                _content_for_name(VALID_SKILL_CONTENT_2, "my-skill"),
            )

        assert result["success"] is False
        assert "lookup is incomplete" in result["error"].lower()
        assert (local_skill / "SKILL.md").read_text(
            encoding="utf-8"
        ) == original

    def test_edit_long_desc_includes_prompt_preview(self, tmp_path):
        edit_content = LONG_DESC_CONTENT.replace("name: long-desc", "name: test-skill")
        with _skill_dir(tmp_path):
            _create_skill("test-skill", VALID_SKILL_CONTENT)
            result = _edit_skill("test-skill", edit_content)
        assert result["success"] is True
        assert "system_prompt_preview" in result


class TestPatchSkill:
    def test_patch_unique_match(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _patch_skill("my-skill", "Do the thing.", "Do the new thing.")
        assert result["success"] is True
        content = (tmp_path / "my-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "Do the new thing." in content

    def test_canonical_patch_closes_directory_context_on_fuzzy_exception(
        self, tmp_path
    ):
        session = MagicMock()
        session.__enter__.return_value = (
            123,
            tmp_path / "my-skill",
        )
        session.__exit__.return_value = False
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            with patch(
                "tools.skill_manager_tool._open_existing_skill_directory",
                return_value=session,
            ), patch(
                "tools.skill_manager_tool._read_canonical_skill_md",
                return_value=_content_for_name(
                    VALID_SKILL_CONTENT,
                    "my-skill",
                ),
            ), patch(
                "tools.fuzzy_match.fuzzy_find_and_replace",
                side_effect=RuntimeError("unexpected fuzzy failure"),
            ):
                result = _patch_skill(
                    "my-skill",
                    "Do the thing.",
                    "Do something else.",
                )

        assert result["success"] is False
        assert "unexpected fuzzy failure" in result["error"]
        assert session.__exit__.call_count == 2
        assert session.__exit__.call_args_list[0].args == (None, None, None)
        assert session.__exit__.call_args_list[1].args[0] is RuntimeError

    def test_patch_rejects_frontmatter_identity_change(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            original = (tmp_path / "my-skill" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            result = _patch_skill(
                "my-skill", "name: my-skill", "name: other-skill"
            )
        assert result["success"] is False
        assert "must match" in result["error"]
        assert (tmp_path / "my-skill" / "SKILL.md").read_text() == original

    def test_patch_nonexistent_string(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _patch_skill("my-skill", "this text does not exist", "replacement")
        assert result["success"] is False
        assert "not found" in result["error"].lower() or "could not find" in result["error"].lower()

    def test_patch_ambiguous_match_rejected(self, tmp_path):
        content = """\
---
name: test-skill
description: A test skill.
---

# Test

word word
"""
        with _skill_dir(tmp_path):
            _create_skill("my-skill", content)
            result = _patch_skill("my-skill", "word", "replaced")
        assert result["success"] is False
        assert "match" in result["error"].lower()

    def test_patch_supporting_file_symlink_escape_blocked(self, tmp_path):
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("old text here", encoding="utf-8")

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            link = tmp_path / "my-skill" / "references" / "evil.md"
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(outside_file)
            except OSError:
                pytest.skip("Symlinks not supported")

            result = _patch_skill("my-skill", "old text", "new text", file_path="references/evil.md")

        assert result["success"] is False
        assert "escapes" in result["error"].lower()
        assert outside_file.read_text() == "old text here"

    @pytest.mark.parametrize("action", ["patch", "write_file"])
    def test_supporting_parent_symlink_swap_after_validation_is_rejected(
        self, tmp_path, action
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_target = outside / "api.md"
        outside_target.write_text("outside old text", encoding="utf-8")

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            references = tmp_path / "my-skill" / "references"
            references.mkdir()
            inside_target = references / "api.md"
            inside_target.write_text("inside old text", encoding="utf-8")
            moved_references = tmp_path / "my-skill" / "held-references"
            real_replace = skill_manager_module._replace_held_regular_text
            swapped = False

            def replace_then_swap(parent_fd, filename, content, **kwargs):
                nonlocal swapped
                real_replace(parent_fd, filename, content, **kwargs)
                if not swapped:
                    swapped = True
                    references.rename(moved_references)
                    references.symlink_to(outside, target_is_directory=True)

            with patch(
                "tools.skill_manager_tool._replace_held_regular_text",
                side_effect=replace_then_swap,
            ):
                if action == "patch":
                    result = _patch_skill(
                        "my-skill",
                        "old text",
                        "new text",
                        file_path="references/api.md",
                    )
                else:
                    result = _write_file(
                        "my-skill",
                        "references/api.md",
                        "new text",
                    )

        assert swapped is True
        assert result["success"] is False
        assert "path changed" in result["error"].lower()
        assert outside_target.read_text(encoding="utf-8") == "outside old text"
        assert (
            moved_references / "api.md"
        ).read_text(encoding="utf-8") == "inside old text"


class TestDeleteSkill:
    def test_delete_cleans_empty_category_dir(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT, category="devops")
            _delete_skill("my-skill")
        assert not (tmp_path / "devops").exists()


    def test_delete_with_absorbed_into_equals_self_rejected(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("narrow", VALID_SKILL_CONTENT)
            result = _delete_skill("narrow", absorbed_into="narrow")
        assert result["success"] is False
        assert "cannot equal" in result["error"]
        assert (tmp_path / "narrow").exists()

# ---------------------------------------------------------------------------
# write_file / remove_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def test_write_reference_file(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _write_file("my-skill", "references/api.md", "# API\nEndpoint docs.")
        assert result["success"] is True
        assert (tmp_path / "my-skill" / "references" / "api.md").exists()

    def test_write_to_nonexistent_skill(self, tmp_path):
        with _skill_dir(tmp_path):
            result = _write_file("nonexistent", "references/doc.md", "content")
        assert result["success"] is False

    def test_write_to_disallowed_path(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _write_file("my-skill", "secret/evil.py", "malicious")
        assert result["success"] is False

    def test_write_skill_md_uses_validated_edit_contract(self, tmp_path):
        mismatched = _content_for_name(VALID_SKILL_CONTENT_2, "other-skill")
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            original = (tmp_path / "my-skill" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            result = _write_file("my-skill", "SKILL.md", mismatched)
        assert result["success"] is False
        assert "must match" in result["error"]
        assert (tmp_path / "my-skill" / "SKILL.md").read_text() == original

    def test_write_support_directory_skill_md_remains_supporting_file(
        self, tmp_path
    ):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            original = (tmp_path / "my-skill" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            result = _write_file(
                "my-skill",
                "references/SKILL.md",
                "preserved package",
            )
        assert result["success"] is True
        assert (
            tmp_path / "my-skill" / "references" / "SKILL.md"
        ).read_text() == "preserved package"
        assert (tmp_path / "my-skill" / "SKILL.md").read_text() == original

    def test_write_symlink_escape_blocked(self, tmp_path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            link = tmp_path / "my-skill" / "references" / "escape"
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(outside_dir, target_is_directory=True)
            except OSError:
                pytest.skip("Symlinks not supported")

            result = _write_file("my-skill", "references/escape/owned.md", "malicious")

        assert result["success"] is False
        assert "escapes" in result["error"].lower()
        assert not (outside_dir / "owned.md").exists()


class TestRemoveFile:
    def test_remove_existing_file(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            _write_file("my-skill", "references/api.md", "content")
            result = _remove_file("my-skill", "references/api.md")
        assert result["success"] is True
        assert not (tmp_path / "my-skill" / "references" / "api.md").exists()

    def test_remove_reports_success_when_empty_parent_cleanup_fails(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            _write_file("my-skill", "references/api.md", "content")
            with patch(
                "tools.skill_manager_tool._secure_cleanup_empty_support_parent",
                side_effect=OSError("injected cleanup failure"),
            ):
                result = _remove_file("my-skill", "references/api.md")

        assert result["success"] is True
        assert not (tmp_path / "my-skill" / "references" / "api.md").exists()

    def test_remove_nonexistent_file(self, tmp_path):
        from tools.skills_tool import MAX_LINKED_FILES_PER_CATEGORY

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            for index in reversed(range(MAX_LINKED_FILES_PER_CATEGORY + 5)):
                _write_file(
                    "my-skill",
                    f"references/{index:03d}.md",
                    "content",
                )
            result = _remove_file("my-skill", "references/nope.md")
        assert result["success"] is False
        assert len(result["available_files"]) == MAX_LINKED_FILES_PER_CATEGORY
        assert result["available_files"] == sorted(result["available_files"])
        assert result["linked_files_summary"]["truncated"] is True

    def test_remove_skill_md_requires_delete_action(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            result = _remove_file("my-skill", "SKILL.md")
        assert result["success"] is False
        assert "delete" in result["error"]
        assert (tmp_path / "my-skill" / "SKILL.md").exists()

    def test_remove_symlink_escape_blocked(self, tmp_path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "keep.txt"
        outside_file.write_text("content", encoding="utf-8")

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            link = tmp_path / "my-skill" / "references" / "escape"
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(outside_dir, target_is_directory=True)
            except OSError:
                pytest.skip("Symlinks not supported")

            result = _remove_file("my-skill", "references/escape/keep.txt")

        assert result["success"] is False
        assert "escapes" in result["error"].lower()
        assert outside_file.exists()


# ---------------------------------------------------------------------------
# skill_manage dispatcher
# ---------------------------------------------------------------------------


class TestSkillManageDispatcher:
    def test_full_create_via_dispatcher(self, tmp_path):
        """Foreground create does NOT mark the skill as agent-created.

        Skills created by user-directed foreground turns belong to the user;
        only the background self-improvement review fork should mark its
        own sediment as agent-created (so the curator can later consolidate
        or prune it).
        """
        with _skill_dir(tmp_path):
            raw = skill_manage(action="create", name="test-skill", content=VALID_SKILL_CONTENT)
            from tools.skill_usage import load_usage
            usage = load_usage()
        result = json.loads(raw)
        assert result["success"] is True
        # No provenance marker on a foreground create — record either missing
        # entirely (telemetry best-effort) or present with created_by unset.
        rec = usage.get("test-skill") or {}
        assert rec.get("created_by") in {None, "", False}

    def test_create_from_background_review_marks_agent_created(self, tmp_path):
        """Background-review fork creates ARE marked as agent-created."""
        from tools.skill_provenance import set_current_write_origin, BACKGROUND_REVIEW
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            with _skill_dir(tmp_path):
                raw = skill_manage(
                    action="create",
                    name="review-sediment",
                    content=_content_for_name(VALID_SKILL_CONTENT, "review-sediment"),
                )
                from tools.skill_usage import load_usage
                usage = load_usage()
        finally:
            from tools.skill_provenance import reset_current_write_origin
            reset_current_write_origin(token)
        result = json.loads(raw)
        assert result["success"] is True
        assert usage["review-sediment"]["created_by"] == "agent"

    def test_delete_via_dispatcher_threads_absorbed_into(self, tmp_path):
        # Dispatcher must plumb absorbed_into through to _delete_skill so the
        # validation + message suffix paths are exercised end-to-end.
        with _skill_dir(tmp_path):
            skill_manage(action="create", name="umbrella", content=_content_for_name(VALID_SKILL_CONTENT, "umbrella"))
            skill_manage(action="create", name="narrow", content=_content_for_name(VALID_SKILL_CONTENT, "narrow"))
            raw = skill_manage(action="delete", name="narrow", absorbed_into="umbrella")
        result = json.loads(raw)
        assert result["success"] is True
        assert "absorbed into 'umbrella'" in result["message"]

    def test_delete_via_dispatcher_rejects_missing_absorbed_target(self, tmp_path):
        with _skill_dir(tmp_path):
            skill_manage(action="create", name="narrow", content=_content_for_name(VALID_SKILL_CONTENT, "narrow"))
            raw = skill_manage(action="delete", name="narrow", absorbed_into="ghost")
        result = json.loads(raw)
        assert result["success"] is False
        assert "does not exist" in result["error"]

    def test_background_review_delete_refuses_bundled_even_with_absorbed_into(self, tmp_path):
        from tools.skill_provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            with _skill_dir(tmp_path), \
                 patch("tools.skill_usage.is_protected_builtin", return_value=False), \
                 patch("tools.skill_usage.is_hub_installed", return_value=False), \
                 patch("tools.skill_usage.is_bundled",
                       side_effect=lambda skill_name: skill_name == "bundled"):
                skill_manage(action="create", name="umbrella", content=_content_for_name(VALID_SKILL_CONTENT, "umbrella"))
                skill_manage(action="create", name="bundled", content=_content_for_name(VALID_SKILL_CONTENT, "bundled"))
                raw = skill_manage(
                    action="delete",
                    name="bundled",
                    absorbed_into="umbrella",
                )
        finally:
            reset_current_write_origin(token)

        result = json.loads(raw)
        assert result["success"] is False
        assert "bundled" in result["error"].lower()
        assert (tmp_path / "bundled" / "SKILL.md").exists()


class TestSecurityScanGate:
    """_security_scan_skill is gated by skills.guard_agent_created config flag."""

    @pytest.mark.parametrize("mutation", ["create", "edit", "patch"])
    def test_disabled_guard_skips_held_snapshot_for_canonical_mutations(
        self, tmp_path, mutation
    ):
        """A disabled guard must not copy even a large skill tree to scan it."""
        replacement = _content_for_name(VALID_SKILL_CONTENT_2, "my-skill")
        with _skill_dir(tmp_path), patch(
            "tools.skill_manager_tool._GUARD_AVAILABLE", True
        ), patch(
            "tools.skill_manager_tool._guard_agent_created_enabled",
            return_value=False,
        ), patch(
            "tools.skill_manager_tool.os.fwalk"
        ) as mock_fwalk, patch(
            "tools.skill_manager_tool.shutil.copyfileobj"
        ) as mock_copy:
            if mutation == "create":
                result = _create_skill("my-skill", VALID_SKILL_CONTENT)
            else:
                assert _create_skill("my-skill", VALID_SKILL_CONTENT)["success"]
                assets = tmp_path / "my-skill" / "assets"
                assets.mkdir()
                (assets / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
                if mutation == "edit":
                    result = _edit_skill("my-skill", replacement)
                else:
                    result = _patch_skill(
                        "my-skill", "Do the thing.", "Do the new thing."
                    )

        assert result["success"] is True
        mock_fwalk.assert_not_called()
        mock_copy.assert_not_called()

    def test_scan_noop_when_flag_off(self, tmp_path):
        """Default config (flag off) short-circuits before running scan_skill."""
        from tools.skill_manager_tool import _security_scan_skill

        with patch("tools.skill_manager_tool._guard_agent_created_enabled", return_value=False), \
             patch("tools.skill_manager_tool.scan_skill") as mock_scan:
            result = _security_scan_skill(tmp_path)

        assert result is None
        mock_scan.assert_not_called()  # scan never ran

    def test_scan_blocks_dangerous_when_flag_on(self, tmp_path):
        """Dangerous verdict + flag on → returns an error string for the agent."""
        from tools.skill_manager_tool import _security_scan_skill
        from tools.skills_guard import ScanResult, Finding

        finding = Finding(
            pattern_id="test", severity="critical", category="exfiltration",
            file="SKILL.md", line=1, match="curl $TOKEN", description="test",
        )
        fake_result = ScanResult(
            skill_name="test",
            source="agent-created",
            trust_level="agent-created",
            verdict="dangerous",
            findings=[finding],
            summary="dangerous",
        )
        with patch("tools.skill_manager_tool._guard_agent_created_enabled", return_value=True), \
             patch("tools.skill_manager_tool.scan_skill", return_value=fake_result):
            result = _security_scan_skill(tmp_path)

        assert result is not None
        assert "Security scan blocked" in result

    def test_guard_flag_handles_config_error(self):
        """If load_config raises, _guard_agent_created_enabled defaults to False (fail-safe off)."""
        from tools.skill_manager_tool import _guard_agent_created_enabled

        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            assert _guard_agent_created_enabled() is False

    def test_guard_flag_quoted_false_stays_disabled(self):
        """Quoted 'false' from YAML edits must not enable the guard."""
        from tools.skill_manager_tool import _guard_agent_created_enabled

        for quoted in ("false", "False", "0", "no", "off"):
            with patch("hermes_cli.config.load_config",
                       return_value={"skills": {"guard_agent_created": quoted}}):
                assert _guard_agent_created_enabled() is False, \
                    f"guard_agent_created={quoted!r} must coerce to False"


# ---------------------------------------------------------------------------
# External skills directories (skills.external_dirs) — mutations in place
# ---------------------------------------------------------------------------


@contextmanager
def _two_roots(local_dir: Path, external_dir: Path):
    """Patch the skill manager so local SKILLS_DIR = local_dir and
    get_all_skills_dirs() returns [local_dir, external_dir] in order."""
    with patch("tools.skill_manager_tool.SKILLS_DIR", local_dir), \
         patch("agent.skill_utils.get_all_skills_dirs",
               return_value=[local_dir, external_dir]):
        yield


def _write_external_skill(external_dir: Path, name: str = "ext-skill") -> Path:
    skill_dir = external_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: An external skill.\n---\n\n"
        "# External\n\nBody with OLD_MARKER here.\n"
    )
    return skill_dir


class TestExternalSkillMutations:
    """Verify skill_manage can patch/edit/write/remove/delete skills that live
    under skills.external_dirs — in place, without duplicating to local.

    Regression for issues #4759 and #4381: the read-only gate used to refuse
    with 'Skill X is in an external directory and cannot be modified', which
    caused agents to create duplicate copies in ~/.hermes/skills/ as a
    workaround.
    """

    def test_patch_external_skill_writes_in_place(self, tmp_path):
        local = tmp_path / "local"
        external = tmp_path / "vault"
        local.mkdir(); external.mkdir()
        skill_dir = _write_external_skill(external)

        with _two_roots(local, external):
            result = _patch_skill("ext-skill", "OLD_MARKER", "NEW_MARKER")

        assert result["success"] is True, result
        assert "NEW_MARKER" in (skill_dir / "SKILL.md").read_text(
            encoding="utf-8"
        )
        # No duplicate in local
        assert not (local / "ext-skill").exists()


    def test_background_review_refuses_to_patch_pinned_skill(self, tmp_path):
        """#25839: the autonomous review fork respects pin like the curator
        does — a pinned skill is off-limits to background maintenance, even
        for patch/edit (which a foreground user-directed call is allowed to
        perform). Without a user in the loop there is no one to consent."""
        from tools.skill_provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        def _fake_get_record(skill_name):
            return {"pinned": True} if skill_name == "my-skill" else {"pinned": False}

        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            token = set_current_write_origin(BACKGROUND_REVIEW)
            try:
                with patch("tools.skill_usage.get_record", side_effect=_fake_get_record):
                    raw = skill_manage(
                        action="patch",
                        name="my-skill",
                        old_string="Do the thing.",
                        new_string="Do the new thing.",
                    )
            finally:
                reset_current_write_origin(token)

        result = json.loads(raw)
        assert result["success"] is False
        assert "pinned" in result["error"].lower()


    def test_background_review_fails_closed_when_ownership_lookup_errors(self, tmp_path):
        from tools.skill_provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        with _skill_dir(tmp_path):
            _create_skill("manual-skill", VALID_SKILL_CONTENT)
            token = set_current_write_origin(BACKGROUND_REVIEW)
            try:
                with patch(
                    "tools.skill_usage.load_usage",
                    side_effect=ValueError("corrupt usage data"),
                ):
                    raw = skill_manage(
                        action="patch",
                        name="manual-skill",
                        old_string="Do the thing.",
                        new_string="Changed.",
                    )
            finally:
                reset_current_write_origin(token)

        result = json.loads(raw)
        assert result["success"] is False
        assert "ownership" in result["error"].lower()
        assert "Do the thing." in (
            tmp_path / "manual-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")

class TestBackgroundOwnershipPolicyConsistency:
    """The autonomous write policy must not depend on its own side effects.

    Issue #67140: the ownership guard keyed on ``isinstance(usage_rec, dict)``,
    so a local skill with NO usage record passed. The successful write then
    called ``bump_patch()``, creating a ``created_by: null`` record — and the
    identical write was refused from then on. "Allowed exactly once" is a race
    with our own bookkeeping, not a policy.
    """

    @staticmethod
    def _bg_patch(tmp_path, name, old, new):
        from tools.skill_manager_tool import mark_background_review_skill_read
        from tools.skill_provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            mark_background_review_skill_read(tmp_path / name / "SKILL.md")
            return json.loads(skill_manage(
                action="patch", name=name, old_string=old, new_string=new,
            ))
        finally:
            reset_current_write_origin(token)

    def test_repeated_identical_write_gets_the_same_answer(self, tmp_path, monkeypatch):
        """The real #67140 shape: no stubbing of load_usage, so the first write's
        telemetry side effect is live. Both attempts must agree."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes" / "skills").mkdir(parents=True, exist_ok=True)
        with _skill_dir(tmp_path):
            _create_skill("flip-skill", VALID_SKILL_CONTENT)
            first = self._bg_patch(
                tmp_path, "flip-skill", "Do the thing.", "Do the new thing.",
            )
            second = self._bg_patch(
                tmp_path, "flip-skill", "Do the thing.", "Do the new thing.",
            )

        assert first["success"] == second["success"], (
            "autonomous write policy flipped between two identical attempts: "
            f"first={first.get('success')} second={second.get('success')}"
        )
        assert first["success"] is False

    def test_foreground_write_to_unmanaged_skill_still_allowed(self, tmp_path, monkeypatch):
        """Fail-closed applies to AUTONOMOUS writes only. A user-directed
        foreground edit to their own skill must keep working."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _create_skill("no-record", VALID_SKILL_CONTENT)
            with patch("tools.skill_usage.load_usage", return_value={}):
                res = json.loads(skill_manage(
                    action="patch", name="no-record",
                    old_string="Do the thing.", new_string="Do the new thing.",
                ))
        assert res["success"] is True

    def test_adopted_skill_becomes_writable_by_autonomous_curation(self, tmp_path, monkeypatch):
        """Adoption is the documented path from refused to allowed."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _create_skill("adopt-me", VALID_SKILL_CONTENT)
            with patch("tools.skill_usage.load_usage", return_value={}):
                before = self._bg_patch(
                    tmp_path, "adopt-me", "Do the thing.", "Do the new thing.",
                )
            with patch(
                "tools.skill_usage.load_usage",
                return_value={"adopt-me": {"created_by": "agent"}},
            ), patch(
                "tools.skill_usage.get_record",
                side_effect=lambda n: {"created_by": "agent", "pinned": False},
            ):
                after = self._bg_patch(
                    tmp_path, "adopt-me", "Do the thing.", "Do the new thing.",
                )

        assert before["success"] is False
        assert after["success"] is True, after


# ---------------------------------------------------------------------------
# Pinned-skill guard — skill_manage refuses only `delete` on pinned skills.
# Patches and edits go through so pinned skills can still evolve as pitfalls
# come up. The user unpins via `hermes curator unpin <name>` to delete.
# ---------------------------------------------------------------------------

class TestPinnedGuard:
    """Delete is refused on pinned skills; patch/edit/write_file/remove_file are allowed."""

    @staticmethod
    def _pin(name: str):
        """Return a patch context that marks *name* as pinned in skill_usage."""
        def _fake_get_record(skill_name, _name=name):
            return {"pinned": True} if skill_name == _name else {"pinned": False}
        return patch("tools.skill_usage.get_record", side_effect=_fake_get_record)

    def test_edit_allowed_when_pinned(self, tmp_path):
        """Pin does NOT block edit — agent can still improve pinned skills."""
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            with self._pin("my-skill"):
                result = _edit_skill(
                    "my-skill",
                    _content_for_name(VALID_SKILL_CONTENT_2, "my-skill"),
                )
        assert result["success"] is True, result
        # Content updated
        content = (tmp_path / "my-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "A test skill" not in content

    def test_delete_refuses_pinned(self, tmp_path):
        """Delete is the one action pin still blocks — it's the irrecoverable one."""
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            with self._pin("my-skill"):
                result = _delete_skill("my-skill")
        assert result["success"] is False
        assert "pinned" in result["error"].lower()
        assert "cannot be deleted" in result["error"]
        assert "hermes curator unpin my-skill" in result["error"]
        # Skill still exists
        assert (tmp_path / "my-skill" / "SKILL.md").exists()

    def test_broken_sidecar_fails_open(self, tmp_path):
        """If skill_usage.get_record raises, we allow delete through.

        Rationale: a corrupted telemetry file shouldn't lock the agent out
        of skills it would otherwise be allowed to touch.
        """
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            with patch("tools.skill_usage.get_record",
                       side_effect=RuntimeError("sidecar broken")):
                result = _delete_skill("my-skill")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# _delete_skill — recursive-delete safety (port of Kilo Code #11240)
# ---------------------------------------------------------------------------


class TestDeleteSkillRmtreeGuard:
    """Defense-in-depth before ``shutil.rmtree`` in ``_delete_skill``.

    Mirrors the Kilo Code #11227 fix: never let a recursive skill delete
    escape the skills tree, target a skills root, or follow a symlink.
    """

    def test_normal_delete_still_works(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("good-skill", VALID_SKILL_CONTENT)
            result = _delete_skill("good-skill", absorbed_into="")
        assert result["success"] is True, result
        assert not (tmp_path / "good-skill").exists()

    def test_symlinked_skill_dir_refused(self, tmp_path):
        """A skill dir that is a symlink must not be rmtree'd — rmtree would
        otherwise follow it and delete the link target's contents."""
        victim = tmp_path.parent / "precious_victim"
        victim.mkdir()
        (victim / "important.txt").write_text(
            "DO NOT DELETE", encoding="utf-8"
        )
        skills = tmp_path / "skills"
        skills.mkdir()
        evil = skills / "evil-skill"
        evil.symlink_to(victim, target_is_directory=True)
        try:
            with patch("tools.skill_manager_tool.SKILLS_DIR", skills), \
                 patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills]), \
                 patch("tools.skill_manager_tool._find_skill",
                       return_value={
                           "path": evil,
                           "_resolved_path": victim,
                           "_dir_identity": (victim.stat().st_dev, victim.stat().st_ino),
                       }):
                result = _delete_skill("evil-skill", absorbed_into="")
            assert result["success"] is False
            assert "symlink" in result["error"].lower()
            assert (victim / "important.txt").exists()
        finally:
            import shutil as _sh
            _sh.rmtree(victim, ignore_errors=True)

    def test_skills_root_itself_refused(self, tmp_path):
        """If discovery ever hands back the skills root, refuse — rmtree would
        wipe every installed skill."""
        with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]), \
             patch("tools.skill_manager_tool._find_skill",
                   return_value={
                       "path": tmp_path,
                       "_resolved_path": tmp_path,
                       "_dir_identity": (tmp_path.stat().st_dev, tmp_path.stat().st_ino),
                   }):
            result = _delete_skill(tmp_path.name, absorbed_into="")
        assert result["success"] is False
        assert "skills root" in result["error"].lower()
        assert tmp_path.exists()

    def test_out_of_tree_path_refused(self, tmp_path):
        """A path that resolves outside every known skills root is refused."""
        skills = tmp_path / "skills"
        skills.mkdir()
        outside = tmp_path / "outside_skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text("x", encoding="utf-8")
        with patch("tools.skill_manager_tool.SKILLS_DIR", skills), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills]), \
             patch("tools.skill_manager_tool._find_skill",
                   return_value={
                       "path": outside,
                       "_resolved_path": outside,
                       "_dir_identity": (outside.stat().st_dev, outside.stat().st_ino),
                   }):
            result = _delete_skill("outside_skill", absorbed_into="")
        assert result["success"] is False
        assert "skills root" in result["error"].lower()
        assert outside.exists()


# ---------------------------------------------------------------------------
# Curator consolidation-pass fail-closed delete guard (#29912)
# ---------------------------------------------------------------------------


@contextmanager
def _curator_pass(tmp_path, *, monkeypatch):
    """Run the body as the curator/background-review fork.

    Points HERMES_HOME at ``tmp_path/.hermes`` so skill_usage's archive path
    (``get_hermes_home()``) resolves into the same tree the skill manager
    searches, and flips ``is_background_review()`` → True so the consolidation
    guard fires.

    Also stubs the ownership check to report every skill as curator-managed.
    The ownership guard runs BEFORE the consolidation / read-before-write
    guards these tests target, and since #67140 a skill with no usage record
    fails closed — so without this, every test in this class would be refused
    by ownership and never reach the guard under test. The real curator only
    ever operates on managed sediment, so "managed" is the correct premise
    here; tests that specifically exercise the ownership guard set their own
    records instead.
    """
    hermes_home = tmp_path / ".hermes"
    skills_root = hermes_home / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    with patch("tools.skill_manager_tool.SKILLS_DIR", skills_root), \
         patch("tools.skills_tool.SKILLS_DIR", skills_root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_root]), \
         patch("tools.skill_usage._is_curator_managed_record", return_value=True), \
         patch("tools.skill_provenance.is_background_review", return_value=True):
        yield skills_root


def _skill_content(name: str) -> str:
    """SKILL.md whose frontmatter ``name:`` matches the directory name.

    ``skill_usage._find_skill_dir`` (used by ``archive_skill``) resolves a
    skill by its frontmatter ``name:`` field, so archive-path tests must keep
    the two in sync.
    """
    return (
        "---\n"
        f"name: {name}\n"
        "description: A test skill for unit testing.\n"
        "---\n\n"
        f"# {name}\n\n"
        "Step 1: Do the thing.\n"
    )


class TestSecureCuratorArchive:
    def test_curator_archive_accepts_directory_alias_for_frontmatter_skill(
        self, tmp_path, monkeypatch
    ):
        with _curator_pass(tmp_path, monkeypatch=monkeypatch) as skills_root:
            _create_curator_skill("umbrella", _skill_content("umbrella"))
            legacy = skills_root / "legacy-directory"
            legacy.mkdir()
            (legacy / "SKILL.md").write_text(
                _skill_content("frontmatter-alias"), encoding="utf-8"
            )
            with patch(
                "tools.skill_manager_tool._background_review_write_guard",
                return_value=None,
            ):
                result = _delete_skill(
                    "legacy-directory", absorbed_into="umbrella"
                )

        assert result["success"] is True, result
        assert not legacy.exists()
        assert (skills_root / ".archive" / "legacy-directory" / "SKILL.md").exists()

    def test_curator_delete_retry_reconciles_archived_canonical_alias(
        self, tmp_path, monkeypatch
    ):
        """A curator delete retry handles a committed alias archive exactly once."""
        with _curator_pass(tmp_path, monkeypatch=monkeypatch) as skills_root:
            _create_curator_skill("umbrella", _skill_content("umbrella"))
            legacy = skills_root / "legacy-directory"
            legacy.mkdir()
            (legacy / "SKILL.md").write_text(
                _skill_content("frontmatter-alias"), encoding="utf-8"
            )
            from tools.skill_usage import mark_agent_created, get_record
            import tools.skill_usage as usage

            mark_agent_created("frontmatter-alias")
            real_persist = usage.persist_lifecycle_move_metadata_strict
            monkeypatch.setattr(
                usage,
                "persist_lifecycle_move_metadata_strict",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    OSError("state write failed")
                ),
            )
            with patch(
                "tools.skill_manager_tool._background_review_write_guard",
                return_value=None,
            ):
                first = _delete_skill(
                    "frontmatter-alias", absorbed_into="umbrella"
                )
            assert first["success"] is False
            assert (skills_root / ".archive" / "legacy-directory").is_dir()

            monkeypatch.setattr(
                usage, "persist_lifecycle_move_metadata_strict", real_persist
            )
            with patch(
                "tools.skill_manager_tool._background_review_write_guard",
                return_value=None,
            ):
                retry = _delete_skill(
                    "legacy-directory", absorbed_into="umbrella"
                )

        assert retry["success"] is True, retry
        assert retry["_archived"] is True
        assert get_record("frontmatter-alias")["state"] == "archived"

    def test_curator_archive_refuses_parent_swap_before_rename(self, tmp_path):
        skill_dir = tmp_path / "legacy-directory"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            _skill_content("frontmatter-alias"), encoding="utf-8"
        )
        with _skill_dir(tmp_path):
            existing = skill_manager_module._find_skill("frontmatter-alias")
            assert existing is not None
            real_matches = skill_manager_module._directory_entry_matches_fd
            calls = 0

            def swap_before_final_match(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    skill_dir.rename(tmp_path / "moved-original")
                    replacement = tmp_path / "legacy-directory"
                    replacement.mkdir()
                    (replacement / "SKILL.md").write_text(
                        _skill_content("replacement"), encoding="utf-8"
                    )
                return real_matches(*args)

            with patch(
                "tools.skill_manager_tool._directory_entry_matches_fd",
                side_effect=swap_before_final_match,
            ):
                ok, message = skill_manager_module._secure_archive_existing_skill(
                    existing, tmp_path
                )

        assert ok is False
        assert "changed before archiving" in message
        assert (tmp_path / "moved-original" / "SKILL.md").exists()
        assert (tmp_path / "legacy-directory" / "SKILL.md").exists()
        assert not list((tmp_path / ".archive").iterdir())

    def test_curator_archive_fails_closed_without_secure_platform(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            _skill_content("my-skill"), encoding="utf-8"
        )
        with _skill_dir(tmp_path):
            existing = skill_manager_module._find_skill("my-skill")
            assert existing is not None
            with patch(
                "tools.skill_manager_tool._secure_directory_create_supported",
                return_value=False,
            ), patch("tools.skill_manager_tool.os.name", "nt"):
                ok, message = skill_manager_module._secure_archive_existing_skill(
                    existing, tmp_path
                )

        assert ok is False
        assert "unavailable" in message
        assert skill_dir.exists()


class TestCommittedFsyncFailures:
    @staticmethod
    def _fail_directory_fsync():
        real_fsync = os.fsync

        def fail_directory(fd):
            if os.path.exists(f"/dev/fd/{fd}") and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected post-commit directory fsync failure")
            return real_fsync(fd)

        return fail_directory

    @pytest.mark.parametrize("action", ["edit", "patch", "write"])
    def test_replace_post_fsync_failure_is_not_reported_as_noop(self, tmp_path, action):
        replacement = _content_for_name(VALID_SKILL_CONTENT_2, "my-skill")
        with _skill_dir(tmp_path), patch(
            "tools.skill_manager_tool.os.fsync", side_effect=self._fail_directory_fsync()
        ):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            if action == "edit":
                result = _edit_skill("my-skill", replacement)
            elif action == "patch":
                result = _patch_skill("my-skill", "Do the thing.", "Changed.")
            else:
                result = _write_file("my-skill", "references/state.txt", "changed")
        assert result["success"] is True

    def test_remove_post_unlink_fsync_failure_reports_committed_success(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            _write_file("my-skill", "references/state.txt", "old")
            with patch("tools.skill_manager_tool.os.fsync", side_effect=self._fail_directory_fsync()):
                result = _remove_file("my-skill", "references/state.txt")
        assert result["success"] is True
        assert not (tmp_path / "my-skill" / "references" / "state.txt").exists()

    def test_hard_delete_post_rmdir_fsync_failure_reports_committed_success(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT)
            with patch("tools.skill_manager_tool.os.fsync", side_effect=self._fail_directory_fsync()):
                result = _delete_skill("my-skill")
        assert result["success"] is True
        assert not (tmp_path / "my-skill").exists()

def _create_curator_skill(name: str, content: str):
    """Create a skill and record the agent ownership a real curator create has."""
    from tools.skill_usage import mark_agent_created

    result = _create_skill(name, content)
    assert result["success"] is True, result
    mark_agent_created(name)
    return result


class TestCuratorConsolidationDeleteGuard:
    """The curator's LLM consolidation pass must fail CLOSED on unverified
    deletes — it may only archive a skill it absorbed into an umbrella.

    Reproduces #29912: the pass archived clusters of active skills with zero
    verified consolidations (``consolidated_this_run == 0``) because a bare
    prune from the LLM pass was accepted. With the guard, a delete without a
    valid ``absorbed_into`` is refused and the skill stays active; a verified
    consolidation is archived RECOVERABLY (not rmtree'd).
    """

    def test_bare_prune_during_curator_pass_refused(self, tmp_path, monkeypatch):
        with _curator_pass(tmp_path, monkeypatch=monkeypatch) as skills_root:
            _create_curator_skill("active-skill", VALID_SKILL_CONTENT)
            result = _delete_skill("active-skill", absorbed_into="")
        assert result["success"] is False
        assert result.get("_fail_closed") is True
        # Skill must remain active on disk — fail closed, no archive.
        assert (skills_root / "active-skill").exists()


    def test_background_review_support_file_overwrite_requires_that_file_read(self, tmp_path, monkeypatch):
        from tools.skills_tool import skill_view
        from tools.skill_manager_tool import _reset_background_review_read_marks

        _reset_background_review_read_marks()
        with _curator_pass(tmp_path, monkeypatch=monkeypatch):
            _create_curator_skill("reviewed", _skill_content("reviewed"))
            ref = tmp_path / ".hermes" / "skills" / "reviewed" / "references"
            ref.mkdir()
            (ref / "workflow.md").write_text("old workflow\n", encoding="utf-8")

            # Reading SKILL.md does not authorize overwriting a linked file.
            assert json.loads(skill_view("reviewed"))["success"] is True
            blocked = json.loads(skill_manage(
                action="write_file",
                name="reviewed",
                file_path="references/workflow.md",
                file_content="new workflow\n",
            ))
            assert blocked["success"] is False
            assert blocked.get("_read_before_write_required") is True

            assert json.loads(skill_view("reviewed", "references/workflow.md"))["success"] is True
            allowed = json.loads(skill_manage(
                action="write_file",
                name="reviewed",
                file_path="references/workflow.md",
                file_content="new workflow\n",
            ))
            assert allowed["success"] is True, allowed

        _reset_background_review_read_marks()

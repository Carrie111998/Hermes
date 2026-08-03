"""Real-path regressions for V4A candidate preflight."""

from pathlib import Path

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.patch_parser import apply_v4a_operations, parse_v4a_patch


def _apply(patch: str, root: Path):
    operations, error = parse_v4a_patch(patch)
    assert error is None
    file_ops = ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))
    return apply_v4a_operations(operations, file_ops)


def test_invalid_structured_candidate_blocks_entire_batch(tmp_path: Path):
    good = tmp_path / "good.txt"
    structured = tmp_path / "config.json"
    good.write_text("old\n")
    structured.write_text('{"ok": true}\n')

    result = _apply(
        f"""*** Begin Patch
*** Update File: {good}
@@
-old
+new
*** Update File: {structured}
@@
-{{"ok": true}}
+{{"ok":
*** End Patch
""",
        tmp_path,
    )

    assert result.success is False
    assert "no files were modified" in (result.error or "")
    assert good.read_text() == "old\n"
    assert structured.read_text() == '{"ok": true}\n'


def test_write_policy_denial_blocks_entire_batch(
    tmp_path: Path,
    monkeypatch,
):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    protected = hermes_home / ".env"
    protected.write_text("SECRET=unchanged\n")
    good = tmp_path / "good.txt"
    good.write_text("old\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = _apply(
        f"""*** Begin Patch
*** Update File: {good}
@@
-old
+new
*** Add File: {protected}
+SECRET=overwritten
*** End Patch
""",
        tmp_path,
    )

    assert result.success is False
    assert "no files were modified" in (result.error or "")
    assert "Write denied" in (result.error or "")
    assert good.read_text() == "old\n"
    assert protected.read_text() == "SECRET=unchanged\n"

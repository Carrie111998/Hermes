"""Real-path regressions for V4A candidate preflight."""

from pathlib import Path
from unittest.mock import MagicMock

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


def test_non_string_backend_preflight_result_is_not_a_rejection(tmp_path: Path):
    target = tmp_path / "plain.txt"
    target.write_text("old\n")
    file_ops = MagicMock()
    file_ops.read_file_raw.return_value.content = "old\n"
    file_ops.read_file_raw.return_value.error = None
    file_ops.validate_write_candidate.return_value = True
    file_ops.write_file.return_value.error = None
    file_ops.write_file.return_value.lsp_diagnostics = None

    operations, error = parse_v4a_patch(
        f"""*** Begin Patch
*** Update File: {target}
@@
-old
+new
*** End Patch
"""
    )
    assert error is None

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is True
    file_ops.write_file.assert_called_once_with(str(target), "new\n")


def test_missing_addition_only_hint_rejects_without_appending(tmp_path: Path):
    target = tmp_path / "plain.py"
    target.write_text("x = 1\n")

    result = _apply(
        f"""*** Begin Patch
*** Update File: {target}
@@ missing_anchor @@
+y = 2
*** End Patch
""",
        tmp_path,
    )

    assert result.success is False
    assert "missing_anchor" in (result.error or "")
    assert "not found" in (result.error or "")
    assert target.read_text() == "x = 1\n"

"""Focused regressions for bounded, verified repository mutation."""

import time
from types import SimpleNamespace

from tools.environments.local import LocalEnvironment
from tools.file_operations import (
    FILE_OPERATION_TIMEOUT_SECONDS,
    ShellFileOperations,
)
from tools.patch_parser import apply_v4a_operations, parse_v4a_patch


def test_shell_file_commands_get_a_bounded_default_timeout():
    class RecordingEnv:
        cwd = "/tmp"

        def __init__(self):
            self.kwargs = None

        def execute(self, _command, **kwargs):
            self.kwargs = kwargs
            return {"output": "", "returncode": 0}

    env = RecordingEnv()
    ShellFileOperations(env)._exec("true")

    assert env.kwargs["timeout"] == FILE_OPERATION_TIMEOUT_SECONDS


def test_shell_file_deadline_shortens_an_explicit_command_timeout():
    class RecordingEnv:
        cwd = "/tmp"

        def __init__(self):
            self.kwargs = None

        def execute(self, _command, **kwargs):
            self.kwargs = kwargs
            return {"output": "", "returncode": 0}

    env = RecordingEnv()
    ops = ShellFileOperations(env)
    ops._operation_deadline = time.monotonic() + 0.2

    ops._exec("slow-command", timeout=30)

    assert env.kwargs["timeout"] == 1


def test_write_file_requires_exact_source_readback(tmp_path, monkeypatch):
    target = tmp_path / "proof.txt"
    ops = ShellFileOperations(
        LocalEnvironment(cwd=str(tmp_path)),
        cwd=str(tmp_path),
    )
    monkeypatch.setattr(
        ops,
        "read_file_raw",
        lambda _path: SimpleNamespace(content="stale", error=None),
    )

    result = ops.write_file(str(target), "expected")

    assert result.error
    assert "Post-write verification failed" in result.error
    assert "outcome is unknown" in result.error


class _StatefulFileOps:
    def __init__(self, files, *, fail_path=None, concurrent_edit=None):
        self.files = dict(files)
        self.fail_path = fail_path
        self.concurrent_edit = concurrent_edit

    def read_file_raw(self, path):
        if path not in self.files:
            return SimpleNamespace(content="", error=f"File not found: {path}")
        return SimpleNamespace(content=self.files[path], error=None)

    def write_file(self, path, content):
        if path == self.fail_path:
            if self.concurrent_edit:
                edit_path, edit_content = self.concurrent_edit
                self.files[edit_path] = edit_content
            return SimpleNamespace(error="fault injected")
        self.files[path] = content
        return SimpleNamespace(error=None)

    def delete_file(self, path):
        self.files.pop(path, None)
        return SimpleNamespace(error=None)

    def move_file(self, src, dst):
        self.files[dst] = self.files.pop(src)
        return SimpleNamespace(error=None)


_TWO_FILE_PATCH = """\
*** Begin Patch
*** Update File: a.py
-value = 1
+value = 2
*** Update File: b.py
-value = 10
+value = 20
*** End Patch"""


def test_multi_file_patch_compensates_and_verifies_on_later_failure():
    operations, error = parse_v4a_patch(_TWO_FILE_PATCH)
    assert error is None
    file_ops = _StatefulFileOps(
        {"a.py": "value = 1", "b.py": "value = 10"},
        fail_path="b.py",
    )

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is False
    assert file_ops.files == {"a.py": "value = 1", "b.py": "value = 10"}
    assert "safely compensated" in result.error


def test_compensation_never_overwrites_a_later_edit():
    operations, error = parse_v4a_patch(_TWO_FILE_PATCH)
    assert error is None
    file_ops = _StatefulFileOps(
        {"a.py": "value = 1", "b.py": "value = 10"},
        fail_path="b.py",
        concurrent_edit=("a.py", "value = 99"),
    )

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is False
    assert file_ops.files["a.py"] == "value = 99"
    assert "protected rollback refused" in result.error
    assert "no mismatched state was overwritten" in result.error


def test_unreadable_add_target_fails_closed_with_zero_writes():
    class UnreadableAddOps(_StatefulFileOps):
        def __init__(self):
            super().__init__({})
            self.write_calls = 0

        def read_file_raw(self, _path):
            return SimpleNamespace(content="", error="Permission denied")

        def write_file(self, path, content):
            self.write_calls += 1
            return super().write_file(path, content)

    operations, error = parse_v4a_patch(
        "*** Begin Patch\n"
        "*** Add File: protected.py\n"
        "+value = 1\n"
        "*** End Patch"
    )
    assert error is None
    file_ops = UnreadableAddOps()

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is False
    assert "failed closed" in result.error
    assert file_ops.write_calls == 0


def test_unknown_write_outcome_is_read_back_and_safely_compensated():
    class UnknownOnceOps(_StatefulFileOps):
        def __init__(self):
            super().__init__({"a.py": "value = 1"})
            self.failed_once = False

        def write_file(self, path, content):
            self.files[path] = content
            if not self.failed_once:
                self.failed_once = True
                return SimpleNamespace(
                    error="Post-write verification failed; outcome is unknown"
                )
            return SimpleNamespace(error=None)

    operations, error = parse_v4a_patch(
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "-value = 1\n"
        "+value = 2\n"
        "*** End Patch"
    )
    assert error is None
    file_ops = UnknownOnceOps()

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is False
    assert file_ops.files == {"a.py": "value = 1"}
    assert "safely compensated" in result.error


def test_move_requires_destination_content_to_match_captured_source():
    class CorruptMoveOps(_StatefulFileOps):
        def move_file(self, src, dst):
            self.files.pop(src)
            self.files[dst] = "CORRUPTED"
            return SimpleNamespace(error=None)

    operations, error = parse_v4a_patch(
        "*** Begin Patch\n"
        "*** Move File: source.py -> destination.py\n"
        "*** End Patch"
    )
    assert error is None
    file_ops = CorruptMoveOps({"source.py": "expected source"})

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is False
    assert "exact planned state" in result.error
    assert "no mismatched state was overwritten" in result.error


def test_elapsed_patch_deadline_cancels_remaining_work_and_compensates(
    monkeypatch,
):
    import tools.patch_parser as patch_parser

    class SlowFirstWriteOps(_StatefulFileOps):
        def write_file(self, path, content):
            if path == "a.py" and content == "value = 2":
                time.sleep(0.08)
            return super().write_file(path, content)

    monkeypatch.setattr(patch_parser, "PATCH_APPLY_TIMEOUT_SECONDS", 0.05)
    operations, error = parse_v4a_patch(_TWO_FILE_PATCH)
    assert error is None
    file_ops = SlowFirstWriteOps(
        {"a.py": "value = 1", "b.py": "value = 10"}
    )

    result = apply_v4a_operations(operations, file_ops)

    assert result.success is False
    assert file_ops.files == {"a.py": "value = 1", "b.py": "value = 10"}
    assert "exceeded 0.05s" in result.error

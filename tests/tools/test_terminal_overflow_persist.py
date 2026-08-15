"""Oversized terminal output must be archived before the cap discards it.

Defence against context overflow is layered (see tools/tool_result_storage.py):
layer 1 is the tool's own cap, layer 2 persists a result that exceeds
``DEFAULT_RESULT_SIZE_CHARS`` into the sandbox and replaces it with a preview
plus a path.

For ``terminal`` the two layers never met. Layer 1 caps at
``tool_output_limits.DEFAULT_MAX_BYTES`` (50 000) -- exactly half of layer 2's
100 000 threshold -- so layer 2's ``len(content) > threshold`` test could not
become true, and the bytes it exists to preserve were already dropped by the
head/tail cap before it ran. Overflow was silently unrecoverable.

The cap still applies (context protection is the point), but the full output
is now written to the result store first and the truncation notice carries its
path.
"""

from unittest.mock import MagicMock

from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
from tools.terminal_tool import _truncate_with_overflow_persist
from tools.tool_output_limits import DEFAULT_MAX_BYTES
from tools.tool_result_storage import persist_overflow_output


def _env(returncode=0, temp_dir="/tmp"):
    env = MagicMock()
    env.get_temp_dir.return_value = temp_dir
    env.execute.return_value = {"returncode": returncode}
    return env


def _written(env):
    """The content pushed through stdin by the sandbox write."""
    return env.execute.call_args.kwargs["stdin_data"]


class TestLayerThresholdsNeverMet:
    def test_tool_cap_sits_below_the_persistence_threshold(self):
        """The invariant that made layer 2 unreachable for terminal.

        Kept as an executable statement of the defect: while the tool cap is
        the lower of the two, a terminal result can never reach layer 2, so
        the archive-before-truncate path below is load-bearing rather than a
        redundant safety net.
        """
        assert DEFAULT_MAX_BYTES < DEFAULT_RESULT_SIZE_CHARS


class TestTruncateWithOverflowPersist:
    def test_output_under_cap_is_untouched_and_writes_nothing(self):
        env = _env()
        out = "x" * 100
        assert _truncate_with_overflow_persist(out, 1000, env, "echo hi") == out
        env.execute.assert_not_called()

    def test_output_at_cap_is_untouched(self):
        env = _env()
        out = "x" * 1000
        assert _truncate_with_overflow_persist(out, 1000, env, "echo hi") == out
        env.execute.assert_not_called()

    def test_full_output_including_omitted_middle_is_persisted(self):
        env = _env()
        out = "HEAD" + ("m" * 5000) + "MIDDLE_MARKER" + ("m" * 5000) + "TAIL"
        result = _truncate_with_overflow_persist(out, 1000, env, "big-cmd")

        # The marker is cut from context ...
        assert "MIDDLE_MARKER" not in result
        # ... but survives in the archive.
        assert _written(env) == out
        assert "MIDDLE_MARKER" in _written(env)

    def test_notice_points_at_the_artifact(self):
        env = _env(temp_dir="/sandbox/tmp")
        result = _truncate_with_overflow_persist("y" * 20_000, 1000, env, "big-cmd")
        assert "/sandbox/tmp/hermes-results/" in result
        assert "full output saved to" in result

    def test_cap_still_bounds_context(self):
        env = _env()
        result = _truncate_with_overflow_persist("y" * 500_000, 1000, env, "big-cmd")
        # Head + tail stay at the cap; only the notice is added on top.
        assert len(result) < 1000 + 400

    def test_head_and_tail_are_preserved(self):
        env = _env()
        out = "STARTMARK" + ("m" * 50_000) + "ENDMARK"
        result = _truncate_with_overflow_persist(out, 1000, env, "big-cmd")
        assert result.startswith("STARTMARK")
        assert result.endswith("ENDMARK")

    def test_failed_write_falls_back_to_plain_truncation(self):
        env = _env(returncode=1)
        result = _truncate_with_overflow_persist("y" * 20_000, 1000, env, "big-cmd")
        assert "OUTPUT TRUNCATED" in result
        assert "full output saved to" not in result

    def test_no_env_falls_back_to_plain_truncation(self):
        result = _truncate_with_overflow_persist("y" * 20_000, 1000, None, "big-cmd")
        assert "OUTPUT TRUNCATED" in result
        assert "full output saved to" not in result

    def test_raising_env_does_not_break_the_tool_call(self):
        env = _env()
        env.execute.side_effect = OSError("sandbox gone")
        result = _truncate_with_overflow_persist("y" * 20_000, 1000, env, "big-cmd")
        assert "OUTPUT TRUNCATED" in result

    def test_command_text_never_reaches_the_filename(self):
        """The command is user/model-controlled; it is hashed, not embedded."""
        env = _env()
        _truncate_with_overflow_persist(
            "y" * 20_000, 1000, env, "cat ../../etc/passwd; rm -rf /"
        )
        path = env.execute.call_args.args[0]
        assert "passwd" not in path
        assert ".." not in path
        assert "rm -rf" not in path

    def test_same_command_is_stable_across_runs(self):
        env1, env2 = _env(), _env()
        _truncate_with_overflow_persist("y" * 20_000, 1000, env1, "make build")
        _truncate_with_overflow_persist("z" * 20_000, 1000, env2, "make build")
        assert env1.execute.call_args.args[0] == env2.execute.call_args.args[0]


class TestPersistOverflowOutput:
    def test_returns_path_on_success(self):
        env = _env(temp_dir="/sandbox/tmp")
        path = persist_overflow_output("payload", "terminal_abc", env)
        assert path == "/sandbox/tmp/hermes-results/terminal_abc.txt"

    def test_returns_none_without_env(self):
        assert persist_overflow_output("payload", "terminal_abc", None) is None

    def test_returns_none_for_empty_content(self):
        env = _env()
        assert persist_overflow_output("", "terminal_abc", env) is None
        env.execute.assert_not_called()

    def test_returns_none_when_write_fails(self):
        assert persist_overflow_output("payload", "terminal_abc", _env(returncode=1)) is None

    def test_swallows_env_exceptions(self):
        env = _env()
        env.execute.side_effect = RuntimeError("backend down")
        assert persist_overflow_output("payload", "terminal_abc", env) is None

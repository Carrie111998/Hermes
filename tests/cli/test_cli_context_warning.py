"""Tests for the low context length warning in the CLI banner."""

import os
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.model_metadata import MINIMUM_CONTEXT_LENGTH


@pytest.fixture
def _isolate(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so tests don't touch real config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


@pytest.fixture
def cli_obj(_isolate):
    """Create a minimal HermesCLI instance for banner testing."""
    with patch("cli.load_cli_config", return_value={
        "display": {"tool_progress": "new"},
        "terminal": {},
    }), patch("cli.get_tool_definitions", return_value=[]), \
         patch("cli.build_welcome_banner"):
        from cli import HermesCLI
        obj = HermesCLI.__new__(HermesCLI)
        obj.model = "test-model"
        obj.enabled_toolsets = ["hermes-core"]
        obj.disabled_toolsets = []
        obj.compact = False
        obj.console = MagicMock()
        obj.session_id = None
        obj.api_key = "test"
        obj.base_url = ""
        obj.provider = "test"
        obj._provider_source = None
        obj._show_tool_availability_warnings = MagicMock()
        # Mock agent with context compressor
        obj.agent = SimpleNamespace(
            context_compressor=SimpleNamespace(context_length=None)
        )
        return obj


class TestLowContextWarning:
    """Tests that the CLI warns about low context lengths."""

    def test_warning_for_below_minimum_context(self, cli_obj):
        """Warning shown when context is below Hermes' minimum."""
        cli_obj.agent.context_compressor.context_length = 32768
        with patch("cli.get_tool_definitions", return_value=[]), \
             patch("cli.build_welcome_banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        warning_calls = [c for c in calls if "too low" in c]
        assert len(warning_calls) == 1
        minimum_calls = [c for c in calls if f"{MINIMUM_CONTEXT_LENGTH:,}" in c]
        assert minimum_calls


    def test_warning_for_2048_context(self, cli_obj):
        """Warning shown for 2048 tokens (common LM Studio default)."""
        cli_obj.agent.context_compressor.context_length = 2048
        with patch("cli.get_tool_definitions", return_value=[]), \
             patch("cli.build_welcome_banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        warning_calls = [c for c in calls if "too low" in c]
        assert len(warning_calls) == 1

    def test_no_warning_at_boundary(self, cli_obj):
        """No warning at exactly Hermes' minimum context length."""
        cli_obj.agent.context_compressor.context_length = MINIMUM_CONTEXT_LENGTH
        with patch("cli.get_tool_definitions", return_value=[]), \
             patch("cli.build_welcome_banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        warning_calls = [c for c in calls if "too low" in c]
        assert len(warning_calls) == 0

    def test_no_warning_above_boundary(self, cli_obj):
        """No warning above Hermes' minimum context length."""
        cli_obj.agent.context_compressor.context_length = MINIMUM_CONTEXT_LENGTH + 1
        with patch("cli.get_tool_definitions", return_value=[]), \
             patch("cli.build_welcome_banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        warning_calls = [c for c in calls if "too low" in c]
        assert len(warning_calls) == 0

    def test_ollama_specific_hint(self, cli_obj):
        """Ollama-specific fix shown when port 11434 detected."""
        cli_obj.agent.context_compressor.context_length = 4096
        cli_obj.base_url = "http://localhost:11434/v1"
        with patch("cli.get_tool_definitions", return_value=[]), \
             patch("cli.build_welcome_banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        ollama_hints = [c for c in calls if "OLLAMA_CONTEXT_LENGTH" in c]
        assert len(ollama_hints) == 1
        assert str(MINIMUM_CONTEXT_LENGTH) in ollama_hints[0]


    def test_generic_hint_for_other_servers(self, cli_obj):
        """Generic fix shown for unknown servers."""
        cli_obj.agent.context_compressor.context_length = 4096
        cli_obj.base_url = "http://localhost:8080/v1"
        with patch("cli.get_tool_definitions", return_value=[]), \
             patch("cli.build_welcome_banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        generic_hints = [c for c in calls if "config.yaml" in c]
        assert len(generic_hints) == 1


    def test_compact_banner_does_not_crash_on_narrow_terminal(self, cli_obj):
        """Compact mode should still have ctx_len defined for warning logic."""
        cli_obj.agent.context_compressor.context_length = 4096

        with patch("shutil.get_terminal_size", return_value=os.terminal_size((70, 40))), \
             patch("cli._build_compact_banner", return_value="compact banner"):
            cli_obj.show_banner()

        calls = [str(c) for c in cli_obj.console.print.call_args_list]
        warning_calls = [c for c in calls if "too low" in c]
        assert len(warning_calls) == 1


def test_full_banner_waits_then_draws_exact_surface(cli_obj):
    entered = threading.Event()
    release = threading.Event()
    expected = [{"function": {"name": "read_file"}}]

    def resolve(**_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return expected

    errors = []

    def show():
        try:
            cli_obj.show_banner()
        except Exception as exc:
            errors.append(exc)

    with (
        patch(
            "hermes_cli.tool_resolution.get_cli_tool_definitions",
            side_effect=resolve,
        ),
        patch("cli.build_welcome_banner") as build_banner,
    ):
        render_thread = threading.Thread(target=show)
        render_thread.start()
        try:
            assert entered.wait(timeout=2)
            build_banner.assert_not_called()
        finally:
            release.set()
        render_thread.join(timeout=2)

    assert not render_thread.is_alive()
    assert errors == []
    assert build_banner.call_args.kwargs["tools"] == expected
    assert cli_obj._startup_tool_resolution[1].result(timeout=2) == expected
    cli_obj._show_tool_availability_warnings.assert_called_once_with()


def test_full_banner_retries_a_failed_prefetch(cli_obj):
    from hermes_cli.tool_resolution import ToolResolutionRequest

    failed = Future()
    failed.set_exception(RuntimeError("transient failure"))
    cli_obj._startup_tool_resolution = (
        ToolResolutionRequest.from_lists(
            cli_obj.enabled_toolsets, cli_obj.disabled_toolsets
        ),
        failed,
    )
    expected = [{"function": {"name": "read_file"}}]

    with (
        patch(
            "hermes_cli.tool_resolution.get_cli_tool_definitions",
            return_value=expected,
        ),
        patch("cli.build_welcome_banner") as build_banner,
    ):
        cli_obj.show_banner()

    assert cli_obj._startup_tool_resolution[1] is not failed
    assert (
        cli_obj._diagnosed_tool_resolution
        is cli_obj._startup_tool_resolution[1]
    )
    assert build_banner.call_args.kwargs["tools"] == expected


def test_full_banner_fails_closed_when_retry_also_fails(cli_obj):
    from hermes_cli.tool_resolution import ToolResolutionRequest

    failed = Future()
    failed.set_exception(RuntimeError("transient failure"))
    cli_obj._startup_tool_resolution = (
        ToolResolutionRequest.from_lists(
            cli_obj.enabled_toolsets, cli_obj.disabled_toolsets
        ),
        failed,
    )

    with (
        patch(
            "hermes_cli.tool_resolution.get_cli_tool_definitions",
            side_effect=RuntimeError("persistent failure"),
        ),
        patch("cli.build_welcome_banner") as build_banner,
    ):
        cli_obj.show_banner()

    assert cli_obj.enabled_toolsets == []
    assert cli_obj._startup_tool_resolution[1].result(timeout=0) == []
    assert build_banner.call_args.kwargs["tools"] == []
    assert any(
        "zero model-callable tools" in str(call)
        for call in cli_obj.console.print.call_args_list
    )


def test_full_banner_fails_closed_when_resolution_times_out(cli_obj):
    from hermes_cli.tool_resolution import ToolResolutionRequest

    pending = Future()
    cli_obj._startup_tool_resolution = (
        ToolResolutionRequest.from_lists(
            cli_obj.enabled_toolsets, cli_obj.disabled_toolsets
        ),
        pending,
    )

    with (
        patch.object(pending, "result", side_effect=TimeoutError) as result,
        patch(
            "hermes_cli.tool_resolution.get_cli_tool_definitions",
            side_effect=AssertionError("a timed-out resolver must not be duplicated"),
        ),
        patch("cli.build_welcome_banner") as build_banner,
    ):
        cli_obj.show_banner()

    assert cli_obj.enabled_toolsets == []
    result.assert_called_once_with(timeout=30.0)
    assert cli_obj._startup_tool_resolution[1].result(timeout=0) == []
    assert build_banner.call_args.kwargs["tools"] == []


def test_full_banner_knows_explicit_empty_without_starting_discovery(cli_obj):
    cli_obj.enabled_toolsets = []
    with (
        patch(
            "hermes_cli.tool_resolution.get_cli_tool_definitions",
            side_effect=AssertionError("explicit empty must not discover"),
        ),
        patch("cli.build_welcome_banner") as build_banner,
    ):
        cli_obj.show_banner()

    assert build_banner.call_args.kwargs["tools"] == []


@pytest.mark.parametrize("selection", [[], (), set()])
def test_availability_diagnostics_skip_every_explicit_empty(cli_obj, selection):
    cli_obj.enabled_toolsets = selection

    with patch("builtins.__import__", wraps=__import__) as import_module:
        from cli import HermesCLI

        HermesCLI._show_tool_availability_warnings(cli_obj)

    assert not any(call.args[0] == "model_tools" for call in import_module.call_args_list)


def test_availability_diagnostics_exclude_disabled_toolsets(cli_obj):
    cli_obj.enabled_toolsets = ["web", "tts"]
    cli_obj.disabled_toolsets = ["tts"]

    with patch("model_tools.check_tool_availability", return_value=([], [])) as check:
        # The fixture replaces this method to keep banner tests isolated.
        from cli import HermesCLI

        HermesCLI._show_tool_availability_warnings(cli_obj)

    check.assert_called_once_with(toolsets={"web"})


def test_availability_diagnostics_expand_composite_toolsets(cli_obj):
    cli_obj.enabled_toolsets = ["hermes-cli"]
    cli_obj.disabled_toolsets = ["tts"]

    with patch("model_tools.check_tool_availability", return_value=([], [])) as check:
        from cli import HermesCLI

        HermesCLI._show_tool_availability_warnings(cli_obj)

    selected = check.call_args.kwargs["toolsets"]
    assert "web" in selected
    assert "tts" not in selected
    assert "hermes-cli" not in selected


def test_main_preserves_default_policy_from_prefetched_request():
    from cli import CLI_CONFIG, main
    from hermes_cli.tool_resolution import ToolResolutionRequest

    future = Future()
    future.set_result([])
    request = ToolResolutionRequest.from_lists(None, [])
    cli_instance = MagicMock()

    with (
        patch("cli.HermesCLI", return_value=cli_instance) as cli_class,
        patch(
            "hermes_cli.tool_resolution.resolve_cli_toolsets",
            return_value=["web"],
        ) as resolve,
        pytest.raises(SystemExit) as exit_info,
    ):
        main(
            list_tools=True,
            _prefetched_tool_resolution=(request, future),
        )

    assert exit_info.value.code == 0
    resolve.assert_called_once_with(None, CLI_CONFIG)
    assert cli_class.call_args.kwargs["toolsets"] == ["web"]


def test_main_fails_closed_when_one_shot_prefetch_times_out():
    from cli import main
    from hermes_cli.tool_resolution import ToolResolutionRequest

    request = ToolResolutionRequest.from_lists(["web"], [])
    pending = Future()
    cli_instance = MagicMock()

    with (
        patch("cli.HermesCLI", return_value=cli_instance) as cli_class,
        patch.object(pending, "result", side_effect=TimeoutError) as result,
    ):
        main(
            query="test query",
            _prefetched_tool_resolution=(request, pending),
        )

    result.assert_called_once_with(timeout=30.0)
    assert cli_class.call_args.kwargs["toolsets"] == []

"""HF-04 Layer A: the real production caller (IGN-252 blocker #3).

Layer A was inert end-to-end before this: nothing in production set
``frontier_required=True``, so the flag could be on and no downgrade would ever
be detected. The board's decision (option "both") was to derive the requirement
from an explicitly-named frontier model on the oneshot / cron / delegated worker
path, plus a literal ``HERMES_FRONTIER_REQUIRED`` override.

Nothing here stubs the classifier — these tests drive
``resolve_frontier_required`` against the committed
``agent.model_classification`` sets, for the same reason
``tests/agent/test_frontier_check_real_classifier.py`` does: a stub would make
the whole feature pass on a tree where the classifier is missing.

The two surfacing tests cover the thing that makes this wiring meaningful at
all: ``run_oneshot`` calls ``logging.disable(logging.CRITICAL)`` for the entire
run, so the guard's own log line reaches nobody on this path. The warning has to
come out of the usage report and stderr or it does not exist.
"""

import json
from unittest.mock import patch

import pytest

from agent import frontier_guard as fg
from agent import model_classification as mc
from hermes_cli import oneshot
from hermes_cli.oneshot import _write_usage_file, run_oneshot


# Real members of the committed sets, asserted as such rather than assumed.
REAL_FRONTIER = "claude-opus-5"
REAL_CHEAP = "gemini-3.6-flash"


def test_fixture_models_are_really_in_the_committed_sets():
    assert REAL_FRONTIER in mc.FRONTIER_MODELS
    assert REAL_CHEAP in mc.CHEAP_MODELS
    assert mc.model_class(REAL_FRONTIER) == "frontier"
    assert mc.model_class(REAL_CHEAP) != "frontier"


class TestResolveFrontierRequired:
    """Derivation + override precedence, against the real classifier."""

    def test_explicit_frontier_model_asserts_the_requirement(self, monkeypatch):
        monkeypatch.delenv(fg.FRONTIER_REQUIRED_ENV, raising=False)
        assert fg.resolve_frontier_required(REAL_FRONTIER, explicitly_requested=True) is True

    def test_explicit_cheap_model_does_not(self, monkeypatch):
        monkeypatch.delenv(fg.FRONTIER_REQUIRED_ENV, raising=False)
        assert fg.resolve_frontier_required(REAL_CHEAP, explicitly_requested=True) is False

    def test_config_default_is_not_an_assertion(self, monkeypatch):
        # Same frontier model, but the caller never named it — that is
        # "use my defaults", not a per-call promise.
        monkeypatch.delenv(fg.FRONTIER_REQUIRED_ENV, raising=False)
        assert fg.resolve_frontier_required(REAL_FRONTIER, explicitly_requested=False) is False

    def test_unknown_model_does_not_assert(self, monkeypatch):
        monkeypatch.delenv(fg.FRONTIER_REQUIRED_ENV, raising=False)
        assert mc.model_class("some-local-gguf") == "unknown"
        assert fg.resolve_frontier_required("some-local-gguf", explicitly_requested=True) is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_override_asserts_even_for_a_cheap_or_implicit_model(self, monkeypatch, raw):
        monkeypatch.setenv(fg.FRONTIER_REQUIRED_ENV, raw)
        assert fg.resolve_frontier_required(REAL_CHEAP, explicitly_requested=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_override_suppresses_an_otherwise_derived_requirement(self, monkeypatch, raw):
        monkeypatch.setenv(fg.FRONTIER_REQUIRED_ENV, raw)
        assert fg.resolve_frontier_required(REAL_FRONTIER, explicitly_requested=True) is False

    def test_unrecognised_override_is_ignored_and_warned_not_guessed(self, monkeypatch, caplog):
        monkeypatch.setenv(fg.FRONTIER_REQUIRED_ENV, "maybe")
        with caplog.at_level("WARNING", logger="agent.frontier_guard"):
            assert fg.frontier_required_override() is None
            # Falls through to derivation rather than silently picking a side.
            assert fg.resolve_frontier_required(REAL_FRONTIER, explicitly_requested=True) is True
        assert "not a recognised boolean" in caplog.text

    def test_blank_override_means_derive(self, monkeypatch):
        monkeypatch.setenv(fg.FRONTIER_REQUIRED_ENV, "   ")
        assert fg.frontier_required_override() is None
        assert fg.resolve_frontier_required(REAL_CHEAP, explicitly_requested=True) is False


class TestUsageReportSurfacing:
    def test_warnings_are_carried_into_the_usage_report(self, tmp_path):
        path = tmp_path / "usage.json"
        warning = {
            "type": "frontier_downgrade",
            "requested_model": REAL_FRONTIER,
            "served_model": REAL_CHEAP,
            "requested_class": "frontier",
            "served_class": "benchmarked_cheap",
        }
        _write_usage_file(str(path), {"warnings": [warning]})
        report = json.loads(path.read_text())
        assert report["warnings"] == [warning]

    def test_no_warnings_key_when_the_turn_was_clean(self, tmp_path):
        path = tmp_path / "usage.json"
        _write_usage_file(str(path), {"model": REAL_FRONTIER})
        assert "warnings" not in json.loads(path.read_text())

    def test_empty_warnings_list_is_not_reported(self, tmp_path):
        path = tmp_path / "usage.json"
        _write_usage_file(str(path), {"warnings": []})
        assert "warnings" not in json.loads(path.read_text())


class _FakeAgent:
    """Stands in for AIAgent — records what the caller asserted."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.model = kwargs.get("model")
        self.run_conversation_kwargs = None
        self._session_messages = []

    def run_conversation(self, prompt, **kwargs):
        self.run_conversation_kwargs = kwargs
        return {"final_response": "ok"}

    def shutdown_memory_provider(self, *args, **kwargs):
        pass

    def close(self):
        pass


class TestOneshotWiresTheFlag:
    """The wiring itself: ``_run_agent`` must reach ``run_conversation``.

    This is the test that would have caught the original defect — Layer A
    existed but no production caller ever set ``frontier_required``.
    """

    @pytest.fixture
    def built(self, monkeypatch):
        import hermes_cli.config as config_mod
        import hermes_cli.fallback_config as fallback_mod
        import hermes_cli.mcp_startup as mcp_mod
        import hermes_cli.models as models_mod
        import hermes_cli.runtime_provider as runtime_mod
        import hermes_cli.tools_config as tools_mod
        import run_agent as run_agent_mod

        agents = []

        def _factory(**kwargs):
            agent = _FakeAgent(**kwargs)
            agents.append(agent)
            return agent

        monkeypatch.setattr(
            config_mod, "load_config", lambda *a, **k: {"model": {"default": REAL_CHEAP}}
        )
        monkeypatch.setattr(models_mod, "detect_provider_for_model", lambda *a, **k: None)
        monkeypatch.setattr(
            runtime_mod, "resolve_runtime_provider", lambda **k: {"provider": "openrouter"}
        )
        monkeypatch.setattr(tools_mod, "_get_platform_tools", lambda *a, **k: set())
        monkeypatch.setattr(fallback_mod, "get_fallback_chain", lambda *a, **k: [])
        monkeypatch.setattr(oneshot, "get_fallback_chain", lambda *a, **k: [])
        monkeypatch.setattr(
            mcp_mod, "ensure_mcp_discovery_before_agent_build", lambda *a, **k: None
        )
        monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
        monkeypatch.setattr(run_agent_mod, "AIAgent", _factory)
        monkeypatch.delenv(fg.FRONTIER_REQUIRED_ENV, raising=False)
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        return agents

    def test_explicit_frontier_model_makes_layer_a_live(self, built):
        oneshot._run_agent("hi", model=REAL_FRONTIER)
        agent = built[0]
        assert agent.run_conversation_kwargs == {"frontier_required": True}
        # Stamped so the guard compares against what the caller asked for, not a
        # primary-runtime snapshot that may already reflect a startup fallback.
        assert agent._frontier_requested_model == REAL_FRONTIER

    def test_env_named_frontier_model_also_counts_as_explicit(self, built, monkeypatch):
        monkeypatch.setenv("HERMES_INFERENCE_MODEL", REAL_FRONTIER)
        oneshot._run_agent("hi")
        assert built[0].run_conversation_kwargs == {"frontier_required": True}

    def test_configured_default_does_not_assert(self, built):
        oneshot._run_agent("hi")
        agent = built[0]
        assert agent.run_conversation_kwargs == {"frontier_required": False}
        assert not hasattr(agent, "_frontier_requested_model")

    def test_explicit_cheap_model_does_not_assert(self, built):
        oneshot._run_agent("hi", model=REAL_CHEAP)
        assert built[0].run_conversation_kwargs == {"frontier_required": False}

    def test_literal_override_asserts_without_an_explicit_model(self, built, monkeypatch):
        monkeypatch.setenv(fg.FRONTIER_REQUIRED_ENV, "1")
        oneshot._run_agent("hi")
        agent = built[0]
        assert agent.run_conversation_kwargs == {"frontier_required": True}
        assert agent._frontier_requested_model == REAL_CHEAP

    def test_literal_override_can_suppress(self, built, monkeypatch):
        monkeypatch.setenv(fg.FRONTIER_REQUIRED_ENV, "off")
        oneshot._run_agent("hi", model=REAL_FRONTIER)
        assert built[0].run_conversation_kwargs == {"frontier_required": False}


class TestStderrSurfacing:
    """``hermes -z`` must print the finding where a cron job will see it."""

    def _run(self, capsys, result):
        with patch.object(oneshot, "_run_agent", return_value=("done", result)):
            code = run_oneshot("hi")
        return code, capsys.readouterr()

    def test_downgrade_goes_to_stderr_and_leaves_stdout_untouched(self, capsys):
        code, captured = self._run(
            capsys,
            {
                "final_response": "done",
                "warnings": [
                    {
                        "type": "frontier_downgrade",
                        "requested_model": REAL_FRONTIER,
                        "served_model": REAL_CHEAP,
                    }
                ],
            },
        )
        assert code == 0
        assert "frontier guarantee not held" in captured.err
        assert REAL_FRONTIER in captured.err and REAL_CHEAP in captured.err
        # Piped consumers of the final response must be byte-identical.
        assert captured.out == "done\n"

    def test_unavailable_says_unverified_not_violated(self, capsys):
        _, captured = self._run(
            capsys,
            {
                "final_response": "done",
                "warnings": [
                    {
                        "type": "frontier_check_unavailable",
                        "requested_model": REAL_FRONTIER,
                        "served_model": REAL_FRONTIER,
                        "detail": "shared model classifier is not importable",
                    }
                ],
            },
        )
        assert "frontier guarantee unverified" in captured.err
        assert "not held" not in captured.err

    def test_clean_turn_prints_nothing_extra(self, capsys):
        _, captured = self._run(capsys, {"final_response": "done"})
        assert captured.err == ""

    def test_non_dict_warning_entries_do_not_break_the_run(self, capsys):
        code, captured = self._run(
            capsys, {"final_response": "done", "warnings": ["oops", None]}
        )
        assert code == 0
        assert captured.err == ""

"""HF-04 Layer A: the frontier check against the *real* committed classifier.

IGN-252. The pre-existing Layer A suites
(``test_frontier_downgrade_guard.py``, ``test_api_server_frontier_check.py``)
install a stand-in for ``agent.model_classification.model_class`` so they can
test the guard's and the adapter's wiring against fixed model names that will
not churn as the frontier list moves. That is a legitimate thing to want, but on
its own it left a hole big enough to hide the whole feature in: IGN-195's
classifier was never committed, and because every test that touched it supplied
its own stub, all of them passed on a clean checkout where
``_resolve_model_class()`` returned ``None`` and no frontier-required call was
ever actually checked.

This module closes that hole. **Nothing here stubs the classifier.** These tests
resolve, import and run the module that production resolves, so:

* if ``agent/model_classification.py`` is missing from the tree (the IGN-252
  blocker #1 state), :func:`test_guard_resolves_the_committed_classifier` fails
  instead of quietly skipping;
* if the frontier/cheap membership changes, the behavioural tests below change
  with it, because they name real models from the real sets.

It also covers three cases the stubbed suites never reached: the cron/delegated
turn path through ``finalize_turn``, the guarantee that one call cannot produce
two warnings, and the distinct log lines for ``frontier_downgrade`` versus
``frontier_check_unavailable``.
"""

import inspect
import logging
from types import SimpleNamespace

import pytest

from agent import frontier_guard as fg
from agent import model_classification as mc
from agent.turn_finalizer import finalize_turn


# Real members of the committed sets, asserted as such below rather than
# assumed, so that a membership change fails loudly here instead of turning
# these tests into vacuous no-ops.
REAL_FRONTIER = "claude-opus-5"
REAL_FRONTIER_ALT = "gpt-5.6-sol"
REAL_CHEAP = "gemini-3.6-flash"
NOT_IN_ANY_SET = "some-model-nobody-benchmarked"


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "1")


@pytest.fixture(autouse=True)
def _reset_latch(monkeypatch):
    monkeypatch.setattr(fg, "_classifier_missing_reported", False)


@pytest.fixture(autouse=True)
def _fixtures_are_real_members():
    """Guard the guards: these names must really be in the committed sets."""
    assert REAL_FRONTIER in mc.FRONTIER_MODELS
    assert REAL_FRONTIER_ALT in mc.FRONTIER_MODELS
    assert REAL_CHEAP in mc.CHEAP_MODELS
    assert NOT_IN_ANY_SET not in mc.FRONTIER_MODELS | mc.CHEAP_MODELS


# --- the classifier is actually reachable from the guard ----------------------


def test_guard_resolves_the_committed_classifier():
    """The regression test for IGN-252 blocker #1.

    ``frontier_guard`` resolves the classifier by import over a candidate list.
    If ``agent/model_classification.py`` is not in the tree, that resolution
    returns ``None``, every frontier-required call degrades to
    ``frontier_check_unavailable``, and Layer A detects nothing. No stub here:
    this asserts the shipped module is what production will find.
    """
    resolved = fg._resolve_model_class()
    assert resolved is not None, (
        "agent.model_classification is not importable — the frontier check "
        "cannot run at all in this checkout"
    )
    assert resolved is mc.model_class


def test_first_import_candidate_is_the_committed_module():
    """The preferred candidate must be the one that actually exists.

    The candidate list exists so the guard survived A1 landing the classifier
    somewhere else. It did not: the first entry is the real home, and resolution
    must not be relying on a later fallback entry.
    """
    assert fg._MODEL_CLASS_IMPORT_CANDIDATES[0] == (
        "agent.model_classification",
        "model_class",
    )


def test_classify_model_returns_the_real_vocabulary(flag_on):
    """The real classifier says 'benchmarked_cheap', never bare 'cheap'.

    The stubbed suite in ``test_frontier_downgrade_guard.py`` returns 'cheap'
    from its stand-in, so nothing there would catch a caller that compared
    against the wrong literal.
    """
    assert fg.classify_model(REAL_FRONTIER) == "frontier"
    assert fg.classify_model(REAL_CHEAP) == "benchmarked_cheap"
    assert fg.classify_model(NOT_IN_ANY_SET) == "unknown"


# --- guard behaviour, real classifier ----------------------------------------


def test_real_downgrade_is_detected(flag_on):
    result = {}
    warning = fg.check_frontier_downgrade(
        result,
        requested_model=REAL_FRONTIER,
        served_model=REAL_CHEAP,
        frontier_required=True,
    )
    assert warning["type"] == "frontier_downgrade"
    assert warning["requested_class"] == "frontier"
    assert warning["served_class"] == "benchmarked_cheap"
    assert result["warnings"] == [warning]


def test_real_in_class_fallback_is_clean(flag_on):
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model=REAL_FRONTIER,
            served_model=REAL_FRONTIER_ALT,
            frontier_required=True,
        )
        is None
    )
    assert "warnings" not in result


def test_unclassified_served_model_counts_as_a_downgrade(flag_on):
    """'unknown' is not a pass.

    A model nobody has benchmarked cannot discharge a frontier promise; the
    whole point of explicit membership is that an unrecognised name fails the
    check rather than sliding through it.
    """
    result = {}
    warning = fg.check_frontier_downgrade(
        result,
        requested_model=REAL_FRONTIER,
        served_model=NOT_IN_ANY_SET,
        frontier_required=True,
    )
    assert warning["type"] == "frontier_downgrade"
    assert warning["served_class"] == "unknown"


def test_provider_qualified_real_names_are_stripped_before_classifying(flag_on):
    """``anthropic/claude-opus-5`` must not read as an unknown-model downgrade."""
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model=f"anthropic/{REAL_FRONTIER}",
            served_model=f"openrouter/{REAL_FRONTIER_ALT}",
            frontier_required=True,
        )
        is None
    )


def test_real_cheap_request_cannot_be_downgraded(flag_on):
    """Nothing was promised, so nothing can be broken."""
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model=REAL_CHEAP,
            served_model=NOT_IN_ANY_SET,
            frontier_required=True,
        )
        is None
    )


# --- log lines: the two findings must not share a sentence -------------------


def test_downgrade_logs_the_guarantee_not_held_line(flag_on, caplog):
    with caplog.at_level(logging.WARNING, logger=fg.__name__):
        fg.check_frontier_downgrade(
            {},
            requested_model=REAL_FRONTIER,
            served_model=REAL_CHEAP,
            frontier_required=True,
        )
    assert "Frontier guarantee not held" in caplog.text


def test_unavailable_does_not_claim_the_guarantee_was_broken(
    monkeypatch, flag_on, caplog
):
    """IGN-252: 'unavailable' is *unverified*, not violated.

    Logging the downgrade sentence on this path sends whoever reads the
    transcript hunting a downgrade that may never have happened, and hides the
    thing that does need fixing — that the check itself did not run.
    """
    monkeypatch.setattr(fg, "_resolve_model_class", lambda: None)
    with caplog.at_level(logging.WARNING, logger=fg.__name__):
        warning = fg.check_frontier_downgrade(
            {},
            requested_model=REAL_FRONTIER,
            served_model=REAL_CHEAP,
            frontier_required=True,
        )
    assert warning["type"] == "frontier_check_unavailable"
    assert "Frontier guarantee not held" not in caplog.text
    assert "unverified" in caplog.text


# --- cron / delegated turn path, driven through finalize_turn ----------------


class _TurnAgent:
    """Minimal AIAgent stand-in able to reach the end of ``finalize_turn``.

    Shaped after ``tests/agent/test_turn_finalizer_iteration_limit_exit.py``'s
    ``_LimitAgent``; the budget is left unexhausted so the turn takes the plain
    success path and the frontier check is what the assertions are about.
    """

    def __init__(self, *, served_model, primary_runtime=None):
        self.model = served_model
        self.max_iterations = 60
        self.iteration_budget = SimpleNamespace(remaining=59, used=1, max_total=60)
        self.quiet_mode = True
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-frontier"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        if primary_runtime is not None:
            self._primary_runtime = primary_runtime

    def _emit_status(self, *_a, **_kw):
        pass

    def _safe_print(self, *_a, **_kw):
        pass

    def _save_trajectory(self, *_a, **_kw):
        pass

    def _cleanup_task_resources(self, *_a, **_kw):
        pass

    def _drop_trailing_empty_response_scaffolding(self, _messages):
        pass

    def _persist_session(self, _messages, _history):
        pass

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kw):
        pass

    def _handle_max_iterations(self, _messages, _api_call_count):
        return None


def _finalize(agent, *, frontier_required):
    return finalize_turn(
        agent,
        final_response="done",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="completed",
        frontier_required=frontier_required,
    )


@pytest.fixture
def _no_plugin_hooks(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])


def test_cron_turn_that_fell_back_off_frontier_warns(
    flag_on, _no_plugin_hooks
):
    """The cron/delegated case Layer A exists for.

    ``try_activate_fallback()`` mutates ``agent.model`` in place mid-loop, so
    the requested model survives only in the primary-runtime snapshot taken at
    agent init. Turn end is where the retry loop has stopped and the served
    model is final.
    """
    agent = _TurnAgent(
        served_model=REAL_CHEAP, primary_runtime={"model": REAL_FRONTIER}
    )
    result = _finalize(agent, frontier_required=True)

    assert result["final_response"] == "done"
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["type"] == "frontier_downgrade"
    assert warnings[0]["requested_model"] == REAL_FRONTIER
    assert warnings[0]["served_model"] == REAL_CHEAP


def test_cron_turn_that_stayed_on_frontier_is_clean(flag_on, _no_plugin_hooks):
    agent = _TurnAgent(
        served_model=REAL_FRONTIER_ALT, primary_runtime={"model": REAL_FRONTIER}
    )
    result = _finalize(agent, frontier_required=True)
    assert "warnings" not in result


def test_caller_stamped_requested_model_wins_over_the_runtime_snapshot(
    flag_on, _no_plugin_hooks
):
    """``_frontier_requested_model`` closes the pre-agent fallback window.

    ``gateway/run.py::_try_resolve_fallback_provider`` can swap the provider
    before the agent exists, so the primary-runtime snapshot may already be the
    fallback. A caller that knows what it originally asked for stamps it, and
    that must take precedence.
    """
    agent = _TurnAgent(
        served_model=REAL_CHEAP, primary_runtime={"model": REAL_CHEAP}
    )
    agent._frontier_requested_model = REAL_FRONTIER
    result = _finalize(agent, frontier_required=True)
    assert result["warnings"][0]["requested_model"] == REAL_FRONTIER


def test_cron_turn_is_untouched_when_the_caller_did_not_require_frontier(
    flag_on, _no_plugin_hooks
):
    """Opt-in: every caller that exists today leaves the kwarg at its default."""
    agent = _TurnAgent(
        served_model=REAL_CHEAP, primary_runtime={"model": REAL_FRONTIER}
    )
    result = _finalize(agent, frontier_required=False)
    assert "warnings" not in result


def test_cron_turn_is_untouched_when_the_flag_is_off(monkeypatch, _no_plugin_hooks):
    """Rollback path, exercised on the real turn path rather than on the guard."""
    monkeypatch.delenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, raising=False)
    agent = _TurnAgent(
        served_model=REAL_CHEAP, primary_runtime={"model": REAL_FRONTIER}
    )
    result = _finalize(agent, frontier_required=True)
    assert "warnings" not in result


def test_a_downgraded_cron_turn_warns_exactly_once(flag_on, _no_plugin_hooks):
    """One turn, one warning — the finalizer hook fires a single time.

    ``finalize_turn`` is called once per turn from ``run_conversation``, but the
    check appends rather than replaces, so a second invocation on the same
    result dict would stack duplicates. This pins the per-turn count.
    """
    agent = _TurnAgent(
        served_model=REAL_CHEAP, primary_runtime={"model": REAL_FRONTIER}
    )
    result = _finalize(agent, frontier_required=True)
    assert len(result["warnings"]) == 1


# --- the two hooks must not both fire for one call ---------------------------


def test_api_server_path_does_not_forward_the_flag_into_run_conversation():
    """No double-warning across the two Layer A hooks.

    Both hooks read the same flag. If ``_run_agent`` forwarded
    ``frontier_required`` into ``run_conversation``, the turn-end hook would
    append its own warning for the same call and every API-server downgrade
    would be reported twice. ``_run_agent`` checks at its own seam and must
    keep the kwarg to itself.
    """
    source = inspect.getsource(
        __import__(
            "gateway.platforms.api_server", fromlist=["APIServerAdapter"]
        ).APIServerAdapter._run_agent
    )
    assert "frontier_required=frontier_required" not in source


def test_duplicate_warnings_are_not_produced_by_a_single_guard_call(flag_on):
    """Appending twice requires two calls; one call appends one entry."""
    result = {"warnings": [{"type": "something_else"}]}
    fg.check_frontier_downgrade(
        result,
        requested_model=REAL_FRONTIER,
        served_model=REAL_CHEAP,
        frontier_required=True,
    )
    assert len(result["warnings"]) == 2
    assert [w["type"] for w in result["warnings"]] == [
        "something_else",
        "frontier_downgrade",
    ]

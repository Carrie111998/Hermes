"""HF-04 Layer A (IGN-196): fail-loud frontier check on the API-server path.

``APIServerAdapter._run_agent`` is the point at which the API-server call path
has finished resolving fallbacks and ``agent.model`` is the model that actually
served the turn — the same point the existing ``confirmed_runtime_lock``
hard-check reads. These tests drive the real ``_run_agent`` coroutine against a
stand-in adapter so the wiring (not just ``agent.frontier_guard``, which
IGN-197 covers on its own) is what gets exercised.

Two independent gates must both be open for a warning to appear: the caller's
per-call ``frontier_required`` and the shared
``HERMES_FRONTIER_DOWNGRADE_CHECK`` env flag, which defaults off.
"""

import contextlib
import sys
import types

import pytest

from gateway.platforms.api_server import APIServerAdapter
from agent import frontier_guard as fg


FRONTIER = {"gpt-5.6-sol", "claude-opus-5"}
CHEAP = {"gemini-3.6-flash", "deepseek-v4-flash"}


def _stub_model_class(model):
    bare = str(model).split("/")[-1].strip()
    if bare in FRONTIER:
        return "frontier"
    if bare in CHEAP:
        return "benchmarked_cheap"
    return "unknown"


@pytest.fixture
def classifier(monkeypatch):
    """Install a stand-in for IGN-195's shared ``model_class``.

    The guard resolves the classifier by import, so the real
    ``agent.model_classification`` is what runs in production; stubbing it here
    keeps these tests about the API-server wiring rather than about which model
    names happen to be on the frontier list today.
    """
    module = types.ModuleType("agent.model_classification")
    module.model_class = _stub_model_class
    monkeypatch.setitem(sys.modules, "agent.model_classification", module)
    return module


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "1")


@pytest.fixture(autouse=True)
def _reset_missing_classifier_latch(monkeypatch):
    monkeypatch.setattr(fg, "_classifier_missing_reported", False)


class _FakeAgent:
    """Minimal stand-in for the AIAgent that ``_create_agent`` would return."""

    def __init__(self, served_model):
        self.model = served_model
        self.provider = "stub-provider"
        self.session_id = "sess-1"
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self._tool_guardrail_halt_decision = None

    def run_conversation(self, **kwargs):
        return {"final_response": "ok", "messages": [], "api_calls": 1, "tools": []}


class _StubAdapter:
    """Enough of APIServerAdapter for the real ``_run_agent`` to run.

    ``_run_agent`` is taken unbound from the real class so the code under test
    is the shipped implementation, not a re-description of it.
    """

    _run_agent = APIServerAdapter._run_agent
    # ``_clean_runtime_id`` is a staticmethod on the real class; copying the
    # plain function across would rebind it as an instance method and feed it
    # ``self``. The other two are classmethods, which stay bound on access.
    _clean_runtime_id = staticmethod(APIServerAdapter._clean_runtime_id)
    _split_provider_prefixed_model = APIServerAdapter._split_provider_prefixed_model
    _sanitize_runtime_metadata = APIServerAdapter._sanitize_runtime_metadata

    def __init__(self, served_model):
        self._served_model = served_model
        self._active_run_agents = {}
        self._shutdown_interruptible_agents = {}
        self._inflight_agent_runs = 0

    def _activate_admitted_request(self):
        return None

    @staticmethod
    @contextlib.contextmanager
    def _profile_scope(profile):
        yield

    def _bind_api_server_session(self, **kwargs):
        return None

    def _create_agent(self, **kwargs):
        return _FakeAgent(self._served_model)


async def _run(served_model, *, requested, frontier_required, **extra):
    adapter = _StubAdapter(served_model)
    result, _usage = await adapter._run_agent(
        user_message="hi",
        conversation_history=[],
        requested_model=requested,
        frontier_required=frontier_required,
        **extra,
    )
    return result


@pytest.mark.asyncio
async def test_downgrade_is_reported_to_the_caller(flag_on, classifier):
    result = await _run(
        "gemini-3.6-flash", requested="gpt-5.6-sol", frontier_required=True
    )
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert warning["type"] == "frontier_downgrade"
    assert warning["requested_model"] == "gpt-5.6-sol"
    assert warning["served_model"] == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_no_warning_when_flag_is_off(monkeypatch, classifier):
    """Default-off rollback path: a real downgrade produces nothing."""
    monkeypatch.delenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, raising=False)
    result = await _run(
        "gemini-3.6-flash", requested="gpt-5.6-sol", frontier_required=True
    )
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_no_warning_when_caller_did_not_require_frontier(flag_on, classifier):
    """Opt-in: every existing caller leaves ``frontier_required`` at its default."""
    result = await _run(
        "gemini-3.6-flash", requested="gpt-5.6-sol", frontier_required=False
    )
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_frontier_served_by_frontier_is_clean(flag_on, classifier):
    """A fallback that stays in-class is not a downgrade."""
    result = await _run(
        "claude-opus-5", requested="gpt-5.6-sol", frontier_required=True
    )
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_provider_prefixed_served_model_is_not_a_false_positive(
    flag_on, classifier
):
    """``provider::model`` must be stripped before classifying.

    Left prefixed, the classifier would return 'unknown' and an in-class
    fallback would be reported as a downgrade that never happened.
    """
    result = await _run(
        "openai::claude-opus-5", requested="gpt-5.6-sol", frontier_required=True
    )
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_requested_model_comes_from_the_lock_contract_when_present(
    flag_on, classifier
):
    """``route``/``requested_runtime`` win over the bare per-request value.

    This mirrors the ``expected_model`` precedence the adjacent
    ``confirmed_runtime_lock`` check already uses.
    """
    result = await _run(
        "gemini-3.6-flash",
        requested="deepseek-v4-flash",
        frontier_required=True,
        requested_runtime={"model": "gpt-5.6-sol"},
    )
    assert result["warnings"][0]["requested_model"] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_check_runs_even_when_runtime_metadata_is_not_reported(
    flag_on, classifier
):
    """``include_runtime`` gates *reporting*, never the downgrade check.

    With no route, requested_runtime, lock, or non-global route_source, the
    runtime-metadata block is skipped entirely — the check must still fire.
    """
    result = await _run(
        "gemini-3.6-flash", requested="gpt-5.6-sol", frontier_required=True
    )
    assert "runtime" not in result
    assert result["warnings"][0]["type"] == "frontier_downgrade"


@pytest.mark.asyncio
async def test_missing_classifier_fails_loud_rather_than_passing_silently(
    monkeypatch, flag_on
):
    """A check that cannot run must not look like a clean one."""
    monkeypatch.setattr(fg, "_resolve_model_class", lambda: None)
    result = await _run(
        "gemini-3.6-flash", requested="gpt-5.6-sol", frontier_required=True
    )
    assert result["warnings"][0]["type"] == "frontier_check_unavailable"


@pytest.mark.asyncio
async def test_downgrade_never_blocks_the_response(flag_on, classifier):
    """Non-blocking by contract — unlike the exact-pin confirmed lock."""
    result = await _run(
        "gemini-3.6-flash", requested="gpt-5.6-sol", frontier_required=True
    )
    assert result["final_response"] == "ok"


def test_flag_defaults_to_off_and_is_read_per_call(monkeypatch):
    """Rollback is instant: no process-start caching of the flag."""
    monkeypatch.delenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, raising=False)
    assert fg.frontier_downgrade_check_enabled() is False
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "1")
    assert fg.frontier_downgrade_check_enabled() is True
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "0")
    assert fg.frontier_downgrade_check_enabled() is False


def test_run_agent_signature_is_opt_in():
    """Existing callers must be unaffected: the new kwarg defaults to False."""
    import inspect

    params = inspect.signature(APIServerAdapter._run_agent).parameters
    assert "frontier_required" in params
    assert params["frontier_required"].default is False

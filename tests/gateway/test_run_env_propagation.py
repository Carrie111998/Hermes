"""Per-run env propagation + isolation (POST /v1/runs). Fake values only."""
import asyncio

import pytest

import gateway.session_context as sc


# --- get/set/reset + default ---------------------------------------------
def test_get_run_env_empty_by_default():
    assert sc.get_run_env() == {}


def test_set_get_reset_run_env():
    tok = sc.set_run_env({"SITE_URL": "https://thebikeratlas.com"})
    try:
        assert sc.get_run_env() == {"SITE_URL": "https://thebikeratlas.com"}
    finally:
        sc.reset_run_env(tok)
    assert sc.get_run_env() == {}


def test_set_run_env_coerces_values_to_str():
    tok = sc.set_run_env({"BIKER_ATLAS_DB_DISABLED": "true"})
    try:
        v = sc.get_run_env()["BIKER_ATLAS_DB_DISABLED"]
        assert v == "true" and isinstance(v, str)
    finally:
        sc.reset_run_env(tok)


# --- concurrency isolation (the core guarantee) --------------------------
@pytest.mark.asyncio
async def test_concurrent_runs_isolated():
    seen = {}

    async def run(name, env):
        tok = sc.set_run_env(env)
        await asyncio.sleep(0.02)  # interleave the two tasks
        seen[name] = sc.get_run_env()
        sc.reset_run_env(tok)

    await asyncio.gather(
        asyncio.create_task(run("a", {"SITE_URL": "https://a.example"})),
        asyncio.create_task(run("b", {"SITE_URL": "https://b.example"})),
    )
    assert seen["a"] == {"SITE_URL": "https://a.example"}
    assert seen["b"] == {"SITE_URL": "https://b.example"}


# --- cleanup on all terminal paths (mirrors _run_sync try/finally) -------
def test_cleanup_after_success():
    tok = sc.set_run_env({"X": "1"})
    try:
        pass  # "run" completes normally
    finally:
        sc.reset_run_env(tok)
    assert sc.get_run_env() == {}


def test_cleanup_after_exception():
    tok = sc.set_run_env({"X": "1"})
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        pass
    finally:
        sc.reset_run_env(tok)
    assert sc.get_run_env() == {}


@pytest.mark.asyncio
async def test_cleanup_after_cancellation():
    tok = sc.set_run_env({"X": "1"})
    try:
        raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    finally:
        sc.reset_run_env(tok)
    assert sc.get_run_env() == {}


# --- subprocess bridge: run env reaches child env, session-var-authoritative
def test_subprocess_bridge_overlays_run_env():
    from tools.environments.local import _inject_session_context_env

    tok = sc.set_run_env({
        "SITE_URL": "https://thebikeratlas.com",
        "BIKER_ATLAS_DB_DISABLED": "true",
    })
    try:
        env: dict = {}
        _inject_session_context_env(env)
        assert env["SITE_URL"] == "https://thebikeratlas.com"
        assert env["BIKER_ATLAS_DB_DISABLED"] == "true"
        assert "DATABASE_URL" not in env
    finally:
        sc.reset_run_env(tok)

    # After reset, the child env no longer carries run values.
    env2: dict = {}
    _inject_session_context_env(env2)
    assert "SITE_URL" not in env2


# --- validation (POST /v1/runs body.env) ---------------------------------
def _validate():
    from gateway.platforms.api_server import _validate_run_env
    return _validate_run_env


def test_validate_accepts_string_map():
    fn = _validate()
    assert fn({"A": "1", "B": "x"}) == {"A": "1", "B": "x"}


def test_validate_none_is_empty():
    assert _validate()(None) == {}


def test_validate_rejects_non_object():
    with pytest.raises(ValueError):
        _validate()(["not", "an", "object"])


def test_validate_rejects_non_string_value():
    with pytest.raises(ValueError):
        _validate()({"A": True})


def test_validate_rejects_binding_object():
    # The exact bug: an unresolved Paperclip binding must be rejected, not
    # passed through.
    with pytest.raises(ValueError):
        _validate()({"BIKER_ATLAS_DB_DISABLED": {"type": "plain", "value": "true"}})


# --- reserved / unsafe keys (review concern #1) --------------------------
@pytest.mark.parametrize(
    "key",
    [
        "PATH", "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV",
        "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "NO_PROXY",
        "HERMES_SESSION_ID", "HERMES_TASK_ID", "hermes_anything",
    ],
)
def test_validate_rejects_reserved_or_unsafe_keys(key):
    # A per-run env must not hijack tool subprocess execution, redirect egress,
    # or spoof session attribution.
    with pytest.raises(ValueError):
        _validate()({key: "x"})


def test_validate_accepts_ordinary_app_keys():
    # The Biker Atlas keys are ordinary app config and must still pass.
    env = {
        "SITE_URL": "https://thebikeratlas.com",
        "DIRECTORY_API_URL": "https://thebikeratlas.com/api",
        "BIKER_ATLAS_DB_DISABLED": "true",
        "DIRECTORY_AGENT_PASSWORD": "sup3r-s3cr3t-value",
    }
    assert _validate()(env) == env


# --- run env follows the real worker-thread channel (review concern #2) ---
def test_run_env_propagates_through_worker_thread():
    """ContextVars (incl. _RUN_ENV) reach pool threads via the same
    propagate_context_to_thread channel the session vars use."""
    from concurrent.futures import ThreadPoolExecutor

    from tools.environments.local import _inject_session_context_env
    from tools.thread_context import propagate_context_to_thread

    def _read_env_in_thread():
        env: dict = {}
        _inject_session_context_env(env)
        return env

    tok = sc.set_run_env({"SITE_URL": "https://thebikeratlas.com"})
    try:
        wrapped = propagate_context_to_thread(_read_env_in_thread)
        with ThreadPoolExecutor(max_workers=1) as pool:
            child_env = pool.submit(wrapped).result()
        assert child_env.get("SITE_URL") == "https://thebikeratlas.com"
    finally:
        sc.reset_run_env(tok)


# --- api_server error redactor scrubs registered run secrets --------------
def test_redact_api_error_text_scrubs_registered_secret():
    from agent.redact import set_extra_literal_secrets, reset_extra_literal_secrets
    from gateway.platforms.api_server import _redact_api_error_text

    secret = "sup3r-s3cr3t-value"
    tok = set_extra_literal_secrets([secret])
    try:
        out = _redact_api_error_text(f"boom while using {secret} in run")
        assert secret not in out
        assert "boom" in out  # non-secret context preserved
    finally:
        reset_extra_literal_secrets(tok)
    # After reset the value is no longer force-scrubbed by the literal pass.
    assert secret in _redact_api_error_text(f"echo {secret}")


def test_redact_ignores_short_common_secret_values():
    """Short common values must NOT be force-scrubbed (over-redaction guard).

    A secret-keyed value such as "true"/"admin"/"prod" would otherwise strike
    identical substrings in legitimate agent output and corrupt it.
    """
    from agent.redact import set_extra_literal_secrets, reset_extra_literal_secrets
    from gateway.platforms.api_server import _redact_api_error_text

    tok = set_extra_literal_secrets(["true", "admin", "prod"])
    try:
        out = _redact_api_error_text("the flag is true and the admin approved prod")
        assert "true" in out and "admin" in out and "prod" in out
    finally:
        reset_extra_literal_secrets(tok)


# --- HTTP integration: POST /v1/runs validation at the real endpoint ------
def _runs_app():
    from tests.gateway.test_api_server import _make_adapter, _create_app
    adapter = _make_adapter()
    app = _create_app(adapter)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    return adapter, app


@pytest.mark.asyncio
async def test_post_runs_rejects_binding_object():
    from aiohttp.test_utils import TestClient, TestServer
    _, app = _runs_app()
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/v1/runs", json={
            "input": "hi",
            "env": {"BIKER_ATLAS_DB_DISABLED": {"type": "plain", "value": "true"}},
        })
        assert resp.status == 400
        assert "env" in str(await resp.json()).lower()


@pytest.mark.asyncio
async def test_post_runs_rejects_non_string_value():
    from aiohttp.test_utils import TestClient, TestServer
    _, app = _runs_app()
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/v1/runs", json={"input": "hi", "env": {"X": True}})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_post_runs_accepts_valid_string_env():
    from unittest.mock import MagicMock, patch
    from aiohttp.test_utils import TestClient, TestServer
    adapter, app = _runs_app()
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "ok"}
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_create_agent", return_value=mock_agent):
            resp = await cli.post("/v1/runs", json={
                "input": "hi",
                "env": {
                    "SITE_URL": "https://thebikeratlas.com",
                    "BIKER_ATLAS_DB_DISABLED": "true",
                },
            })
            # Validation passed → the run is accepted (not a 400 reject).
            assert resp.status != 400

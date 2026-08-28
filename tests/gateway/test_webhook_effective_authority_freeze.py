"""Regressions for immutable webhook execution-authority publication."""

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.platforms.webhook_intake as webhook_intake_module
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    _PROFILE_AUTHORITY_INCARNATION_FILENAME,
    _clear_quarantined_retirement_owner,
    _profile_incarnation_token,
    _quarantined_retirement_owners,
)
from gateway.platforms.webhook_filters import (
    WebhookScriptDisposition,
    WebhookScriptResult,
)
from gateway.platforms.webhook_contract import WebhookContractError


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _adapter(route: dict, *, secret: str = "authority-secret") -> WebhookAdapter:
    route = {
        "secret": secret,
        "signature_mode": "generic_v1",
        **route,
    }
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {"events": route},
            },
        )
    )


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _signed(payload: dict, secret: str = "authority-secret"):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
    }


def _request(body: bytes, headers: dict[str, str]):
    async def read():
        return body

    return SimpleNamespace(
        headers=headers,
        content_length=len(body),
        match_info={"route_name": "events"},
        read=read,
    )


@pytest.mark.asyncio
async def test_deep_authenticated_json_is_rejected_as_payload_contract_error():
    adapter = _adapter({"prompt": "never-run"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    body = b'{"value":' + b"[" * 500 + b"0" + b"]" * 500 + b"}"
    signature = hmac.new(
        b"authority-secret",
        body,
        hashlib.sha256,
    ).hexdigest()

    response = await adapter._handle_webhook(
        _request(
            body,
            {
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            },
        )
    )

    assert response.status == 400
    assert b"Invalid authenticated webhook payload" in response.body
    assert adapter._operation_ledger.count() == 0
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_filter_file_change_after_signature_cannot_widen_frozen_filter(
    tmp_path,
):
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text('["allowed"]', encoding="utf-8")
    adapter = _adapter({
        "filters": {
            "field": "actor",
            "in_file": "watchlist.json",
        },
        "prompt": "actor={actor}",
    })
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)

    original = adapter._live_route_authority_matches
    checked = threading.Event()
    proceed = threading.Event()

    def pause_after_live_check(route_name, bundle):
        result = original(route_name, bundle)
        checked.set()
        assert proceed.wait(timeout=5)
        return result

    adapter._live_route_authority_matches = pause_after_live_check
    body, headers = _signed({"actor": "newly-added"})
    async with TestClient(TestServer(_app(adapter))) as client:
        pending = asyncio.create_task(
            client.post(
                "/webhooks/events",
                data=body,
                headers=headers,
            )
        )
        assert await asyncio.to_thread(checked.wait, 5)
        watchlist.write_text('["allowed","newly-added"]', encoding="utf-8")
        proceed.set()
        response = await pending
        payload = await response.json()

    assert response.status == 200
    assert payload["status"] == "ignored"
    assert payload["reason"] == "filter"
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_filter_file_change_withdraws_route_and_releases_new_claim(tmp_path):
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text('["old"]', encoding="utf-8")
    adapter = _adapter({
        "filters": {"field": "actor", "in_file": "watchlist.json"},
    })
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    watchlist.write_text('["old","widened"]', encoding="utf-8")
    body, headers = _signed({"actor": "widened"})

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/events",
            data=body,
            headers=headers,
        )

    assert response.status == 503
    assert "events" not in adapter._routes
    assert adapter._operation_ledger.count() == 0
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_literal_in_file_key_inside_equals_is_not_read_as_filter_file():
    adapter = _adapter({
        "filters": {
            "field": "meta",
            "equals": {"in_file": "literal-not-a-path"},
        },
        "prompt": "matched",
    })
    adapter.handle_message = AsyncMock()
    body, headers = _signed({"meta": {"in_file": "literal-not-a-path"}})

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/events",
            data=body,
            headers=headers,
        )

    assert response.status == 202
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_skill_scaffold_is_frozen_across_post_signature_change(monkeypatch):
    version = {"value": "v1"}

    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/review": object()},
    )

    def build_skill(command, *, user_instruction):
        assert command == "/review"
        return f"skill-{version['value']}\n{user_instruction}"

    monkeypatch.setattr(
        "agent.skill_commands.build_skill_invocation_message",
        build_skill,
    )
    adapter = _adapter({"skills": ["review"], "prompt": "payload={value}"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)

    original = adapter._live_route_authority_matches
    checked = threading.Event()
    proceed = threading.Event()

    def pause_after_live_check(route_name, bundle):
        result = original(route_name, bundle)
        checked.set()
        assert proceed.wait(timeout=5)
        return result

    adapter._live_route_authority_matches = pause_after_live_check
    body, headers = _signed({"value": "one"})
    async with TestClient(TestServer(_app(adapter))) as client:
        pending = asyncio.create_task(
            client.post(
                "/webhooks/events",
                data=body,
                headers=headers,
            )
        )
        assert await asyncio.to_thread(checked.wait, 5)
        version["value"] = "v2"
        proceed.set()
        response = await pending

    assert response.status == 202
    event = adapter.handle_message.await_args.args[0]
    assert event.text.startswith("skill-v1\n")
    assert "skill-v2" not in event.text


@pytest.mark.asyncio
async def test_home_channel_change_requires_rotated_route_policy():
    home_a = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="chat-a",
        name="home-a",
        thread_id="1",
    )
    target_config = PlatformConfig(enabled=True, home_channel=home_a)
    target = SimpleNamespace(config=target_config)
    adapter = _adapter({
        "deliver": "telegram",
        "deliver_only": True,
        "prompt": "notice",
    })
    runner = SimpleNamespace(
        adapters={Platform.WEBHOOK: adapter, Platform.TELEGRAM: target},
        config=SimpleNamespace(
            multiplex_profiles=False, get_home_channel=lambda _p: None
        ),
        _active_profile_name=lambda: "default",
    )
    adapter.gateway_runner = runner
    adapter._bind_route_authentication_authorities(adapter._routes)
    target.config.home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="chat-b",
        name="home-b",
        thread_id="2",
    )
    body, headers = _signed({"value": 1})

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/events",
            data=body,
            headers=headers,
        )

    assert response.status == 503
    assert "events" not in adapter._routes
    assert adapter._operation_ledger.count() == 0


@pytest.mark.asyncio
async def test_old_key_request_cannot_rejoin_new_name_indexed_bundle():
    adapter = _adapter({"prompt": "policy-a"}, secret="key-a")
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    original = adapter._live_route_authority_matches
    checked = threading.Event()
    proceed = threading.Event()

    def pause_after_live_check(route_name, bundle):
        result = original(route_name, bundle)
        checked.set()
        assert proceed.wait(timeout=5)
        return result

    adapter._live_route_authority_matches = pause_after_live_check
    old_body, old_headers = _signed({"value": "old"}, "key-a")
    async with TestClient(TestServer(_app(adapter))) as client:
        stale = asyncio.create_task(
            client.post(
                "/webhooks/events",
                data=old_body,
                headers=old_headers,
            )
        )
        assert await asyncio.to_thread(checked.wait, 5)
        route_b = {
            "events": {
                "secret": "key-b",
                "signature_mode": "generic_v1",
                "prompt": "policy-b-elevated",
                "toolsets": ["terminal"],
            }
        }
        adapter._bind_route_authentication_authorities(route_b)
        adapter._routes = route_b
        proceed.set()
        stale_response = await stale

        adapter._live_route_authority_matches = original
        new_body, new_headers = _signed({"value": "new"}, "key-b")
        current_response = await client.post(
            "/webhooks/events",
            data=new_body,
            headers=new_headers,
        )

    assert stale_response.status == 503
    assert current_response.status == 202
    assert adapter._routes["events"]["prompt"] == "policy-b-elevated"
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "policy-b-elevated"


@pytest.mark.asyncio
async def test_filter_cancellation_retains_global_slot_across_replacement(
    monkeypatch,
):
    monkeypatch.setattr(
        webhook_intake_module,
        "_route_worker_slots",
        threading.BoundedSemaphore(1),
    )
    adapter = _adapter({"filters": [], "prompt": "retryable"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    entered = threading.Event()
    proceed = threading.Event()
    exited = threading.Event()
    calls = 0
    original = adapter._route_processor.route_filters_match

    def blocking_filter(*_args):
        nonlocal calls
        calls += 1
        try:
            entered.set()
            assert proceed.wait(timeout=5)
            return True
        finally:
            exited.set()

    adapter._route_processor.route_filters_match = blocking_filter
    body, headers = _signed({"value": "cancel-filter"})
    task = asyncio.create_task(adapter._handle_webhook(_request(body, headers)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter._operation_ledger.count() == 0

    replacement = _adapter({"filters": [], "prompt": "retryable"})
    replacement.handle_message = AsyncMock()
    replacement._bind_route_authentication_authorities(replacement._routes)

    busy_body, busy_headers = _signed({"value": "busy-filter"})
    busy = await replacement._handle_webhook(_request(busy_body, busy_headers))
    assert busy.status == 503
    assert busy.headers["Retry-After"] == "1"
    assert calls == 1
    assert replacement._operation_ledger.count() == 0

    proceed.set()
    assert await asyncio.to_thread(exited.wait, 5)
    await asyncio.sleep(0)
    adapter._route_processor.route_filters_match = original
    retry = await replacement._handle_webhook(_request(body, headers))
    assert retry.status == 202
    pending = tuple(replacement._background_tasks)
    if pending:
        await asyncio.gather(*pending)
    replacement.handle_message.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_route_worker_capacity_never_exceeds_four(monkeypatch):
    monkeypatch.setattr(
        webhook_intake_module,
        "_route_worker_slots",
        threading.BoundedSemaphore(4),
    )
    adapters = [
        _adapter({"filters": [], "prompt": "bounded"}),
        _adapter({"filters": [], "prompt": "bounded"}),
    ]
    for adapter in adapters:
        adapter.handle_message = AsyncMock()
        adapter._bind_route_authentication_authorities(adapter._routes)
    lock = threading.Lock()
    all_entered = threading.Event()
    proceed = threading.Event()
    active = 0
    maximum_active = 0
    calls = 0

    def blocking_filter(*_args):
        nonlocal active, maximum_active, calls
        with lock:
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 4:
                all_entered.set()
        try:
            assert proceed.wait(timeout=5)
            return True
        finally:
            with lock:
                active -= 1

    for adapter in adapters:
        adapter._route_processor.route_filters_match = blocking_filter
    requests = []
    for index in range(8):
        body, headers = _signed({"value": f"bounded-{index}"})
        adapter = adapters[index % len(adapters)]
        requests.append(
            asyncio.create_task(adapter._handle_webhook(_request(body, headers)))
        )

    assert await asyncio.to_thread(all_entered.wait, 5)
    for _attempt in range(500):
        if sum(request.done() for request in requests) == 4:
            break
        await asyncio.sleep(0.01)
    assert sum(request.done() for request in requests) == 4
    proceed.set()
    responses = await asyncio.gather(*requests)
    statuses = [response.status for response in responses]
    assert statuses.count(202) == 4
    assert statuses.count(503) == 4
    assert calls == 4
    assert maximum_active == 4
    pending = tuple(task for adapter in adapters for task in adapter._background_tasks)
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_script_worker_saturation_releases_before_mark_started(
    tmp_path,
    monkeypatch,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "route.py").write_text(
        "import json, sys\njson.dump(json.load(sys.stdin), sys.stdout)\n",
        encoding="utf-8",
    )
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(webhook_intake_module, "_route_worker_slots", gate)
    adapter = _adapter({"script": "route.py", "prompt": "scripted"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    original_mark_started = adapter._operation_ledger.mark_script_started
    mark_started = MagicMock(wraps=original_mark_started)
    monkeypatch.setattr(
        adapter._operation_ledger,
        "mark_script_started",
        mark_started,
    )
    original_worker = adapter._run_retained_route_worker
    saturated = False

    async def saturate_after_filter(operation, *args, **kwargs):
        nonlocal saturated
        result = await original_worker(operation, *args, **kwargs)
        if not saturated:
            assert gate.acquire(blocking=False)
            saturated = True
        return result

    monkeypatch.setattr(
        adapter,
        "_run_retained_route_worker",
        saturate_after_filter,
    )
    body, headers = _signed({"value": "busy-script"})

    try:
        response = await adapter._handle_webhook(_request(body, headers))
    finally:
        if saturated:
            gate.release()

    assert response.status == 503
    assert response.headers["Retry-After"] == "1"
    assert adapter._operation_ledger.count() == 0
    mark_started.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_cancellation_waits_for_worker_then_marks_indeterminate(
    tmp_path,
    monkeypatch,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "route.py").write_text(
        "import json, sys\njson.dump(json.load(sys.stdin), sys.stdout)\n",
        encoding="utf-8",
    )
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(webhook_intake_module, "_route_worker_slots", gate)
    adapter = _adapter({"script": "route.py", "prompt": "scripted"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    entered = threading.Event()
    exited = threading.Event()
    original = adapter._route_processor.run_prepared_script
    real_mark_indeterminate = adapter._operation_ledger.mark_indeterminate

    def blocking_script(*_args, cancellation_event=None):
        assert cancellation_event is not None
        try:
            entered.set()
            assert cancellation_event.wait(timeout=5)
            return WebhookScriptResult(
                disposition=WebhookScriptDisposition.INDETERMINATE,
                error="cancelled",
            )
        finally:
            exited.set()

    def mark_after_worker_exit(authority, reason):
        assert exited.is_set()
        return real_mark_indeterminate(authority, reason)

    adapter._route_processor.run_prepared_script = blocking_script
    monkeypatch.setattr(
        adapter._operation_ledger,
        "mark_indeterminate",
        mark_after_worker_exit,
    )
    body, headers = _signed({"value": "cancel-script"})
    task = asyncio.create_task(adapter._handle_webhook(_request(body, headers)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
    assert gate.acquire(blocking=False)
    assert not gate.acquire(blocking=False)
    gate.release()

    retry = await adapter._handle_webhook(_request(body, headers))
    assert retry.status == 409
    assert b"indeterminate" in retry.body
    adapter._route_processor.run_prepared_script = original
    next_body, next_headers = _signed({"value": "after-cancel"})
    accepted = await adapter._handle_webhook(_request(next_body, next_headers))
    assert accepted.status == 202
    pending = tuple(adapter._background_tasks)
    if pending:
        await asyncio.gather(*pending)
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_post_start_script_failure_is_indeterminate(
    tmp_path,
    monkeypatch,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "route.py").write_text(
        "import json, sys\njson.dump(json.load(sys.stdin), sys.stdout)\n",
        encoding="utf-8",
    )
    adapter = _adapter({"script": "route.py", "prompt": "scripted"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    original_mark = adapter._operation_ledger.mark_indeterminate
    mark_indeterminate = MagicMock(wraps=original_mark)
    monkeypatch.setattr(
        adapter._operation_ledger,
        "mark_indeterminate",
        mark_indeterminate,
    )
    monkeypatch.setattr(
        adapter._route_processor,
        "run_prepared_script",
        MagicMock(side_effect=RecursionError("nested script output")),
    )
    body, headers = _signed({"value": "deep-script-output"})

    response = await adapter._handle_webhook(_request(body, headers))

    assert response.status == 500
    assert b"indeterminate" in response.body
    mark_indeterminate.assert_called_once()
    authority = mark_indeterminate.call_args.args[0]
    restored = adapter._operation_ledger.lookup_session(authority.session_key)
    assert restored is not None
    assert restored.state.value == "indeterminate"
    retry = await adapter._handle_webhook(_request(body, headers))
    assert retry.status == 409
    adapter.handle_message.assert_not_awaited()


def test_route_publication_rejects_profile_rotation_during_dependency_snapshot(
    tmp_path,
    monkeypatch,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script_path = scripts / "route.py"
    script_path.write_text("version = 'old'\n", encoding="utf-8")
    adapter = _adapter({"script": "route.py", "prompt": "scripted"})
    token_path = tmp_path / _PROFILE_AUTHORITY_INCARNATION_FILENAME
    original_prepare = adapter._route_processor.prepare_route_script
    prior_token = None

    def rotate_after_script_snapshot(script):
        nonlocal prior_token
        prepared, error = original_prepare(script)
        assert prepared is not None
        assert error is None
        prior_token = token_path.read_text(encoding="ascii")
        script_path.write_text("version = 'new'\n", encoding="utf-8")
        token_path.unlink()
        return prepared, error

    monkeypatch.setattr(
        adapter._route_processor,
        "prepare_route_script",
        rotate_after_script_snapshot,
    )

    with pytest.raises(
        WebhookContractError,
        match="profile authority changed while execution dependencies were snapshotted",
    ):
        adapter._bind_route_authentication_authorities(adapter._routes)

    assert prior_token is not None
    assert token_path.read_text(encoding="ascii") != prior_token
    assert adapter._authenticated_route_snapshot is None
    assert dict(adapter._authenticated_route_bundles) == {}

    monkeypatch.setattr(
        adapter._route_processor,
        "prepare_route_script",
        original_prepare,
    )
    adapter._bind_route_authentication_authorities(adapter._routes)
    prepared = adapter._authenticated_route_bundles["events"].prepared_script
    assert prepared is not None
    assert prepared.source == "version = 'new'\n"


def test_route_set_publication_rejects_generation_rotation_between_routes(
    tmp_path,
    monkeypatch,
):
    routes = {
        "a": {
            "secret": "route-a-secret",
            "signature_mode": "generic_v1",
            "prompt": "route-a",
        },
        "b": {
            "secret": "route-b-secret",
            "signature_mode": "generic_v1",
            "prompt": "route-b",
        },
    }
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": routes,
            },
        )
    )
    token_path = tmp_path / _PROFILE_AUTHORITY_INCARNATION_FILENAME
    original_generation = adapter._profile_authority_generation
    prior_token = None
    rotated = False

    def rotate_before_route_b(bound_route, *, authority_profile):
        nonlocal prior_token, rotated
        if bound_route.name == "b" and not rotated:
            prior_token = token_path.read_text(encoding="ascii")
            token_path.unlink()
            rotated = True
        return original_generation(
            bound_route,
            authority_profile=authority_profile,
        )

    monkeypatch.setattr(
        adapter,
        "_profile_authority_generation",
        rotate_before_route_b,
    )

    with pytest.raises(
        WebhookContractError,
        match="authority changed while the route set was snapshotted",
    ):
        adapter._bind_route_authentication_authorities(adapter._routes)

    assert rotated
    assert prior_token is not None
    assert token_path.read_text(encoding="ascii") != prior_token
    assert adapter._authenticated_route_snapshot is None
    assert dict(adapter._authenticated_route_bundles) == {}
    assert dict(adapter._authenticated_route_profile_generations) == {}


@pytest.mark.asyncio
async def test_published_script_route_profile_rotation_fails_before_script_start(
    tmp_path,
    monkeypatch,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "route.py").write_text(
        "import json, sys\njson.dump(json.load(sys.stdin), sys.stdout)\n",
        encoding="utf-8",
    )
    adapter = _adapter({"script": "route.py", "prompt": "scripted"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    published_generation = adapter._authenticated_route_bundles[
        "events"
    ].profile_generation
    mark_script_started = MagicMock(wraps=adapter._operation_ledger.mark_script_started)
    run_prepared_script = MagicMock(wraps=adapter._route_processor.run_prepared_script)
    monkeypatch.setattr(
        adapter._operation_ledger,
        "mark_script_started",
        mark_script_started,
    )
    monkeypatch.setattr(
        adapter._route_processor,
        "run_prepared_script",
        run_prepared_script,
    )
    (tmp_path / _PROFILE_AUTHORITY_INCARNATION_FILENAME).unlink()
    body, headers = _signed({"value": "rotated-profile"})

    response = await adapter._handle_webhook(_request(body, headers))

    assert response.status == 503
    assert adapter._operation_ledger.count() == 0
    assert "events" not in adapter._routes
    mark_script_started.assert_not_called()
    run_prepared_script.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    assert (
        adapter._current_profile_authority_generation(
            "default",
            route_name="events",
        )
        != published_generation
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_transition",
    ["release_pre_effect", "settle_no_effect", "mark_script_started"],
)
async def test_live_transition_failure_fences_and_quarantines_exact_owner(
    tmp_path,
    monkeypatch,
    failed_transition,
):
    route = {"prompt": "transition"}
    if failed_transition == "settle_no_effect":
        route["events"] = ["never-selected"]
    elif failed_transition == "mark_script_started":
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "route.py").write_text(
            "import json, sys\njson.dump(json.load(sys.stdin), sys.stdout)\n",
            encoding="utf-8",
        )
        route["script"] = "route.py"

    adapter = _adapter(route)
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        adapters={Platform.WEBHOOK: adapter},
        _profile_adapters={},
        _startup_restore_in_progress=False,
        _draining=False,
        _external_drain_active=False,
        _running=True,
        _shutdown_event=asyncio.Event(),
        _schedule_webhook_recovery_retry=MagicMock(),
        _update_platform_runtime_status=MagicMock(),
    )
    adapter.gateway_runner = runner
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    if failed_transition == "release_pre_effect":
        monkeypatch.setattr(adapter, "_record_rate_limit_hit", lambda *_a, **_k: False)
    monkeypatch.setattr(
        adapter._operation_ledger,
        failed_transition,
        MagicMock(side_effect=RuntimeError("injected durable transition failure")),
    )
    body, headers = _signed({"event": "not-selected"})

    try:
        response = await adapter._handle_webhook(_request(body, headers))

        assert response.status == 503
        assert adapter._accepting_webhooks is False
        assert _quarantined_retirement_owners(adapter._operation_ledger) == (
            adapter._operation_ledger.instance_id,
        )
        runner._schedule_webhook_recovery_retry.assert_called_once_with(adapter)
        runner._update_platform_runtime_status.assert_called_once()
        assert (
            runner._update_platform_runtime_status.call_args.kwargs["error_code"]
            == "webhook_transition_failed"
        )
        adapter.handle_message.assert_not_awaited()
    finally:
        _clear_quarantined_retirement_owner(
            adapter._operation_ledger,
            adapter._operation_ledger.instance_id,
        )


def test_profile_incarnation_is_atomic_under_concurrent_first_use(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(
            executor.map(
                lambda _index: _profile_incarnation_token(tmp_path),
                range(24),
            )
        )

    assert len(set(tokens)) == 1
    token_path = tmp_path / _PROFILE_AUTHORITY_INCARNATION_FILENAME
    assert token_path.read_text(encoding="ascii") == f"{tokens[0]}\n"
    if os.name == "posix":
        assert token_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("failed_call", ["write", "fsync"])
def test_profile_incarnation_persist_failure_removes_temporary_file(
    tmp_path,
    monkeypatch,
    failed_call,
):
    def fail(*_args, **_kwargs):
        raise OSError(f"injected {failed_call} failure")

    monkeypatch.setattr(f"gateway.platforms.webhook_common.os.{failed_call}", fail)

    with pytest.raises(
        WebhookContractError,
        match="profile authority incarnation token cannot be persisted",
    ):
        _profile_incarnation_token(tmp_path)

    assert not (tmp_path / _PROFILE_AUTHORITY_INCARNATION_FILENAME).exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_profile_delete_recreate_gets_new_generation_even_with_inode_reuse(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    first = _profile_incarnation_token(profile)
    token_path = profile / _PROFILE_AUTHORITY_INCARNATION_FILENAME
    token_path.unlink()
    profile.rmdir()
    profile.mkdir()
    second = _profile_incarnation_token(profile)

    assert second != first


@pytest.mark.asyncio
async def test_unreachable_explicit_profile_does_not_consume_key(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_matches_home",
        lambda _name: False,
    )
    invalid = _adapter({"profile": "not-this-process"}, secret="reusable-key")
    assert await invalid.connect() is False
    assert invalid._runner is None
    assert invalid.fatal_error_code == "webhook_configuration_invalid"
    assert invalid.fatal_error_retryable is False
    assert "not this gateway's profile" in (invalid.fatal_error_message or "")

    corrected = _adapter({}, secret="reusable-key")
    corrected._bind_route_authentication_authorities(corrected._routes)
    assert "events" in corrected._authenticated_route_bundles


@pytest.mark.asyncio
async def test_missing_durable_key_proof_withdraws_cached_route(monkeypatch):
    adapter = _adapter({"prompt": "must not run from cache"})
    adapter.handle_message = AsyncMock()
    adapter._bind_route_authentication_authorities(adapter._routes)
    monkeypatch.setattr(
        adapter._authentication_authority_ledger,
        "authentication_keys_match",
        lambda _bindings: False,
    )
    body, headers = _signed({"value": "after-replacement"})

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/events",
            data=body,
            headers=headers,
        )

    assert response.status == 503
    assert "events" not in adapter._routes
    assert adapter._operation_ledger.count() == 0
    adapter.handle_message.assert_not_awaited()

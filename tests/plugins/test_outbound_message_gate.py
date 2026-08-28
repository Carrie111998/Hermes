from __future__ import annotations

import importlib
import hashlib
import ipaddress
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


mod = importlib.import_module("plugins.outbound_message_gate")



def settings(*targets: str):
    return {
        "protected_targets": list(targets),
        "success_terms": [
            "fixed", "working", "resolved", "live", "ready", "deployed", "verified"
        ],
    }


def record_verifier_receipt(*, turn_id="turn-1", public_fetch=True, public_url="https://linkedin.example/post/1"):
    output = {"authorize": "PASS", "post": "PASS", "public_url": "PASS", "fetch": "PASS"}
    mod.record_tool_result(
        session_id="s1",
        turn_id=turn_id,
        tool_name="linkedin_journey_verifier",
        args={"journey": "linkedin-public-post-journey", "mode": "live"},
        status="success",
        allowed_verifiers={
            "linkedin-verifier": {
                "tool_name": "linkedin_journey_verifier",
                "dedicated_verifier": True,
                "args": {"journey": "linkedin-public-post-journey", "mode": "live"},
                "check_id": "linkedin-public-post-journey",
                "journey_id": "linkedin-public-post-journey",
                "command_id": "linkedin-public-post-verifier-v1",
                "passing_output": output,
            }
        },
        result={
            "outbound_verifier_receipt": {
                "check_id": "linkedin-public-post-journey",
                "verifier_id": "linkedin-verifier",
                "journey_id": "linkedin-public-post-journey",
                "command_id": "linkedin-public-post-verifier-v1",
                "exit_status": 0,
                "build_id": mod.current_build_id(),
                "runtime_id": mod.current_runtime_id(),
                "session_id": "s1",
                "turn_id": turn_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "output": output,
                "output_digest": hashlib.sha256(
                    json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "public_url": public_url,
                "public_fetch": {"ok": public_fetch, "status": 200},
            }
        },
    )


def test_gate_fails_closed_when_runtime_build_identity_drifts(monkeypatch):
    monkeypatch.setattr(
        mod, "_assert_runtime_build_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("security source disk drift")),
    )

    result = mod.gate_outbound_message(
        platform="telegram", chat_id="paul", content="ordinary text",
        metadata={}, settings=settings("telegram:paul"),
    )

    assert result == {
        "action": "rewrite",
        "reason": "runtime_build_identity_invalid",
        "content": mod.SAFE_POLICY_FAILURE_NOTICE,
    }


def test_receipt_admission_rejects_runtime_build_identity_drift(monkeypatch):
    mod.clear_receipts_for_tests()
    monkeypatch.setattr(
        mod, "_assert_runtime_build_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("loaded module mismatch")),
    )

    record_verifier_receipt()

    assert mod._receipts == []


def test_non_protected_target_is_unchanged():
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="other",
        content="Fixed https://dead.example.test/path",
        metadata={},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    assert result == {"action": "allow"}


def test_protected_target_matching_normalizes_platform_and_case_sensitive_id():
    result = mod.gate_outbound_message(
        platform="SLACK",
        chat_id="C123",
        content="The defect is fixed.",
        metadata={},
        settings=settings(" slack:C123 "),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )

    assert result["action"] == "rewrite"
    assert result["reason"] == "claim_receipt_missing"


def test_required_success_patterns_cannot_be_disabled_and_see_visible_markdown():
    for claim in (
        "**dＯne**.",
        "The work is com**plete**.",
        "The service is operational.",
        "The bug no longer occurs.",
        "All checks pass.",
    ):
        result = mod.gate_outbound_message(
            platform="telegram",
            chat_id="paul",
            content=claim,
            metadata={},
            settings={"protected_targets": ["telegram:paul"], "success_terms": []},
            fetcher=lambda _url: {"ok": True, "status": 200},
        )
        assert result["action"] == "rewrite", claim
        assert result["reason"] == "claim_receipt_missing", claim


def test_every_url_is_fetched_and_dead_url_replaces_original_with_honest_failure():
    seen = []

    def fetcher(url):
        seen.append(url)
        if "dead.example" in url:
            return {"ok": False, "status": 404, "final_url": url, "error": "HTTP 404"}
        return {"ok": True, "status": 200, "final_url": url}

    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content="Use https://good.example/a and https://dead.example/b.",
        metadata={},
        settings=settings("telegram:paul"),
        fetcher=fetcher,
    )

    assert seen == ["https://good.example/a", "https://dead.example/b"]
    assert result["action"] == "rewrite"
    assert result["reason"] == "url_check_failed"
    assert "Use https://good.example/a" not in result["content"]
    assert "dead.example" not in result["content"]
    assert "/b" not in result["content"]
    assert result["content"] == mod.SAFE_POLICY_FAILURE_NOTICE


def test_unsupported_url_scheme_fails_closed_instead_of_bypassing_fetch():
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content="Download ftp://files.example.test/release.zip",
        metadata={},
        settings=settings("telegram:paul"),
        fetcher=mod.fetch_url_live,
    )

    assert result["action"] == "rewrite"
    assert result["reason"] == "url_check_failed"
    assert result["content"] == mod.SAFE_POLICY_FAILURE_NOTICE


def test_gate_caps_url_count_before_any_network_io():
    attempted = []
    content = " ".join(f"https://example.com/{index}" for index in range(9))
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content=content,
        metadata={},
        settings={"protected_targets": ["telegram:paul"], "max_urls": 8},
        fetcher=lambda url: attempted.append(url) or {"ok": True, "status": 200},
    )

    assert attempted == []
    assert result == {
        "action": "rewrite",
        "reason": "url_check_failed",
        "content": mod.SAFE_POLICY_FAILURE_NOTICE,
    }


def test_live_fetch_rejects_local_private_link_local_metadata_and_tailnet_without_io():
    attempted = []

    def forbidden_requester(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("network I/O must not start")
    urls = (
        "http://127.0.0.1/admin",
        "http://10.0.0.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/tailnet",
        "http://[::1]/admin",
    )

    for url in urls:
        result = mod.fetch_url_live(url, requester=forbidden_requester)
        assert result["ok"] is False, url
        assert result["error"] == "destination is not public", url
    assert attempted == []


def test_redirect_hops_are_revalidated_and_private_target_is_never_requested():
    requested = []

    def resolver(host, port, type=0):
        del port, type
        if host == "public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        raise AssertionError(f"private literal should be rejected before DNS: {host}")

    def requester(url, addresses, timeout):
        requested.append((url, tuple(str(item) for item in addresses), timeout))
        return {"status": 302, "headers": {"location": "https://127.0.0.1/admin"}, "body": b""}

    result = mod.fetch_url_live(
        "https://public.example/start", resolver=resolver, requester=requester
    )

    assert result["ok"] is False
    assert result["error"] == "destination is not public"
    assert [item[0] for item in requested] == ["https://public.example/start"]


def test_dns_answers_are_pinned_and_mixed_public_private_answers_are_rejected():
    requested = []

    def requester(url, addresses, timeout):
        requested.append((url, tuple(str(item) for item in addresses), timeout))
        return {"status": 200, "headers": {}, "body": b"ok"}

    mixed = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
    ]
    result = mod.fetch_url_live(
        "https://public.example/start", resolver=mixed, requester=requester
    )

    assert result["ok"] is False
    assert result["error"] == "destination is not public"
    assert requested == []


def test_dns_resolution_consumes_total_deadline_before_request(monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    requested = []

    def resolve_before_deadline(call, _deadline):
        resolved = call()
        clock["now"] = 10.2
        return resolved

    monkeypatch.setattr(mod, "_call_before_deadline", resolve_before_deadline)
    result = mod.fetch_url_live(
        "https://public.example/",
        timeout=0.1,
        resolver=lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        requester=lambda *_args, **_kwargs: requested.append(True),
    )

    assert result["ok"] is False
    assert result["error"] == "fetch timeout"
    assert requested == []


def test_bounded_resolver_pool_caps_stuck_running_and_queued_work():
    release = threading.Event()
    started = []
    started_lock = threading.Lock()
    pool = mod._BoundedResolverPool(max_workers=2, max_queue=3, name="test-dns")
    outcomes = []

    def stuck():
        with started_lock:
            started.append(threading.current_thread().name)
        release.wait()
        return "late"

    def caller():
        try:
            pool.call(stuck, time.monotonic() + 0.05)
        except Exception as exc:
            outcomes.append(str(exc))

    callers = [threading.Thread(target=caller) for _ in range(20)]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(timeout=0.5)

    assert all(not thread.is_alive() for thread in callers)
    assert len(started) == 2
    assert pool.capacity == 5
    assert pool.outstanding <= pool.capacity
    assert len(outcomes) == 20
    assert set(outcomes) <= {"fetch timeout", "resolver overloaded"}
    assert len(pool.worker_threads) == 2
    assert all(thread.daemon for thread in pool.worker_threads)

    before = time.monotonic()
    pool.shutdown(wait=False)
    assert time.monotonic() - before < 0.05
    release.set()


def test_resolver_deadline_uses_injected_clock_and_overload_is_deterministic():
    clock = {"now": 10.0}
    release = threading.Event()
    pool = mod._BoundedResolverPool(
        max_workers=1, max_queue=1, name="fake-clock-dns",
        clock=lambda: clock["now"],
    )
    first_started = threading.Event()

    def stuck():
        first_started.set()
        release.wait()

    first = threading.Thread(target=lambda: pytest.raises(TimeoutError, pool.call, stuck, 10.01))
    first.start()
    assert first_started.wait(0.2)
    queued = threading.Thread(target=lambda: pytest.raises(TimeoutError, pool.call, stuck, 10.01))
    queued.start()
    with pytest.raises(RuntimeError, match="resolver overloaded"):
        pool.call(stuck, 10.01)
    clock["now"] = 10.02
    first.join(0.2)
    queued.join(0.2)
    assert not first.is_alive() and not queued.is_alive()
    release.set()
    pool.shutdown(wait=False)


def test_live_fetch_converts_resolver_overload_to_fail_closed_result(monkeypatch):
    monkeypatch.setattr(
        mod, "_call_before_deadline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("resolver overloaded")),
    )

    result = mod.fetch_url_live("https://public.example/")

    assert result["ok"] is False
    assert result["error"] == "resolver overloaded"


def test_pinned_request_caps_validated_address_attempts(monkeypatch):
    attempted = []

    class FailingConnection:
        def __init__(self, _host, _port, address, _timeout):
            attempted.append(address)

        def request(self, *_args, **_kwargs):
            raise OSError("no route")

        def close(self):
            pass

    monkeypatch.setattr(mod, "_PinnedHTTPSConnection", FailingConnection)
    addresses = {ipaddress.ip_address(f"93.184.216.{index}") for index in range(30, 35)}

    with pytest.raises(OSError):
        mod._request_pinned("https://example.com/", addresses, 10.0)

    assert len(attempted) == mod._MAX_PINNED_ADDRESS_ATTEMPTS == 2


def test_build_digest_source_set_explicitly_binds_direct_and_native_boundaries():
    required = {
        "tools/send_message_tool.py",
        "gateway/platforms/base.py",
        "gateway/relay/adapter.py",
        "plugins/platforms/google_chat/adapter.py",
        "plugins/platforms/whatsapp/adapter.py",
        "plugins/platforms/photon/adapter.py",
        "plugins/platforms/discord/adapter.py",
        "plugins/platforms/telegram/adapter.py",
        "plugins/platforms/slack/adapter.py",
        "gateway/delivery.py",
        "gateway/platform_registry.py",
        "gateway/platforms/qqbot/adapter.py",
        "cron/scheduler.py",
        "gateway/run.py",
    }
    assert required <= set(mod.GATE_BUILD_SOURCE_PATHS)
    full_digest = mod._gate_build_digest(source_paths=mod.GATE_BUILD_SOURCE_PATHS)
    assert full_digest == mod._gate_build_digest()
    omitted = tuple(
        path for path in mod.GATE_BUILD_SOURCE_PATHS
        if path != "tools/send_message_tool.py"
    )
    assert mod._gate_build_digest(source_paths=omitted) != full_digest


def test_security_source_manifest_rejects_omitted_required_path():
    omitted = tuple(
        path for path in mod.GATE_BUILD_SOURCE_PATHS
        if path != "gateway/delivery.py"
    )
    with pytest.raises(RuntimeError, match="manifest mismatch"):
        mod._validate_gate_build_manifest(omitted, root=mod._gate_repo_root())


def test_security_source_inventory_rejects_added_transport_file(tmp_path):
    added = tmp_path / "plugins" / "platforms" / "new_platform" / "transport.py"
    added.parent.mkdir(parents=True)
    added.write_text("async def publish(payload): pass\n")

    with pytest.raises(RuntimeError, match="unreviewed.*transport.py"):
        mod._validate_gate_build_manifest((), root=tmp_path)


def test_security_source_reader_rejects_symlink_path(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("VALUE = 1\n")
    link = tmp_path / "gateway" / "delivery.py"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    with pytest.raises(RuntimeError, match="symlink"):
        mod._read_security_source_bytes(tmp_path, "gateway/delivery.py")


def test_loaded_runtime_snapshot_rejects_disk_modified_after_capture(tmp_path):
    source = tmp_path / "gateway" / "delivery.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n")
    snapshots = mod._capture_source_snapshots(
        root=tmp_path, source_paths=("gateway/delivery.py",),
    )
    source.write_text("VALUE = 2\n")

    with pytest.raises(RuntimeError, match="disk drift"):
        mod._assert_source_snapshots(
            snapshots, root=tmp_path, check_loaded_modules=False,
        )


def test_loaded_module_identity_rejects_module_file_mismatch(tmp_path):
    source = tmp_path / "gateway" / "delivery.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n")
    snapshot = mod._capture_source_snapshots(
        root=tmp_path, source_paths=("gateway/delivery.py",),
    )["gateway/delivery.py"]
    fake_module = SimpleNamespace(
        __file__=str(tmp_path / "elsewhere.py"),
        __loader__=SimpleNamespace(get_data=lambda _path: snapshot.source_bytes),
        __spec__=SimpleNamespace(origin=str(tmp_path / "elsewhere.py")),
    )

    with pytest.raises(RuntimeError, match="module path mismatch"):
        mod._assert_loaded_module_identity(
            "gateway.delivery", fake_module, snapshot,
        )


def test_runtime_inventory_validation_rejects_ast_violation(tmp_path, monkeypatch):
    source = tmp_path / "gateway" / "delivery.py"
    source.parent.mkdir(parents=True)
    source.write_text("async def bypass(): pass\n")
    snapshots = mod._capture_source_snapshots(
        root=tmp_path, source_paths=("gateway/delivery.py",),
    )
    monkeypatch.setattr(
        "outbound_transport_inventory.scan_terminal_transport_inventory",
        lambda *_args, **_kwargs: ["gateway/delivery.py:1:bypass:send"],
    )

    with pytest.raises(RuntimeError, match="terminal transport inventory violation"):
        mod._validate_terminal_transport_inventory(snapshots)


def test_bare_oauth_callback_requires_narrow_configured_status_exception():
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ]
    requester = lambda *_args, **_kwargs: {"status": 400, "headers": {}, "body": b""}
    url = "https://auth.example/oauth/callback"

    default_result = mod.fetch_url_live(url, resolver=resolver, requester=requester)
    excepted_result = mod.fetch_url_live(
        url,
        resolver=resolver,
        requester=requester,
        status_exceptions=(
            {"host": "auth.example", "path": "/oauth/callback", "statuses": [400]},
        ),
    )

    assert default_result["ok"] is False
    assert default_result["status"] == 400
    assert excepted_result["ok"] is True


def test_gate_passes_only_configured_status_exceptions_to_live_fetch(monkeypatch):
    captured = {}

    def fake_fetch(url, timeout, *, status_exceptions=()):
        captured.update(url=url, timeout=timeout, status_exceptions=status_exceptions)
        return {"ok": True, "status": 400, "final_url": url}

    monkeypatch.setattr(mod, "fetch_url_live", fake_fetch)
    exception = {"host": "auth.example", "path": "/oauth/callback", "statuses": [400]}
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content="See https://auth.example/oauth/callback",
        metadata={},
        settings={
            "protected_targets": ["telegram:paul"],
            "status_exceptions": [exception],
            "fetch_timeout_seconds": 2,
        },
    )

    assert result == {"action": "allow"}
    assert captured["status_exceptions"] == (exception,)


def test_success_claim_without_receipt_is_stamped_unverified():
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content="The LinkedIn flow is fixed.",
        metadata={"_hermes_session_id": "s1"},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert result["action"] == "rewrite"
    assert result["reason"] == "claim_receipt_missing"
    assert result["content"].startswith("UNVERIFIED")
    assert "Missing: a structured receipt from an allowlisted verifier" in result["content"]
    assert "The LinkedIn flow is fixed." in result["content"]


def test_arbitrary_terminal_output_cannot_forge_a_verifier_receipt():
    mod.clear_receipts_for_tests()
    fake = "BUILD_ID=fake journey=PASS authorize=PASS post=PASS public_url=PASS fetch=PASS"
    mod.record_tool_result(
        session_id="s1",
        turn_id="turn-1",
        tool_name="terminal",
        args={"command": "printf '" + fake + "'"},
        result=fake,
        status="success",
    )
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content=(
            "LinkedIn publishing is fixed.\n\n"
            "Receipt: linkedin-public-post-journey\n"
            f"Passing output: {fake}"
        ),
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )

    assert result["action"] == "rewrite"
    assert result["reason"] == "claim_receipt_missing"


def test_malformed_structured_receipt_is_rejected_without_breaking_post_tool_hook():
    mod.clear_receipts_for_tests()
    mod.record_tool_result(
        session_id="s1",
        turn_id="turn-1",
        tool_name="linkedin_journey_verifier",
        status="success",
        allowed_verifiers={
            "linkedin-verifier": {
                "tool_name": "linkedin_journey_verifier",
                "check_id": "linkedin-public-post-journey",
                "journey_id": "linkedin-public-post-journey",
            }
        },
        result={
            "outbound_verifier_receipt": {
                "check_id": "linkedin-public-post-journey",
                "verifier_id": "linkedin-verifier",
                "journey_id": "linkedin-public-post-journey",
                "exit_status": "not-an-integer",
                "session_id": "s1",
                "turn_id": "turn-1",
                "output_digest": "a" * 64,
            }
        },
    )

    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content="LinkedIn publishing is fixed.",
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert result["reason"] == "claim_receipt_missing"


def test_single_same_turn_linkedin_receipt_still_fails_closed():
    mod.clear_receipts_for_tests()
    record_verifier_receipt()
    content = "LinkedIn publishing is verified."
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content=content,
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda url: {"ok": True, "status": 200, "final_url": url},
    )
    assert result["action"] == "rewrite"
    assert result["reason"] == "linkedin_journey_incomplete"


def test_receipt_rejects_stale_timestamp_fake_runtime_and_unrecomputed_digest():
    mod.clear_receipts_for_tests()
    output = "ratchet=PASS"
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="ratchet_verifier", status="success",
        allowed_verifiers={"ratchet": {
            "tool_name": "ratchet_verifier", "check_id": "defect-ratchet",
            "journey_id": "defect-ratchet", "command_id": "defect-ratchet-v1",
            "passing_output": output,
        }},
        result={"outbound_verifier_receipt": {
            "verifier_id": "ratchet", "check_id": "defect-ratchet",
            "journey_id": "defect-ratchet", "command_id": "defect-ratchet-v1",
            "session_id": "s1", "turn_id": "turn-1", "exit_status": 0,
            "build_id": "FAKE", "runtime_id": "FAKE",
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "output": output, "output_digest": "a" * 64,
        }},
    )
    result = mod.gate_outbound_message(
        platform="telegram", chat_id="paul", content="The defect is fixed.",
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1",
                  "_outbound_claim_check_id": "defect-ratchet"},
        settings=settings("telegram:paul"), fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert result["reason"] == "claim_receipt_missing"


def test_receipt_requires_allowlisted_canonical_command_id():
    mod.clear_receipts_for_tests()
    output = "ratchet=PASS"
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="ratchet_verifier", status="success",
        allowed_verifiers={"ratchet": {
            "tool_name": "ratchet_verifier", "check_id": "defect-ratchet",
            "journey_id": "defect-ratchet", "command_id": "defect-ratchet-v1",
            "passing_output": output,
        }},
        result={"outbound_verifier_receipt": {
            "verifier_id": "ratchet", "check_id": "defect-ratchet",
            "journey_id": "defect-ratchet", "command_id": "caller-invented-command",
            "session_id": "s1", "turn_id": "turn-1", "exit_status": 0,
            "build_id": mod.current_build_id(), "runtime_id": mod.current_runtime_id(),
            "timestamp": datetime.now(timezone.utc).isoformat(), "output": output,
            "output_digest": hashlib.sha256(output.encode()).hexdigest(),
        }},
    )
    assert mod._receipt_for_turn("s1", "turn-1", "defect-ratchet") is None


def test_linkedin_receipt_public_url_is_validated_and_fetched_not_self_asserted():
    mod.clear_receipts_for_tests()
    record_verifier_receipt(public_fetch=True, public_url="javascript:fake")
    fetched = []
    result = mod.gate_outbound_message(
        platform="telegram", chat_id="paul", content="LinkedIn is verified.",
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda url: fetched.append(url) or {"ok": True, "status": 200, "final_url": url},
    )
    assert result["reason"] == "linkedin_journey_incomplete"
    assert fetched == []


def test_stale_receipt_from_previous_turn_is_rejected():
    mod.clear_receipts_for_tests()
    record_verifier_receipt(turn_id="old-turn")
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content=(
            "The defect is fixed.\n\nReceipt: defect-ratchet\n"
            "Passing output: BUILD_ID=old ratchet=PASS"
        ),
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "new-turn"},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert result["action"] == "rewrite"
    assert result["reason"] == "claim_receipt_missing"
    assert result["content"].startswith("UNVERIFIED")


def test_linkedin_success_requires_full_public_post_journey_markers():
    mod.clear_receipts_for_tests()
    record_verifier_receipt(public_fetch=False, public_url="")
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content=(
            "LinkedIn is working.\n\nReceipt: linkedin-public-post-journey\n"
            "Passing output: BUILD_ID=build-123 journey=PASS authorize=PASS post=PASS"
        ),
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert result["action"] == "rewrite"
    assert result["reason"] == "linkedin_journey_incomplete"
    assert "independent same-turn LinkedIn authorization" in result["content"]


def _dedicated_ratchet_config(expected_args=None):
    return {"ratchet": {
        "tool_name": "defect_ratchet_verifier",
        "dedicated_verifier": True,
        "check_id": "defect-ratchet",
        "journey_id": "defect-ratchet",
        "command_id": "defect-ratchet-v1",
        "args": expected_args or {"ratchet": "defect-ratchet", "mode": "live"},
        "passing_output": {"ratchet": "PASS"},
    }}


def _ratchet_result(**overrides):
    receipt = {
        "verifier_id": "ratchet",
        "check_id": "defect-ratchet",
        "journey_id": "defect-ratchet",
        "command_id": "defect-ratchet-v1",
        "exit_status": 0,
        "session_id": "s1",
        "turn_id": "turn-1",
        "output": {"ratchet": "PASS"},
        "build_id": "caller-controlled-build",
        "runtime_id": "caller-controlled-runtime",
        "timestamp": "2000-01-01T00:00:00+00:00",
        "output_digest": "caller-controlled-digest",
    }
    receipt.update(overrides)
    return {"outbound_verifier_receipt": receipt}


def _legacy_acceptable_ratchet_result(**overrides):
    return _ratchet_result(
        build_id=mod.current_build_id(),
        runtime_id=mod.current_runtime_id(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        output_digest=hashlib.sha256(b'{"ratchet":"PASS"}').hexdigest(),
        **overrides,
    )


def test_receipt_rejects_mismatched_actual_invocation_args():
    mod.clear_receipts_for_tests()
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="defect_ratchet_verifier",
        args={"ratchet": "NOT-CANONICAL", "mode": "live"}, status="success",
        allowed_verifiers=_dedicated_ratchet_config(),
        result=_legacy_acceptable_ratchet_result(),
    )
    assert mod._receipt_for_turn("s1", "turn-1", "defect-ratchet") is None


def test_receipt_rejects_generic_terminal_even_when_allowlisted_as_verifier():
    mod.clear_receipts_for_tests()
    config = _dedicated_ratchet_config({"command": "run-ratchet"})
    config["ratchet"]["tool_name"] = "terminal"
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="terminal",
        args={"command": "run-ratchet"}, status="success",
        allowed_verifiers=config, result=_legacy_acceptable_ratchet_result(),
    )
    assert mod._receipt_for_turn("s1", "turn-1", "defect-ratchet") is None


def test_valid_receipt_is_host_stamped_and_ignores_self_asserted_identity_fields():
    mod.clear_receipts_for_tests()
    before = time.time()
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="defect_ratchet_verifier",
        args={"ratchet": "defect-ratchet", "mode": "live"}, status="success",
        allowed_verifiers=_dedicated_ratchet_config(), result=_ratchet_result(),
    )
    receipt = mod._receipt_for_turn("s1", "turn-1", "defect-ratchet")
    assert receipt is not None
    assert receipt.build_id == mod.current_build_id() != "caller-controlled-build"
    assert receipt.runtime_id == mod.current_runtime_id() != "caller-controlled-runtime"
    assert receipt.timestamp_epoch >= before
    assert receipt.timestamp != "2000-01-01T00:00:00+00:00"
    assert receipt.output_digest == hashlib.sha256(b'{"ratchet":"PASS"}').hexdigest()

def test_valid_dedicated_exact_invocation_receipt_allows_non_linkedin_claim():
    mod.clear_receipts_for_tests()
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="defect_ratchet_verifier",
        args={"ratchet": "defect-ratchet", "mode": "live"}, status="success",
        allowed_verifiers=_dedicated_ratchet_config(), result=_ratchet_result(),
    )
    decision = mod.gate_outbound_message(
        platform="telegram", chat_id="paul", content="The defect is fixed.",
        metadata={
            "_hermes_session_id": "s1", "_hermes_turn_id": "turn-1",
            "_outbound_claim_check_id": "defect-ratchet",
        },
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert decision == {"action": "allow"}


def test_linkedin_completion_fails_closed_without_independent_three_event_journey():
    mod.clear_receipts_for_tests()
    config = _dedicated_ratchet_config()
    config["ratchet"].update({
        "check_id": "linkedin-public-post-journey",
        "journey_id": "linkedin-public-post-journey",
    })
    result = _legacy_acceptable_ratchet_result(
        check_id="linkedin-public-post-journey",
        journey_id="linkedin-public-post-journey",
        public_url="https://www.linkedin.com/feed/update/urn:li:activity:123",
    )
    mod.record_tool_result(
        session_id="s1", turn_id="turn-1", tool_name="defect_ratchet_verifier",
        args={"ratchet": "defect-ratchet", "mode": "live"}, status="success",
        allowed_verifiers=config, result=result,
    )
    decision = mod.gate_outbound_message(
        platform="telegram", chat_id="paul", content="LinkedIn publishing is verified.",
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda url: {"ok": True, "status": 200, "final_url": url},
    )
    assert decision["action"] == "rewrite"
    assert decision["reason"] == "linkedin_journey_incomplete"

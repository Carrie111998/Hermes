from __future__ import annotations

import importlib
import socket


mod = importlib.import_module("plugins.outbound_message_gate")



def settings(*targets: str):
    return {
        "protected_targets": list(targets),
        "success_terms": [
            "fixed", "working", "resolved", "live", "ready", "deployed", "verified"
        ],
    }


def record_verifier_receipt(*, turn_id="turn-1", public_fetch=True, public_url="https://linkedin.example/post/1"):
    mod.record_tool_result(
        session_id="s1",
        turn_id=turn_id,
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
                "exit_status": 0,
                "build_id": "build-123",
                "runtime_id": "pid-456",
                "session_id": "s1",
                "turn_id": turn_id,
                "timestamp": "2026-08-27T12:00:00Z",
                "output_digest": "a" * 64,
                "public_url": public_url,
                "public_fetch": {"ok": public_fetch, "status": 200},
            }
        },
    )


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


def test_same_turn_receipt_must_match_passing_tool_output_and_live_build():
    mod.clear_receipts_for_tests()
    record_verifier_receipt()
    content = "LinkedIn publishing is verified."
    result = mod.gate_outbound_message(
        platform="telegram",
        chat_id="paul",
        content=content,
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "turn-1"},
        settings=settings("telegram:paul"),
        fetcher=lambda _url: {"ok": True, "status": 200},
    )
    assert result == {"action": "allow"}


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
    assert "public URL and passing fresh public fetch" in result["content"]

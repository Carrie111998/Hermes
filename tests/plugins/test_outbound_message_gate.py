from __future__ import annotations

import importlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


mod = importlib.import_module("plugins.outbound_message_gate")


class _PolicyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        if self.path == "/oauth/callback":
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        del format, args


def _policy_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PolicyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def settings(*targets: str):
    return {
        "protected_targets": list(targets),
        "success_terms": [
            "fixed", "working", "resolved", "live", "ready", "deployed", "verified"
        ],
    }


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
    assert "https://dead.example/b" not in result["content"]
    assert "dead.example /b" in result["content"]
    assert "HTTP 404" in result["content"]


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
    assert "unsupported or malformed URL" in result["content"]


def test_live_fetch_rejects_local_private_link_local_metadata_and_tailnet_without_io(monkeypatch):
    attempted = []

    def forbidden_open(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("network I/O must not start")

    monkeypatch.setattr(mod, "urlopen", forbidden_open)
    urls = (
        "http://127.0.0.1/admin",
        "http://10.0.0.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/tailnet",
        "http://[::1]/admin",
    )

    for url in urls:
        result = mod.fetch_url_live(url)
        assert result["ok"] is False, url
        assert result["error"] == "destination is not public", url
    assert attempted == []


def test_bare_oauth_callback_requires_narrow_configured_status_exception():
    server, thread = _policy_server()
    try:
        result = mod.fetch_url_live(
            f"http://127.0.0.1:{server.server_port}/oauth/callback"
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result["ok"] is True
    assert result["status"] == 400


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
    assert "Missing: a named same-turn ratchet or journey receipt" in result["content"]
    assert "The LinkedIn flow is fixed." in result["content"]


def test_same_turn_receipt_must_match_passing_tool_output_and_live_build():
    mod.clear_receipts_for_tests()
    mod.record_tool_result(
        session_id="s1",
        turn_id="turn-1",
        tool_name="terminal",
        args={"command": "npm run verify:linkedin-public-post-journey"},
        result="BUILD_ID=build-123 journey=PASS authorize=PASS post=PASS public_url=PASS fetch=PASS",
        status="success",
    )
    content = (
        "LinkedIn publishing is verified.\n\n"
        "Receipt: linkedin-public-post-journey\n"
        "Passing output: BUILD_ID=build-123 journey=PASS authorize=PASS post=PASS public_url=PASS fetch=PASS"
    )
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
    mod.record_tool_result(
        session_id="s1",
        turn_id="old-turn",
        tool_name="terminal",
        args={"command": "npm run ratchet"},
        result="BUILD_ID=old ratchet=PASS",
        status="success",
    )
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
    mod.record_tool_result(
        session_id="s1",
        turn_id="turn-1",
        tool_name="terminal",
        args={"command": "npm run verify:linkedin-public-post-journey"},
        result="BUILD_ID=build-123 journey=PASS authorize=PASS post=PASS",
        status="success",
    )
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
    assert "public_url=PASS" in result["content"]
    assert "fetch=PASS" in result["content"]

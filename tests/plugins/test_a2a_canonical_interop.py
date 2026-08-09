"""F3 (Wave 10) — A2A canonical interop: outbound/inbound round-trip proof.

Proves the two halves of the A2A plugin agree on the JSON-RPC method space:

1. Round-trip proof — every outbound method string in ``tools.py`` (extracted
   by parsing the source; no network code is imported-and-called) must be
   accepted by the inbound adapter's dispatch table ``_method_info`` with a
   non-empty canonical operation.

2. Canonical v1.0 coverage — the full PascalCase method set from A2A v1.0
   §5.3/§9.4 maps to the expected canonical operations, and every legacy
   slash alias resolves to the SAME operation as its PascalCase twin
   (parity matrix).

3. Negative coverage — unknown methods resolve to ``('', False)`` and the
   JSON-RPC dispatch layer answers them with error code -32601
   (MethodNotFound).

4. Drift pin — ``tools.py`` may not contain any method-shaped string outside
   the accepted mapping, so future outbound methods that the inbound adapter
   cannot understand fail the build instead of failing at a peer.

Hermetic by construction: no network, no port binding (never 9900), no real
HTTP. Handler-level dispatch tests drive ``A2ARequestHandler.do_POST`` on a
``__new__``-constructed handler backed by in-memory buffers, so the full
do_POST code path runs without a socket.

Note: ``GetExtendedAgentCard`` is a GET endpoint on the HTTP surface
(``.well-known/agent.json`` served by ``do_GET``), NOT a JSON-RPC method.
It is therefore intentionally absent from the dispatch table and excluded
from every round-trip assertion in this file.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import re
from email.message import Message
from types import SimpleNamespace

import pytest

from plugins.platforms.a2a import protocol
from plugins.platforms.a2a.adapter import A2ARequestHandler, _method_info

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS_PATH = REPO_ROOT / "plugins" / "platforms" / "a2a" / "tools.py"

# A2A v1.0 §5.3 canonical JSON-RPC methods (PascalCase) -> canonical operation.
CANONICAL_V1 = {
    "SendMessage": "send",
    "SendStreamingMessage": "stream",
    "GetTask": "get",
    "ListTasks": "list",
    "CancelTask": "cancel",
    "SubscribeToTask": "subscribe",
    "CreateTaskPushNotificationConfig": "push_create",
    "GetTaskPushNotificationConfig": "push_get",
    "ListTaskPushNotificationConfigs": "push_list",
    "DeleteTaskPushNotificationConfig": "push_delete",
}

# Legacy slash aliases -> their PascalCase twin. push_create carries three
# historical spellings; all must resolve identically.
LEGACY_TWINS = {
    "message/send": "SendMessage",
    "message/stream": "SendStreamingMessage",
    "tasks/get": "GetTask",
    "tasks/list": "ListTasks",
    "tasks/cancel": "CancelTask",
    "tasks/subscribe": "SubscribeToTask",
    "tasks/pushNotificationConfig/create": "CreateTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/set": "CreateTaskPushNotificationConfig",
    "tasks/pushNotification/set": "CreateTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/get": "GetTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/list": "ListTaskPushNotificationConfigs",
    "tasks/pushNotificationConfig/delete": "DeleteTaskPushNotificationConfig",
}

# Every string the inbound adapter accepts (both spellings) — the allowed
# universe for the tools.py drift pin.
ACCEPTED_METHODS = set(CANONICAL_V1) | set(LEGACY_TWINS)

# Method-shaped literals: PascalCase identifiers with a lowercase letter
# (v1 names) or slash paths inside the A2A JSON-RPC namespaces. The slash
# namespace is restricted to message/ and tasks/ — the ONLY method prefixes
# in the A2A spec — so MIME types like "application/json" never match.
_PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_SLASH = re.compile(r"^(message|tasks)(/[A-Za-z][A-Za-z0-9]*)+$")

# PascalCase string constants in tools.py that are provably NOT JSON-RPC
# methods (HTTP header names). Documented, deterministic exclusion — extend
# with justification only.
_PASCAL_NON_METHOD = {"Authorization"}


def _tools_source() -> str:
    return TOOLS_PATH.read_text(encoding="utf-8")


def _tools_tree() -> ast.Module:
    return ast.parse(_tools_source())


def _docstring_lines(tree: ast.Module) -> set[int]:
    """Line numbers of module/function/class docstrings (module docs are
    prose, not wire data — the module docstring legitimately names the
    legacy ``message/send`` spelling in a sentence)."""
    return {
        node.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def extract_outbound_methods() -> set[str]:
    """Structural extraction: the ``"method"`` value of every JSON-RPC request
    dict literal in tools.py. Parses the source; never imports-and-calls the
    network code."""
    found: set[str] = set()
    for node in ast.walk(_tools_tree()):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "method"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
    return found


def extract_method_shaped_constants() -> set[str]:
    """Conservative drift net: every non-docstring string constant in
    tools.py whose shape resembles a JSON-RPC method name (PascalCase
    identifier or message//tasks/ slash path). Documented non-method
    constants (HTTP headers) are excluded via _PASCAL_NON_METHOD."""
    tree = _tools_tree()
    skip = _docstring_lines(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in skip:
                continue
            s = node.value
            if s in _PASCAL_NON_METHOD:
                continue
            if (_PASCAL.match(s) and any(c.islower() for c in s)) or _SLASH.match(s):
                found.add(s)
    return found


def _bare_adapter():
    """Same construction convention as tests/plugins/test_a2a_plugin.py."""
    from plugins.platforms.a2a.adapter import A2AAdapter
    from gateway.config import PlatformConfig
    return A2AAdapter(PlatformConfig(enabled=True))


def _local_only_env(monkeypatch) -> None:
    """Force localhost-only security mode: authenticate() identifies the
    caller by socket address and never requires a bearer token."""
    monkeypatch.delenv("A2A_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
    monkeypatch.delenv("A2A_TRUSTED_PEERS", raising=False)


def _fake_handler(monkeypatch, body: dict):
    """A2ARequestHandler bound to in-memory streams — the full do_POST code
    path runs without a socket, so no port (9900 or otherwise) is bound and
    no HTTP connection is made."""
    _local_only_env(monkeypatch)
    adapter = _bare_adapter()
    adapter._rate_limiter = SimpleNamespace(allow=lambda identity: True)
    handler = A2ARequestHandler.__new__(A2ARequestHandler)
    handler.server = SimpleNamespace(adapter=adapter)
    handler.client_address = ("127.0.0.1", 0)
    payload = json.dumps(body).encode("utf-8")
    headers = Message()
    headers["Content-Length"] = str(len(payload))
    handler.headers = headers
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.path = "/"
    # requestline is normally set by BaseHTTPRequestHandler.handle_one_request
    # after parsing the request line; do_POST's logging/error paths read it,
    # so the in-memory construction must provide it too.
    handler.requestline = "POST / HTTP/1.1"
    return handler


def _posted(monkeypatch, body: dict) -> tuple[int, dict]:
    handler = _fake_handler(monkeypatch, body)
    handler.do_POST()  # noqa: N802 — stdlib naming
    handler.wfile.seek(0)
    raw = handler.wfile.read().decode("utf-8")
    head, _, payload = raw.partition("\r\n\r\n")
    status = int(head.split()[1])
    return status, json.loads(payload) if payload else {}


# --------------------------------------------------------------------------
# 1. Round-trip proof: every outbound method must be inbound-accepted
# --------------------------------------------------------------------------

class TestOutboundInboundRoundTrip:
    def test_outbound_method_extraction_finds_sendmessage(self):
        """Pin the extraction itself: today tools.py sends exactly one method."""
        assert extract_outbound_methods() == {"SendMessage"}

    def test_every_outbound_method_is_inbound_accepted(self):
        """Round-trip invariant: whatever tools.py puts on the wire must be
        understood by our own inbound adapter (non-empty operation)."""
        outbound = extract_outbound_methods()
        assert outbound, "tools.py must send at least one JSON-RPC method"
        for method in outbound:
            operation, _ = _method_info(method)
            assert operation, (
                f"outbound method {method!r} from tools.py is not accepted "
                f"by the inbound adapter _method_info"
            )

    def test_get_extended_agent_card_is_not_a_jsonrpc_method(self):
        """GetExtendedAgentCard is served by do_GET on the HTTP surface
        (.well-known/agent.json); it is not part of the JSON-RPC dispatch
        table and must not accidentally be treated as one."""
        assert _method_info("GetExtendedAgentCard") == ("", False)


# --------------------------------------------------------------------------
# Canonical v1.0 PascalCase set (A2A v1.0 §5.3) -> expected operations
# --------------------------------------------------------------------------

class TestCanonicalV1Mapping:
    @pytest.mark.parametrize(
        ("method", "operation"), sorted(CANONICAL_V1.items())
    )
    def test_canonical_method_maps_to_operation(self, method, operation):
        assert _method_info(method) == (operation, True)

    def test_canonical_set_covers_exactly_ten_operations(self):
        assert set(CANONICAL_V1.values()) == {
            "send", "stream", "get", "list", "cancel", "subscribe",
            "push_create", "push_get", "push_list", "push_delete",
        }


# --------------------------------------------------------------------------
# Legacy alias parity matrix: every alias == its PascalCase twin's operation
# --------------------------------------------------------------------------

class TestLegacyAliasParity:
    @pytest.mark.parametrize(
        ("alias", "twin"), sorted(LEGACY_TWINS.items())
    )
    def test_alias_resolves_to_same_operation_as_pascal_twin(self, alias, twin):
        alias_op, alias_v1 = _method_info(alias)
        twin_op, twin_v1 = _method_info(twin)
        assert alias_op == twin_op
        assert alias_op != ""
        assert alias_v1 is False  # legacy spelling
        assert twin_v1 is True    # v1 canonical spelling

    def test_all_push_create_aliases_collapse_to_one_operation(self):
        aliases = [a for a, t in LEGACY_TWINS.items() if t == "CreateTaskPushNotificationConfig"]
        assert len(aliases) == 3
        ops = {_method_info(a)[0] for a in aliases} | {
            _method_info("CreateTaskPushNotificationConfig")[0]}
        assert ops == {"push_create"}


# --------------------------------------------------------------------------
# SSE streaming parity
# --------------------------------------------------------------------------

class TestStreamingParity:
    def test_send_streaming_message_maps_to_stream(self):
        assert _method_info("SendStreamingMessage") == ("stream", True)

    def test_message_stream_alias_maps_to_stream(self):
        assert _method_info("message/stream") == ("stream", False)

    def test_sse_pair_is_one_operation_two_spellings(self):
        pascal = _method_info("SendStreamingMessage")
        legacy = _method_info("message/stream")
        assert pascal[0] == legacy[0] == "stream"
        assert pascal[1] is not legacy[1]


# --------------------------------------------------------------------------
# Negative coverage: unknown methods
# --------------------------------------------------------------------------

class TestUnknownMethods:
    def test_unknown_method_returns_empty_operation(self):
        assert _method_info("Bogus/Op") == ("", False)

    def test_unknown_methods_variants_all_rejected(self):
        for method in ("tasks/destroy", "SendMessagee", "sendmessage", ""):
            assert _method_info(method) == ("", False), method

    def test_dispatch_table_contracts_unknown_to_method_not_found(self):
        """Dispatch-table contract: an empty operation is exactly the signal
        do_POST turns into ERR_METHOD_NOT_FOUND (-32601)."""
        operation, _ = _method_info("Bogus/Op")
        assert not operation
        payload = protocol.jsonrpc_error(7, protocol.ERR_METHOD_NOT_FOUND, "method not found: Bogus/Op")
        assert payload["error"]["code"] == -32601


# --------------------------------------------------------------------------
# JSON-RPC dispatch layer: do_POST driven without a socket
# --------------------------------------------------------------------------

class TestDispatchUnknownMethod:
    """The full inbound entry point for JSON-RPC is ``A2ARequestHandler.do_POST``,
    which requires a live HTTP server when reached through a socket. To keep
    this suite hermetic we drive the same method on a ``__new__``-constructed
    handler backed by in-memory rfile/wfile: identical code path (auth ->
    parse -> routing -> rate limit -> method dispatch), zero network. The
    -32601 branch is therefore proven at the real dispatch layer, not just
    the mapping table."""

    def test_do_post_unknown_method_returns_32601(self, monkeypatch):
        body = {"jsonrpc": "2.0", "id": 11, "method": "Bogus/Op", "params": {}}
        status, resp = _posted(monkeypatch, body)
        assert status == 200
        assert resp["id"] == 11
        assert resp["error"]["code"] == protocol.ERR_METHOD_NOT_FOUND == -32601
        assert "Bogus/Op" in resp["error"]["message"]

    def test_do_post_unknown_method_variant_32601(self, monkeypatch):
        body = {"jsonrpc": "2.0", "id": "abc", "method": "message/sendTo", "params": {}}
        status, resp = _posted(monkeypatch, body)
        assert status == 200
        assert resp["error"]["code"] == -32601

    def test_do_post_accepts_canonical_sendmessage_not_32601(self, monkeypatch):
        """Counter-case: a canonical v1 method passes the dispatch table and
        produces a task result — never a MethodNotFound. ``_await_reply`` is
        stubbed so the send handler completes synchronously without blocking
        on a (nonexistent) agent reply; dispatch routing is what's under
        test here, and no outbound HTTP is involved."""
        handler = _fake_handler(monkeypatch, {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "SendMessage",
            "params": {"message": protocol.text_message(protocol.ROLE_USER, "hello")},
        })
        handler.server.adapter._await_reply = lambda pending: (protocol.STATE_COMPLETED, "pong")
        handler.do_POST()  # noqa: N802
        handler.wfile.seek(0)
        raw = handler.wfile.read().decode("utf-8")
        _, _, payload = raw.partition("\r\n\r\n")
        resp = json.loads(payload)
        assert resp["id"] == 12
        assert "error" not in resp or resp["error"]["code"] != -32601
        assert "result" in resp


# --------------------------------------------------------------------------
# Drift pin: tools.py must not carry method strings the adapter rejects
# --------------------------------------------------------------------------

class TestOutboundDriftPin:
    def test_no_method_shaped_string_outside_accepted_mapping(self):
        """Parse-based pin: any future outbound method string added to
        tools.py that is absent from the inbound mapping fails here, forcing
        a deliberate mapping-table update instead of silent interop drift."""
        offenders = {
            s for s in extract_method_shaped_constants()
            if s not in ACCEPTED_METHODS
        }
        assert not offenders, (
            f"tools.py carries method-shaped strings unknown to the inbound "
            f"adapter _method_info: {sorted(offenders)} — extend the mapping "
            f"in plugins/platforms/a2a/adapter.py or update this pin"
        )

    def test_drift_pin_detects_a_bogus_outbound_method(self):
        """Self-test the pin logic: an injected unknown method string must be
        flagged, proving the net actually catches drift."""
        bogus = {"SendMessage", "Bogus/Op"}
        offenders = {s for s in bogus if s not in ACCEPTED_METHODS}
        assert offenders == {"Bogus/Op"}

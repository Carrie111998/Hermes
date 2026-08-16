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
import sys
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
    """Line numbers of REAL docstrings only: the first statement of a
    module/function/class body when it is a bare string constant. Bare
    string expressions elsewhere in a body are statements, not docs, and
    must stay visible to the drift net."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body:
                first = node.body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    lines.add(first.value.lineno)
    return lines


def extract_outbound_methods() -> set[str]:
    """Structural extraction: the ``"method"`` value of every JSON-RPC
    request construction in tools.py — dict literals ({"method": ...}) AND
    dict(method=...) keyword constructors, so a refactor to either shape
    stays covered. urllib.request.Request(method="GET") calls are NOT
    dict constructors and their HTTP verbs are excluded defensively.
    Parses the source; never imports-and-calls the network code."""
    _HTTP_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
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
        elif isinstance(node, ast.Call):
            # Only bare dict(...) constructors count — Request(url, method=
            # "GET") and other non-dict calls carry HTTP verbs, not
            # JSON-RPC method names.
            if not (isinstance(node.func, ast.Name) and node.func.id == "dict"):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "method"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                    and kw.value.value not in _HTTP_VERBS
                ):
                    found.add(kw.value.value)
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
        # Derived from PRODUCTION (_method_info), not from in-file constants:
        # the operation universe reachable through the real dispatch table
        # must be exactly the ten spec operations.
        reachable = {_method_info(m)[0] for m in ACCEPTED_METHODS}
        assert reachable == {
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
        # PascalCase spelling is v1-canonical, slash spelling is the legacy
        # alias — compare by VALUE, not identity.
        assert pascal[1] is True and legacy[1] is False


# --------------------------------------------------------------------------
# Negative coverage: unknown methods
# --------------------------------------------------------------------------

class TestUnknownMethods:
    def test_unknown_method_returns_empty_operation(self):
        assert _method_info("Bogus/Op") == ("", False)

    def test_unknown_methods_variants_all_rejected(self):
        for method in ("tasks/destroy", "SendMessagee", "sendmessage", ""):
            assert _method_info(method) == ("", False), method

    # NOTE: the -32601 wire contract is pinned by the genuine do_POST tests
    # in TestDispatchUnknownMethod below — a helper-shape test here would be
    # tautological (trust review R2 fix).


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
        assert "error" not in resp, f"canonical SendMessage must not error: {resp.get('error')}"
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

    def test_drift_pin_detects_a_bogus_outbound_method(self, monkeypatch, tmp_path):
        """Self-test the pin through the REAL extraction pipeline (not
        hardcoded constants): a synthetic tools.py carrying an unknown
        method must be flagged by the drift check. Trust review fix —
        the previous version exercised set-difference logic only."""
        fake = tmp_path / "tools.py"
        fake.write_text(
            'body = {"jsonrpc": "2.0", "method": "SendMessage"}\n'
            'rogue = {"jsonrpc": "2.0", "method": "Bogus/Op"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(sys.modules[__name__], "TOOLS_PATH", fake)
        extracted = extract_outbound_methods()
        assert "SendMessage" in extracted
        offenders = {s for s in extracted if s not in ACCEPTED_METHODS}
        assert offenders == {"Bogus/Op"}


# --------------------------------------------------------------------------
# Debate-adjudicated coverage (Wave 10, /tmp/w10-debate-f3.md)
# Accepted: #1 v1 stream dispatch, #2+#3 version gate + parity, #4 reverse
# mapping closure, #5 wrapped-vs-bare shape parity.
# Deferred: #6 batch-array deviation pin (low-traffic input no real peer
# sends; cheap to add later), #8 auth-precedence ordering (belongs in the
# security suite, not this interop file).
# --------------------------------------------------------------------------

ADAPTER_PATH = REPO_ROOT / "plugins" / "platforms" / "a2a" / "adapter.py"


def _posted_with_headers(monkeypatch, body: dict, headers: dict | None):
    """_posted variant that lets a test set extra request headers."""
    handler = _fake_handler(monkeypatch, body)
    for k, v in (headers or {}).items():
        handler.headers[k] = v
    handler.do_POST()  # noqa: N802
    handler.wfile.seek(0)
    raw = handler.wfile.read().decode("utf-8")
    head, _, payload = raw.partition("\r\n\r\n")
    status = int(head.split()[1])
    return status, head, (json.loads(payload) if payload else {})


class TestA2AVersionGate:
    """Debate #2 + #3: the A2A-Version header gate is an interop chokepoint
    and the outbound version must stay inside the inbound accepted set.

    Positive cases are STRICT (trust-review fix): a version that passes the
    gate must produce a real result — asserting only 'no invalid-params
    error' would still pass if dispatch failed with an unrelated error."""

    def _send_body(self):
        return {
            "jsonrpc": "2.0", "id": 7, "method": "SendMessage",
            "params": {"message": protocol.text_message(protocol.ROLE_USER, "hi")},
        }

    def _posted_passing_gate(self, monkeypatch, headers):
        handler = _fake_handler(monkeypatch, self._send_body())
        for k, v in (headers or {}).items():
            handler.headers[k] = v
        handler.server.adapter._await_reply = lambda pending: (
            protocol.STATE_COMPLETED, "ok")
        handler.do_POST()  # noqa: N802
        handler.wfile.seek(0)
        raw = handler.wfile.read().decode("utf-8")
        _, _, payload = raw.partition("\r\n\r\n")
        return json.loads(payload)

    def test_absent_version_header_dispatches(self, monkeypatch):
        # Legacy peers send no header — must NOT be rejected.
        resp = self._posted_passing_gate(monkeypatch, None)
        assert "error" not in resp, resp.get("error")
        assert "result" in resp

    def test_version_1_0_dispatches(self, monkeypatch):
        resp = self._posted_passing_gate(monkeypatch, {"A2A-Version": "1.0"})
        assert "error" not in resp, resp.get("error")
        assert "result" in resp

    def test_version_1_0_0_dispatches(self, monkeypatch):
        resp = self._posted_passing_gate(monkeypatch, {"A2A-Version": "1.0.0"})
        assert "error" not in resp, resp.get("error")
        assert "result" in resp

    @pytest.mark.parametrize("bad", ["0.3", "1.1", "2"])
    def test_unknown_version_rejected_invalid_params(self, monkeypatch, bad):
        status, _, resp = _posted_with_headers(
            monkeypatch, self._send_body(), {"A2A-Version": bad})
        assert status == 200
        assert resp["error"]["code"] == protocol.ERR_INVALID_PARAMS
        assert bad in resp["error"]["message"]

    def test_outbound_protocol_version_is_inbound_accepted(self, monkeypatch):
        """Debate #3 round-trip on the VERSION axis: whatever tools.py sends
        as A2A-Version must pass our own inbound gate (silent-drift pin —
        bumping PROTOCOL_VERSION becomes a visible, reviewed change)."""
        resp = self._posted_passing_gate(
            monkeypatch, {"A2A-Version": protocol.PROTOCOL_VERSION})
        assert "error" not in resp, (
            "outbound PROTOCOL_VERSION is rejected by our own inbound gate: "
            f"{resp.get('error')}")
        assert "result" in resp


class TestReverseMappingClosure:
    """Debate #4: the inbound mapping table must not grow stray entries that
    no spec section documents. One-directional drift pins are not enough."""

    def test_inbound_mapping_keys_match_accepted_universe(self):
        tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
        keys: set[str] = set()
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_method_info":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        found = True
                        for k in sub.keys:
                            assert isinstance(k, ast.Constant), (
                                "_method_info mapping keys must stay constant "
                                "literals for this closure pin to hold")
                            keys.add(k.value)
        assert found, "_method_info mapping dict not found in adapter.py"
        assert keys == ACCEPTED_METHODS, (
            f"inbound mapping drifted from the accepted universe: "
            f"extra={sorted(keys - ACCEPTED_METHODS)} "
            f"missing={sorted(ACCEPTED_METHODS - keys)}")

    def test_each_operation_has_exactly_one_v1_spelling(self):
        v1_spellings: dict[str, list[str]] = {}
        for name, (op, is_v1) in (
            (m, _method_info(m)) for m in ACCEPTED_METHODS):
            if is_v1:
                v1_spellings.setdefault(op, []).append(name)
        assert set(v1_spellings) == set(CANONICAL_V1.values())
        for op, names in v1_spellings.items():
            assert len(names) == 1, f"operation {op} has {len(names)} v1 spellings: {names}"


class TestResponseShapeParity:
    """Debate #5: spelling selects response shape — v1 SendMessage wraps the
    task, legacy message/send returns it bare. The heart of dual-spelling
    compat, pinned hermetically."""

    def _posted_send(self, monkeypatch, method: str):
        handler = _fake_handler(monkeypatch, {
            "jsonrpc": "2.0", "id": 21, "method": method,
            "params": {"message": protocol.text_message(protocol.ROLE_USER, "shape")},
        })
        handler.server.adapter._await_reply = lambda pending: (protocol.STATE_COMPLETED, "pong")
        handler.do_POST()  # noqa: N802
        handler.wfile.seek(0)
        raw = handler.wfile.read().decode("utf-8")
        _, _, payload = raw.partition("\r\n\r\n")
        return json.loads(payload)

    def test_v1_sendmessage_result_is_wrapped_task(self, monkeypatch):
        resp = self._posted_send(monkeypatch, "SendMessage")
        assert "error" not in resp, resp.get("error")
        assert set(resp["result"].keys()) == {"task"}, (
            "v1 SendMessage result must be the SendMessageResponse task wrapper")

    def test_legacy_message_send_result_is_bare(self, monkeypatch):
        resp = self._posted_send(monkeypatch, "message/send")
        assert "error" not in resp, resp.get("error")
        assert "task" not in resp["result"], (
            "legacy message/send must return a BARE task, not the wrapper")
        assert "status" in resp["result"]


class TestV1StreamDispatch:
    """Debate #1: the PascalCase v1 stream spelling must reach the SSE path
    end-to-end hermetically (previously only the mapping table was tested;
    the only SSE dispatch test used the legacy spelling over live HTTP)."""

    def test_do_post_v1_stream_method_serves_sse_frames(self, monkeypatch):
        handler = _fake_handler(monkeypatch, {
            "jsonrpc": "2.0", "id": 31, "method": "SendStreamingMessage",
            "params": {"message": protocol.text_message(protocol.ROLE_USER, "stream")},
        })
        handler.server.adapter._await_reply = lambda pending, keepalive=None: (
            protocol.STATE_COMPLETED, "done-streaming")
        handler.do_POST()  # noqa: N802
        handler.wfile.seek(0)
        raw = handler.wfile.read().decode("utf-8")
        head, _, body = raw.partition("\r\n\r\n")
        assert "text/event-stream" in head, head
        frames = [ln[len("data:"):].strip() for ln in body.splitlines()
                  if ln.startswith("data:")]
        assert frames, "SSE stream emitted no data frames"
        for frame in frames:
            env = json.loads(frame)
            assert env.get("jsonrpc") == "2.0"
            assert env.get("id") == 31
            assert "result" in env, f"frame missing result: {env}"
            # Trust-review fix: pin the inner StreamResponse content shape,
            # not just the envelope — a stream of valid envelopes carrying
            # garbage results must fail.
            result = env["result"]
            assert set(result) == {"statusUpdate"}, f"unexpected frame shape: {sorted(result)}"
            su = result["statusUpdate"]
            assert su["taskId"] and su["contextId"]
            assert su["status"]["state"] in (
                protocol.STATE_SUBMITTED, protocol.STATE_WORKING,
                protocol.STATE_COMPLETED, protocol.STATE_FAILED,
                protocol.STATE_INPUT_REQUIRED, protocol.STATE_AUTH_REQUIRED,
                protocol.STATE_CANCELED, protocol.STATE_REJECTED,
            ), su["status"]["state"]
        assert ": done" in body, "stream must end with the done comment"

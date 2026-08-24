from __future__ import annotations

import asyncio

import pytest

from tools.mcp_protocol import (
    LegacyProofError,
    ProtocolNegotiationState,
    ProtocolPolicy,
    StaleConnectionGenerationError,
    is_candidate_legacy_discovery_rejection,
    negotiate_protocol,
    normalize_protocol_policy,
)
from tools.mcp_tool import MCPServerTask


class _Err(Exception):
    def __init__(
        self,
        code: int,
        message: str = "Invalid request parameters",
        data: object = "",
    ) -> None:
        super().__init__(message)
        self.error = type(
            "ErrorData",
            (),
            {"code": code, "message": message, "data": data},
        )()


class _Session:
    def __init__(self, *, discover: object = "DISCOVER", initialize: object = "INITIALIZE") -> None:
        self._discover = discover
        self._initialize = initialize
        self.calls: list[str] = []
        self.protocol_version: str | None = None

    async def discover(self) -> object:
        self.calls.append("server/discover")
        if isinstance(self._discover, BaseException):
            raise self._discover
        self.protocol_version = "2026-07-28"
        return self._discover

    async def initialize(self) -> object:
        self.calls.append("initialize")
        if isinstance(self._initialize, BaseException):
            raise self._initialize
        self.protocol_version = "2025-11-25"
        return self._initialize


def _state(policy: ProtocolPolicy, generation: int = 1) -> ProtocolNegotiationState:
    return ProtocolNegotiationState(generation=generation, policy=policy)


def _run(coro):
    return asyncio.run(coro)


class TestProtocolPolicy:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("auto", ProtocolPolicy.AUTO),
            ("legacy", ProtocolPolicy.LEGACY),
            ("stateless", ProtocolPolicy.MODERN),
            ("2026-07-28", ProtocolPolicy.MODERN),
            ("  AUTO  ", ProtocolPolicy.AUTO),
        ],
    )
    def test_public_values_normalize_once(self, raw, expected):
        assert normalize_protocol_policy(raw) is expected

    def test_omitted_protocol_is_auto(self):
        assert normalize_protocol_policy() is ProtocolPolicy.AUTO

    @pytest.mark.parametrize("raw", [None, "", "modern", "handshake", "bogus", 42])
    def test_unknown_or_non_string_values_fail(self, raw):
        with pytest.raises((TypeError, ValueError), match="protocol"):
            normalize_protocol_policy(raw)

    def test_unknown_value_fails_before_http_preflight(self, monkeypatch):
        network_called = False

        async def fail_if_called(*_args, **_kwargs):
            nonlocal network_called
            network_called = True
            raise AssertionError("network preflight must not run")

        monkeypatch.setattr(MCPServerTask, "_preflight_content_type", fail_if_called)
        task = MCPServerTask("invalid-policy")
        with pytest.raises(ValueError, match="protocol"):
            _run(
                task.run(
                    {
                        "protocol": "bogus",
                        "url": "https://mcp.example.invalid/mcp",
                    }
                )
            )
        assert network_called is False


class TestModernFirstAuto:
    def test_auto_sends_discover_first_and_never_initializes_on_success(self):
        session = _Session()
        outcome = _run(negotiate_protocol(session, _state(ProtocolPolicy.AUTO), timeout=5))
        assert outcome.era == "modern"
        assert outcome.protocol_version == "2026-07-28"
        assert session.calls == ["server/discover"]

    def test_strict_modern_propagates_discover_failure_without_initialize(self):
        rejection = _Err(-32602)
        session = _Session(discover=rejection)
        with pytest.raises(_Err) as raised:
            _run(
                negotiate_protocol(
                    session,
                    _state(ProtocolPolicy.MODERN),
                    timeout=5,
                )
            )
        assert raised.value is rejection
        assert session.calls == ["server/discover"]

    def test_explicit_legacy_never_sends_discover(self):
        session = _Session()
        outcome = _run(
            negotiate_protocol(session, _state(ProtocolPolicy.LEGACY), timeout=5)
        )
        assert outcome.era == "legacy"
        assert session.calls == ["initialize"]


class TestBoundedLegacyProof:
    def test_canonical_legacy_shape_permits_one_initialize_proof(self):
        session = _Session(discover=_Err(-32602))
        state = _state(ProtocolPolicy.AUTO)
        outcome = _run(negotiate_protocol(session, state, timeout=5))
        assert outcome.era == "legacy"
        assert outcome.fallback_reason == "canonical-legacy-discover-rejection"
        assert state.legacy_proof_attempted is True
        assert session.calls == ["server/discover", "initialize"]

    def test_proof_failure_surfaces_discovery_and_initialize_errors(self):
        discovery = _Err(-32602)
        proof = _Err(-32000, "initialize failed", {"detail": "proof"})
        session = _Session(discover=discovery, initialize=proof)
        state = _state(ProtocolPolicy.AUTO)
        with pytest.raises(LegacyProofError) as raised:
            _run(negotiate_protocol(session, state, timeout=5))
        assert raised.value.discovery_error is discovery
        assert raised.value.proof_error is proof
        assert "Invalid request parameters" in str(raised.value)
        assert "initialize failed" in str(raised.value)
        assert state.negotiated_era is None
        assert session.calls == ["server/discover", "initialize"]

    def test_second_discover_failure_does_not_trigger_second_proof(self):
        session = _Session(
            discover=_Err(-32602),
            initialize=_Err(-32000, "proof failed"),
        )
        state = _state(ProtocolPolicy.AUTO)
        with pytest.raises(LegacyProofError):
            _run(negotiate_protocol(session, state, timeout=5))
        with pytest.raises(_Err):
            _run(negotiate_protocol(session, state, timeout=5))
        assert session.calls == ["server/discover", "initialize", "server/discover"]

    @pytest.mark.parametrize(
        "error",
        [
            _Err(-32602, "different message", ""),
            _Err(-32602, "Invalid request parameters", {"unexpected": True}),
            _Err(-32601, "Method not found", None),
            _Err(-32000, "server error", None),
        ],
    )
    def test_noncanonical_discover_errors_never_probe_initialize(self, error):
        session = _Session(discover=error)
        with pytest.raises(type(error)):
            _run(negotiate_protocol(session, _state(ProtocolPolicy.AUTO), timeout=5))
        assert session.calls == ["server/discover"]

    @pytest.mark.parametrize("method", ["tools/list", "tools/call", "prompts/get"])
    def test_invalid_params_from_other_methods_is_not_a_legacy_fingerprint(self, method):
        state = _state(ProtocolPolicy.AUTO)
        assert not is_candidate_legacy_discovery_rejection(
            _Err(-32602),
            state=state,
            request_method=method,
        )

    def test_invalid_params_after_negotiation_is_not_a_legacy_fingerprint(self):
        state = _state(ProtocolPolicy.AUTO)
        state.negotiated_era = "modern"
        assert not is_candidate_legacy_discovery_rejection(
            _Err(-32602),
            state=state,
            request_method="server/discover",
        )


class TestConnectionGeneration:
    def test_old_generation_cannot_mutate_current_state(self):
        task = MCPServerTask("generation-test")
        task._connection_generation = 2
        with pytest.raises(StaleConnectionGenerationError, match="generation 1"):
            task._assert_connection_generation(1)

    def test_generation_replacement_rejects_inflight_negotiation(self):
        session = _Session()
        current_generation = 1

        def assert_current(expected: int) -> None:
            if current_generation != expected:
                raise StaleConnectionGenerationError(expected, current_generation)

        async def replace_during_discover() -> object:
            nonlocal current_generation
            session.calls.append("server/discover")
            current_generation = 2
            return "DISCOVER"

        setattr(session, "discover", replace_during_discover)
        with pytest.raises(StaleConnectionGenerationError):
            _run(
                negotiate_protocol(
                    session,
                    _state(ProtocolPolicy.AUTO),
                    timeout=5,
                    assert_generation=assert_current,
                )
            )
        assert session.calls == ["server/discover"]

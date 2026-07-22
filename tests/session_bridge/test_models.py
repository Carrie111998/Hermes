from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError, fields
import hashlib
import hmac
import json
from typing import Any

import pytest

from session_bridge import (
    BridgeMarkerPayload,
    ContextPack,
    InvalidBridgeMarker,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    UpsertResult,
    canonical_session_id,
    decode_bridge_marker,
    encode_bridge_marker,
    stable_message_key,
)
from session_bridge.models import SidebarJobState


SECRET = b"local-test-key"
MARKER_PREFIX = "HERMES_SESSION_BRIDGE_V1"


def _message(
    *,
    native_event_id: str = "event-1",
    ordinal: int = 0,
    role: str = "user",
    content: str | None = "hello",
    timestamp: float = 1.0,
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=native_event_id,
        ordinal=ordinal,
        role=role,
        content=content,
        timestamp=timestamp,
    )


def _payload(*, target_provider: Provider = Provider.CODEX) -> BridgeMarkerPayload:
    return BridgeMarkerPayload(
        bridge_id="bridge-1",
        source_session_id="claude:source-1",
        target_provider=target_provider,
        policy_generation=3,
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _signed_marker_body(body: bytes, secret: bytes = SECRET) -> str:
    encoded = _b64url(body)
    signature = _b64url(
        hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{MARKER_PREFIX}:{encoded}.{signature}"


def _signed_json_marker(data: Any) -> str:
    return _signed_marker_body(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def test_public_enum_values_are_exact_and_validate_unknown_values():
    assert [member.value for member in Provider] == ["claude", "codex", "hermes"]
    assert [member.value for member in OriginKind] == [
        "native",
        "bridge_placeholder",
        "bridge_continuation",
    ]
    assert [member.value for member in Relation] == ["mirrors", "continues", "forks"]
    assert [member.value for member in MirrorJobState] == [
        "queued",
        "running",
        "retry",
        "succeeded",
        "manual_failure",
    ]

    for enum_type in (Provider, OriginKind, Relation, MirrorJobState):
        with pytest.raises(ValueError):
            enum_type("unknown")


def test_sidebar_job_states_are_the_public_contract() -> None:
    assert [state.value for state in SidebarJobState] == [
        "sidebar_pending",
        "sidebar_leased",
        "sidebar_visible",
        "sidebar_retry",
        "sidebar_failed",
    ]

    with pytest.raises(ValueError):
        SidebarJobState("unknown")


@pytest.mark.parametrize(
    ("provider", "native_id", "expected"),
    [
        (Provider.CLAUDE, "  session-1  ", "claude:session-1"),
        (Provider.CODEX, "\tthread-1\n", "codex:thread-1"),
        (Provider.HERMES, "  existing-hermes-id  ", "existing-hermes-id"),
        ("claude", "session-2", "claude:session-2"),
    ],
)
def test_canonical_session_id(provider, native_id, expected):
    assert canonical_session_id(provider, native_id) == expected


@pytest.mark.parametrize("native_id", ["", "   ", "\t\n", None])
def test_canonical_session_id_rejects_empty_native_ids(native_id):
    with pytest.raises(ValueError):
        canonical_session_id(Provider.CLAUDE, native_id)


def test_canonical_session_id_rejects_unknown_provider_values():
    with pytest.raises(ValueError):
        canonical_session_id("openai", "session-1")


def test_hermes_canonical_id_cannot_alias_claude_external_id():
    external_id = canonical_session_id(Provider.CLAUDE, "session-1")

    assert external_id == "claude:session-1"
    with pytest.raises(ValueError):
        canonical_session_id(Provider.HERMES, external_id)


@pytest.mark.parametrize("native_id", ["claude:session-1", "codex:thread-1"])
def test_hermes_canonical_id_rejects_reserved_external_prefixes(native_id):
    with pytest.raises(ValueError):
        canonical_session_id(Provider.HERMES, native_id)


def test_domain_records_are_frozen_and_preserve_required_defaults():
    message = _message()
    projection = SessionProjection(
        provider=Provider.CLAUDE,
        native_id="source-1",
        title=None,
        cwd=None,
        started_at=1.0,
        last_active=2.0,
        messages=[message],
    )
    records = [
        message,
        projection,
        _payload(),
        UpsertResult("claude:source-1", 1, False, True),
        SessionLink(
            id="link-1",
            from_session_id="claude:source-1",
            to_session_id="codex:target-1",
            relation=Relation.CONTINUES,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=3.0,
        ),
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:source-1",
            target_session_id=None,
            source_cursor="cursor-1",
            source_hash="hash-1",
            budget_chars=10_000,
            payload="context",
            created_at=4.0,
        ),
    ]

    assert message.tool_name is None
    assert message.tool_calls is None
    assert message.tool_call_id is None
    assert message.reasoning is None
    assert projection.native_path is None
    assert projection.native_status == "active"
    assert projection.native_cursor is None
    assert projection.native_hash is None
    assert projection.git_branch is None
    assert projection.parser_version == 1
    assert projection.origin_kind is OriginKind.NATIVE
    assert projection.origin_bridge_id is None
    assert records[-1].immutable_at is None

    for record in records:
        field_name = fields(record)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, "changed")


def test_session_projection_accepts_a_git_branch():
    projection = SessionProjection(
        provider=Provider.CLAUDE,
        native_id="source-1",
        title=None,
        cwd=None,
        started_at=1.0,
        last_active=2.0,
        messages=[],
        git_branch="codex/session-bridge",
    )

    assert projection.git_branch == "codex/session-bridge"


def test_stable_message_key_depends_only_on_native_event_id_and_ordinal():
    original = _message()
    same_identity = _message(role="assistant", content="changed", timestamp=99.0)

    assert stable_message_key(original) == stable_message_key(original)
    assert stable_message_key(original) == stable_message_key(same_identity)
    assert stable_message_key(original) != stable_message_key(
        _message(native_event_id="event-2")
    )
    assert stable_message_key(original) != stable_message_key(_message(ordinal=1))


@pytest.mark.parametrize(
    "message",
    [
        _message(native_event_id=""),
        _message(native_event_id="   "),
        _message(ordinal=-1),
    ],
)
def test_stable_message_key_rejects_invalid_identity(message):
    with pytest.raises(ValueError):
        stable_message_key(message)


@pytest.mark.parametrize("target_provider", [Provider.CLAUDE, Provider.CODEX])
def test_signed_marker_round_trip_uses_canonical_compact_json(target_provider):
    payload = _payload(target_provider=target_provider)

    marker = encode_bridge_marker(payload, SECRET)

    assert decode_bridge_marker(marker, SECRET) == payload
    prefix, encoded_and_signature = marker.rsplit(":", 1)
    encoded, signature = encoded_and_signature.split(".", 1)
    assert prefix == MARKER_PREFIX
    assert "=" not in encoded
    assert "=" not in signature
    decoded_json = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    assert decoded_json == json.dumps(
        {
            "bridge_id": "bridge-1",
            "policy_generation": 3,
            "source_session_id": "claude:source-1",
            "target_provider": target_provider.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_signed_marker_rejects_wrong_key():
    marker = encode_bridge_marker(_payload(), SECRET)

    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(marker, b"different-key")


def test_signed_marker_rejects_encoded_body_tamper():
    marker = encode_bridge_marker(_payload(), SECRET)

    prefix, encoded_and_signature = marker.rsplit(":", 1)
    encoded, signature = encoded_and_signature.split(".", 1)
    replacement = "A" if encoded[-1] != "A" else "B"
    tampered = f"{prefix}:{encoded[:-1]}{replacement}.{signature}"
    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(tampered, b"local-test-key")


def test_signed_marker_rejects_noncanonical_signature_tamper():
    marker = encode_bridge_marker(_payload(), SECRET)
    prefix, encoded_and_signature = marker.rsplit(":", 1)
    encoded, signature = encoded_and_signature.split(".", 1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    replacement = alphabet[alphabet.index(signature[-1]) + 1]
    tampered_signature = f"{signature[:-1]}{replacement}"
    padding = "=" * (-len(signature) % 4)
    tampered_padding = "=" * (-len(tampered_signature) % 4)

    assert base64.urlsafe_b64decode(signature + padding) == base64.urlsafe_b64decode(
        tampered_signature + tampered_padding
    )
    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(f"{prefix}:{encoded}.{tampered_signature}", SECRET)


def test_signed_marker_rejects_noncanonical_body_base64url():
    marker = encode_bridge_marker(_payload(), SECRET)
    prefix, encoded_and_signature = marker.rsplit(":", 1)
    encoded, _ = encoded_and_signature.split(".", 1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    replacement = alphabet[alphabet.index(encoded[-1]) + 1]
    alternate_encoded = f"{encoded[:-1]}{replacement}"
    padding = "=" * (-len(encoded) % 4)
    alternate_padding = "=" * (-len(alternate_encoded) % 4)
    alternate_signature = _b64url(
        hmac.new(SECRET, alternate_encoded.encode("ascii"), hashlib.sha256).digest()
    )

    assert base64.urlsafe_b64decode(encoded + padding) == base64.urlsafe_b64decode(
        alternate_encoded + alternate_padding
    )
    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(
            f"{prefix}:{alternate_encoded}.{alternate_signature}", SECRET
        )


def test_signed_marker_rejects_resigned_noncanonical_json():
    body = json.dumps(
        {
            "target_provider": "codex",
            "source_session_id": "claude:source-1",
            "policy_generation": 3,
            "bridge_id": "bridge-1",
        },
        indent=2,
        sort_keys=False,
    ).encode("utf-8")
    marker = _signed_marker_body(body)

    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(marker, SECRET)


@pytest.mark.parametrize("secret", [None, b"", ""])
def test_signed_marker_rejects_missing_or_empty_secrets(secret):
    with pytest.raises(InvalidBridgeMarker):
        encode_bridge_marker(_payload(), secret)

    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker("anything", secret)


def test_signed_marker_encode_rejects_hermes_target_provider():
    with pytest.raises(InvalidBridgeMarker):
        encode_bridge_marker(_payload(target_provider=Provider.HERMES), SECRET)


@pytest.mark.parametrize("target_provider", ["hermes", "unknown"])
def test_signed_marker_decode_rejects_invalid_target_provider(target_provider):
    marker = _signed_json_marker({
        "bridge_id": "bridge-1",
        "policy_generation": 3,
        "source_session_id": "claude:source-1",
        "target_provider": target_provider,
    })

    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(marker, SECRET)


@pytest.mark.parametrize(
    "marker",
    [
        "",
        MARKER_PREFIX,
        f"{MARKER_PREFIX}:body",
        f"{MARKER_PREFIX}:body.signature.extra",
        "WRONG_PREFIX:body.signature",
    ],
)
def test_signed_marker_rejects_malformed_envelope(marker):
    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(marker, SECRET)


def test_signed_marker_rejects_signed_invalid_base64():
    encoded = "%%%"
    signature = _b64url(
        hmac.new(SECRET, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    marker = f"{MARKER_PREFIX}:{encoded}.{signature}"

    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(marker, SECRET)


def test_signed_marker_rejects_signed_non_json_body():
    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(_signed_marker_body(b"not-json"), SECRET)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {
            "bridge_id": "bridge-1",
            "policy_generation": "3",
            "source_session_id": "claude:source-1",
            "target_provider": "codex",
        },
        {
            "bridge_id": "bridge-1",
            "extra": "not-allowed",
            "policy_generation": 3,
            "source_session_id": "claude:source-1",
            "target_provider": "codex",
        },
    ],
)
def test_signed_marker_rejects_malformed_json_payload(data):
    with pytest.raises(InvalidBridgeMarker):
        decode_bridge_marker(_signed_json_marker(data), SECRET)

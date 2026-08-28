"""Persistent webhook route documents layered on the intake contract.

``webhook_contract.WebhookRouteConfig`` remains the sole authority for route
identity, provider selection, signature mode, event binding, and profile
scope.  This module owns only the larger *stored document* used by management
surfaces.  Keeping those responsibilities separate prevents a persisted model
from drifting into a second authentication contract.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookRouteConfig,
)


_ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_PROFILE_NAME_RE = re.compile(r"^(?:default|[a-z0-9][a-z0-9_-]{0,63})$")
_SECRET_REF_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_LEGACY_CLI_DESCRIPTION_PREFIX = "Agent-created subscription:"


class WebhookRouteDocument(BaseModel):
    """Typed, profile-bound webhook route persistence document.

    Unknown JSON fields are retained so provider extensions and newer route
    policy survive a read/write cycle by an older management surface.  A
    legacy plaintext ``secret`` can be loaded, but it is deliberately kept
    separate from ``secret_ref``: only the secret migration may turn one into
    the other after secure write/readback verification.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=False,
        validate_assignment=True,
    )

    name: str
    enabled: bool = True
    description: str = ""
    profile: str = "default"
    events: list[str] = Field(default_factory=list)
    provider: str | None = None
    signature_mode: str | None = None
    signature_context: str | None = None
    secret_ref: str | None = None

    prompt: str = ""
    skills: list[str] = Field(default_factory=list)
    script: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None

    session_mode: Literal["event", "thread", "keyed"] = "event"
    session_key_template: str | None = None
    approval_mode: Literal["deny", "delivery_target"] = "deny"
    clarification_mode: Literal["fail", "delivery_target"] = "fail"
    response_mode: Literal["accepted", "wait", "callback"] = "accepted"
    callback: dict[str, Any] | None = None
    deliveries: list[dict[str, Any]] = Field(default_factory=list)
    deliver_only: bool = False
    completion_script: str | None = None

    # Plaintext is accepted only for lossless loading of the pre-reference
    # format.  It never appears in repr/model_dump and is never interpreted as
    # a reference.  ``to_persisted_route`` writes it back unchanged until the
    # dedicated migration has durably moved it.
    legacy_secret: str | None = Field(
        default=None,
        alias="secret",
        exclude=True,
        repr=False,
    )
    legacy_secret_value: str | None = Field(
        default=None,
        alias="secret_value",
        exclude=True,
        repr=False,
    )
    _contract: WebhookRouteConfig | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _default_new_generic_route(cls, value: Any) -> Any:
        """Default only a genuinely undeclared *new* route to generic_v2.

        A provider-only document must let the canonical provider choose its
        own default verifier.  Persisted legacy input that had neither field
        explicitly supplies ``signature_mode=None`` in ``from_persisted_route``
        so it fails closed (or takes the one content-derived CLI migration).
        """

        if isinstance(value, Mapping):
            candidate = dict(value)
            if not candidate.get("provider") and "signature_mode" not in candidate:
                candidate["signature_mode"] = "generic_v2"
            return candidate
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not isinstance(value, str) or not _ROUTE_NAME_RE.fullmatch(value):
            raise ValueError("route name must be a canonical lowercase URL slug")
        return value

    @field_validator("profile")
    @classmethod
    def _valid_profile(cls, value: str) -> str:
        if not isinstance(value, str) or not _PROFILE_NAME_RE.fullmatch(value):
            raise ValueError("profile must be a canonical profile id")
        return value

    @field_validator("secret_ref")
    @classmethod
    def _valid_secret_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _SECRET_REF_RE.fullmatch(value):
            raise ValueError("secret_ref must be a canonical secret identifier")
        return value

    @field_validator("events", "skills")
    @classmethod
    def _nonempty_unique_strings(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("route string lists require non-empty strings")
            clean = item.strip()
            if clean not in result:
                result.append(clean)
        return result

    @model_validator(mode="after")
    def _validate_policy(self) -> "WebhookRouteDocument":
        # These are internal Python attribute names for the two accepted
        # on-disk aliases below. With ``extra='allow'``, retaining them as
        # unknown fields would put plaintext into repr/model_dump.
        extras = self.model_extra or {}
        if "legacy_secret" in extras or "legacy_secret_value" in extras:
            raise ValueError("route contains a reserved secret field name")

        plaintext_values = [
            value
            for value in (self.legacy_secret, self.legacy_secret_value)
            if value is not None
        ]
        if len(plaintext_values) > 1:
            raise ValueError("route has more than one legacy plaintext secret")
        if self.secret_ref is not None and plaintext_values:
            raise ValueError(
                "route cannot contain both secret_ref and plaintext secret"
            )
        if plaintext_values and (
            not isinstance(plaintext_values[0], str) or not plaintext_values[0]
        ):
            raise ValueError("legacy plaintext secret must be a non-empty string")

        if self.deliver_only and not self.deliveries:
            raise ValueError("deliver_only routes require at least one delivery target")
        if self.deliver_only and self.response_mode != "accepted":
            raise ValueError("deliver_only routes must use response_mode='accepted'")
        if self.session_mode == "keyed" and not self.session_key_template:
            raise ValueError("keyed session_mode requires session_key_template")
        if self.session_mode != "keyed" and self.session_key_template is not None:
            raise ValueError("session_key_template is only valid for keyed sessions")
        if self.approval_mode == "delivery_target" and not self.deliveries:
            raise ValueError("delivery-target approvals require a delivery target")
        if self.clarification_mode == "delivery_target" and not self.deliveries:
            raise ValueError("delivery-target clarification requires a delivery target")

        if self.response_mode == "callback":
            callback_url = (
                self.callback.get("url") if isinstance(self.callback, dict) else None
            )
            if not isinstance(callback_url, str):
                raise ValueError("callback response mode requires callback.url")
            parsed = urlsplit(callback_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("callback.url must be an absolute HTTP(S) URL")
        elif self.callback is not None:
            raise ValueError("callback is only valid with response_mode='callback'")

        # Delegate every security/identity decision to the canonical contract.
        try:
            self._contract = WebhookRouteConfig.bind(
                self.name,
                self.contract_mapping(),
                headers={},
                request_profile=self.profile,
            )
        except WebhookContractError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @property
    def contract(self) -> WebhookRouteConfig:
        """The exact canonical intake binding for this stored document."""

        if self._contract is None:  # pragma: no cover - validators populate it
            self._contract = WebhookRouteConfig.bind(
                self.name,
                self.contract_mapping(),
                headers={},
                request_profile=self.profile,
            )
        return self._contract

    @property
    def has_legacy_plaintext_secret(self) -> bool:
        return self.legacy_secret is not None or self.legacy_secret_value is not None

    def contract_mapping(self) -> dict[str, Any]:
        """Return the safe full document consumed by the intake contract.

        Provider-specific authority such as Trello's ``callback_url`` and a
        custom-HMAC ``signature`` object lives in retained extra fields.  The
        canonical contract must see those fields; filtering down to a small
        common subset would validate a weaker/different route.  Only secret
        persistence fields are excluded here.
        """

        result = self.model_dump(mode="python", exclude={"name"})
        for key in ("secret_ref", "secret", "secret_value"):
            result.pop(key, None)
        result = {key: value for key, value in result.items() if value is not None}
        return result


def _legacy_delivery(route: Mapping[str, Any]) -> list[dict[str, Any]]:
    target = route.get("deliver")
    extra = route.get("deliver_extra")
    if target is None and not extra:
        return []
    delivery: dict[str, Any] = {}
    if target is not None:
        delivery["target"] = target
    if isinstance(extra, Mapping):
        delivery.update(extra)
    elif extra is not None:
        delivery["extra"] = extra
    return [delivery]


def from_persisted_route(
    name: str,
    route: Mapping[str, Any],
    *,
    profile: str,
) -> WebhookRouteDocument:
    """Parse one persisted route without changing its secret authority."""

    if not isinstance(route, Mapping):
        raise TypeError("persisted webhook route must be an object")
    raw = dict(route)
    embedded_name = raw.pop("name", name)
    embedded_profile = raw.get("profile", profile)
    if embedded_profile != profile:
        raise ValueError("stored route profile does not match its profile store")
    raw["profile"] = profile

    if "deliveries" not in raw:
        raw["deliveries"] = _legacy_delivery(raw)
    # Keep the old singular fields only as extras so a read/write is lossless
    # until the delivery architecture consumes the canonical list.

    if raw.get("provider") and "signature_mode" not in raw:
        # Provider-only routes delegate verifier selection to that provider.
        raw["signature_mode"] = None
    elif not raw.get("provider") and not raw.get("signature_mode"):
        description = raw.get("description")
        if isinstance(description, str) and description.startswith(
            _LEGACY_CLI_DESCRIPTION_PREFIX
        ):
            raw["provider"] = "github"
            # Suppress the creation-time generic_v2 default. Provider-only
            # binding lets the canonical contract select GitHub's own default.
            raw["signature_mode"] = None
        else:
            # ``None`` suppresses the model's creation-time default. The
            # canonical contract then fails closed instead of guessing from
            # attacker-controlled request headers.
            raw["signature_mode"] = None

    return WebhookRouteDocument.model_validate({"name": embedded_name, **raw})


def to_persisted_route(route: WebhookRouteDocument) -> dict[str, Any]:
    """Serialize one route document, preserving unmigrated plaintext exactly."""

    data = route.model_dump(mode="json", exclude={"name"}, by_alias=False)
    # ``None`` is not useful in the on-disk JSON and can accidentally look like
    # an explicit policy override to older readers.
    data = {key: value for key, value in data.items() if value is not None}
    if route.legacy_secret is not None:
        data["secret"] = route.legacy_secret
    if route.legacy_secret_value is not None:
        data["secret_value"] = route.legacy_secret_value
    return data


__all__ = [
    "WebhookRouteDocument",
    "from_persisted_route",
    "to_persisted_route",
]

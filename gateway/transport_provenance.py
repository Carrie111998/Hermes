"""The exact ingress adapter a turn arrived on, as one indivisible record.

The owner map (``transport_profile``) and the adapter slot inside it
(``transport_slot``) only identify a transport together: an owner alone cannot
tell a relay-fronted platform from a native one, and a slot alone does not say
whose credentials hold it. Capture, serialization and decoding all run through
this module so no site can emit half a return address, and a damaged or
contradictory one is dropped rather than resolved to a guess.

A recorded pair is exactly one of three classes (see :func:`classify_provenance`):
``LEGACY`` — both fields absent, from before provenance was recorded, which
keeps the runtime/alias fallback; ``EXACT`` — both present and the slot is one
the turn could have arrived on, which resolves that adapter directly; and
``MALFORMED`` — anything else, which is dropped.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional

PROFILE_FIELD = "transport_profile"
SLOT_FIELD = "transport_slot"
PROVENANCE_KEYS = (PROFILE_FIELD, SLOT_FIELD)

PROFILE_ENV = "HERMES_SESSION_TRANSPORT_PROFILE"
SLOT_ENV = "HERMES_SESSION_TRANSPORT_SLOT"

LEGACY = "legacy"
EXACT = "exact"
MALFORMED = "malformed"


class TransportProvenance(NamedTuple):
    """The owner map and adapter slot the commissioning turn arrived on."""

    profile: str
    slot: str


def _field(record: Any, name: str) -> str:
    """Read *name* off a mapping or an attribute-bearing record."""
    value = record.get(name, "") if hasattr(record, "get") else getattr(record, name, "")
    return str(value or "").strip()


def read_provenance(record: Any, prefix: str = "") -> TransportProvenance:
    """Read a recorded pair back verbatim, damage included.

    Classification is :func:`classify_provenance`'s job: this reader never
    repairs, defaults or drops a field, so a half-record stays visibly half.
    """
    return TransportProvenance(
        _field(record, f"{prefix}{PROFILE_FIELD}"),
        _field(record, f"{prefix}{SLOT_FIELD}"),
    )


def provenance_from_session_env(get_env) -> TransportProvenance:
    """Read the pair from the session context via *get_env*, damage included."""
    return TransportProvenance(
        str(get_env(PROFILE_ENV, "") or "").strip(),
        str(get_env(SLOT_ENV, "") or "").strip(),
    )


def provenance_fields(
    provenance: Optional[TransportProvenance], prefix: str = ""
) -> dict:
    """Both fields as a mapping, so no writer can persist one without the other."""
    profile, slot = provenance if provenance is not None else ("", "")
    return {f"{prefix}{PROFILE_FIELD}": profile, f"{prefix}{SLOT_FIELD}": slot}


def stamp_provenance(
    target: Any, provenance: Optional[TransportProvenance], prefix: str = ""
) -> None:
    """Write both fields onto *target*'s attributes in one call."""
    for name, value in provenance_fields(provenance, prefix).items():
        setattr(target, name, value)


def classify_provenance(provenance: TransportProvenance, platform_name: Any) -> str:
    """Return ``LEGACY``, ``EXACT`` or ``MALFORMED`` for a recorded pair.

    A slot is only credible for the two adapter maps a turn on *platform_name*
    can arrive through: that platform's own slot, or the relay fronting it.
    """
    from gateway.config import Platform

    profile, slot = provenance
    if not profile and not slot:
        return LEGACY
    if not profile or not slot:
        return MALFORMED
    if slot not in (str(platform_name), Platform.RELAY.value):
        return MALFORMED
    return EXACT

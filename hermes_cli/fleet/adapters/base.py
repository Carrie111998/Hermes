"""Shared adapter validation and child-process safety."""

from __future__ import annotations

import os
from typing import Mapping

from ..types import AdapterRequest, Qualification, ReasonCode


_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "HERMES_HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


def safe_child_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct an allow-listed environment with no credential variables."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENV_ALLOWLIST
    }
    if source is not None:
        for key, value in source.items():
            if key.upper() in _ENV_ALLOWLIST:
                environment[key] = value
    environment["NO_COLOR"] = "1"
    return environment


def validate_execution(
    request: AdapterRequest, qualification: Qualification
) -> ReasonCode | None:
    profile = request.profile
    if not qualification.qualified:
        return ReasonCode.QUALIFICATION_FAILED
    if qualification.auth_kind is None:
        return ReasonCode.AUTH_MISSING
    if qualification.auth_kind not in profile.allowed_auth_kinds:
        return ReasonCode.AUTH_KIND_FORBIDDEN
    if not qualification.auth_source:
        return ReasonCode.AUTH_SOURCE_UNKNOWN
    if qualification.overage_disabled is not True:
        return ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON
    if qualification.provider_id != profile.provider_id:
        return ReasonCode.PROVIDER_MISMATCH
    if request.model not in profile.ordered_models:
        return ReasonCode.MODEL_MISMATCH
    if request.model not in qualification.models:
        return ReasonCode.MODEL_MISMATCH
    if request.effort not in profile.supported_efforts:
        return ReasonCode.QUALIFICATION_FAILED
    if request.effort not in qualification.efforts:
        return ReasonCode.QUALIFICATION_FAILED
    if request.fast_mode or not qualification.fast_off_supported:
        return ReasonCode.QUALIFICATION_FAILED
    return None

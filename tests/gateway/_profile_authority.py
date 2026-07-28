"""Test helpers for constructor-frozen gateway profile authority.

These helpers reproduce the production constructor boundary for deliberately
partial ``GatewayRunner`` doubles.  They do not bypass verification and they
support exactly one served profile, matching the one-process/one-profile
runtime contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def install_frozen_profile_authority(
    runner: Any,
    home: Path,
    *,
    profile: str = "default",
):
    """Install and publish one verified profile identity on a runner double."""

    from gateway.api_request_scope import freeze_api_profile_inventory
    from tools.async_delegation import (
        register_frozen_event_delivery_inventory,
    )
    from tools.process_registry import process_registry

    profile_name = str(profile or "").strip()
    if not profile_name:
        raise ValueError("test profile name is required")
    profile_home = Path(home)
    profile_home.mkdir(parents=True, exist_ok=True)
    inventory = freeze_api_profile_inventory(
        ((profile_name, profile_home),)
    )
    register_frozen_event_delivery_inventory(inventory)
    identity = inventory[0]
    process_registry.bind_checkpoint_path(
        Path(identity.canonical_home) / "processes.json"
    )

    runner._served_profile_identity_inventory = inventory
    runner._primary_profile_identity = identity
    runner._served_profile_identities_by_name = {
        profile_name: identity,
    }
    runner._gateway_state_db_path = Path(identity.canonical_home) / "state.db"
    return identity

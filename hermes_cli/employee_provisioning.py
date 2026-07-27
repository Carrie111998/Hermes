"""Provision approved employee records into least-privilege Charterforge profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import organization_db as org


def provision_employee_profile(
    conn,
    employee_id: str,
    *,
    actor: str,
    profile_name: str,
    source_config: Optional[dict] = None,
) -> Path:
    """Create and activate a profile bound to the employee's current mandate.

    Credentials are never cloned.  The new profile receives only explicitly
    mandated toolsets; provider credentials must be provisioned separately by
    the configured secret/identity mechanism.
    """
    from hermes_cli.config import DEFAULT_CONFIG, save_config
    from hermes_cli.profiles import create_profile, write_profile_meta

    employee = org.get_employee_record(conn, employee_id)
    if employee["status"] != "approved":
        raise org.OrganizationError("only approved employees may be provisioned")
    mandate = org.get_current_mandate(conn, employee_id)
    if mandate is None:
        raise org.OrganizationError("employee cannot provision without a mandate")

    org.transition_employee(conn, employee_id, "provisioning", actor=actor)
    profile_dir = create_profile(
        profile_name,
        no_alias=True,
        no_skills=True,
        description=(
            f"{employee['title']} in the governed Charterforge organization. "
            f"Mandate: {mandate['purpose']}"
        ),
    )
    toolsets = sorted(
        {str(item).strip() for item in mandate.get("toolsets") or [] if str(item).strip()}
    )
    # Start from a minimal profile-local config. Carry model selection only;
    # never copy .env, messaging credentials, memories, sessions, or skills.
    config = {
        "_config_version": DEFAULT_CONFIG["_config_version"],
        "platform_toolsets": {"cli": toolsets, "kanban": toolsets},
        "agent": {"disabled_toolsets": []},
        "agentic": {
            "enabled": False,
            "operating_mode": "autonomous",
            "operator_role": "advisor",
        },
    }
    source_model = (source_config or {}).get("model")
    if source_model:
        config["model"] = source_model
    token = set_hermes_home_override(profile_dir)
    try:
        save_config(config, strip_defaults=False)
    finally:
        reset_hermes_home_override(token)

    write_profile_meta(
        profile_dir,
        organization_id=employee["organization_id"],
        employee_id=employee_id,
        manager_employee_id=employee["manager_id"],
        corporate_level=employee["level"],
        employment_class=(
            "contractor" if employee["employment_type"] == "contractor" else "fte"
        ),
        mandate_id=mandate["id"],
        mandate_version=mandate["version"],
        mandate_expires_at=mandate["expires_at"],
    )
    org.transition_employee(
        conn,
        employee_id,
        "active",
        actor=actor,
        profile_name=profile_name,
    )
    return profile_dir

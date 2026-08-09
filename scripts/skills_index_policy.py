"""Publication policy for community skill-catalog entries.

The bundled Photon/Spectrum messaging integration is retired.  Community index
records tied to that product must not republish its identity through the docs
APIs, while ordinary words such as "photonics" and unrelated audio spectra
remain valid catalog content.
"""

from __future__ import annotations

from typing import Any, Mapping


def is_retired_platform_catalog_entry(entry: Mapping[str, Any]) -> bool:
    """Return whether a community record is tied to the retired integration."""
    identity_fields = (
        entry.get("identifier"),
        entry.get("repo"),
        entry.get("resolved_github_id"),
    )
    identities = {
        value.strip().lower()
        for value in identity_fields
        if isinstance(value, str) and value.strip()
    }
    if any(
        value == "photon-hq/skills"
        or value.startswith("photon-hq/skills/")
        or value.startswith("skills-sh/photon-hq/skills/")
        for value in identities
    ):
        return True

    tags = entry.get("tags")
    normalized_tags = {
        tag.strip().lower()
        for tag in tags
        if isinstance(tag, str) and tag.strip()
    } if isinstance(tags, list) else set()
    description = entry.get("description")
    return (
        "photon" in normalized_tags
        and isinstance(description, str)
        and description.lstrip().lower().startswith("photon:")
    )

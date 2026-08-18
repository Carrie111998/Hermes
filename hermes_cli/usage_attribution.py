"""Profile-owned consent for optional provider usage attribution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def usage_attribution_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Require an explicit opt-in before adding new provider attribution tags."""
    if config is None:
        try:
            from hermes_cli.config import read_raw_config_readonly

            # Like shared-metrics consent, this belongs to the active profile.
            # Defaults and managed overlays must not opt a user in.
            config = read_raw_config_readonly()
        except Exception:
            logger.debug("Unable to read usage-attribution policy", exc_info=True)
            return False

    telemetry = config.get("telemetry") if isinstance(config, Mapping) else None
    attribution = (
        telemetry.get("usage_attribution")
        if isinstance(telemetry, Mapping)
        else None
    )
    return isinstance(attribution, Mapping) and attribution.get("enabled") is True

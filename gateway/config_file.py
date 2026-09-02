"""Side-effect-free reads of gateway settings from config.yaml."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_gateway_config_dict(config_path: Path | None = None) -> dict:
    """Load raw gateway configuration without bootstrapping the daemon."""
    raw: dict = {}
    used_canonical = False
    try:
        from hermes_cli.config import get_config_path, read_raw_config

        canonical_path = get_config_path()
        if config_path is None:
            config_path = canonical_path
        if config_path == canonical_path:
            raw = read_raw_config()
            used_canonical = True
    except Exception:
        if config_path is None:
            return {}

    if not used_canonical:
        try:
            if config_path is not None and config_path.exists():
                import yaml

                with open(config_path, "r", encoding="utf-8") as config_file:
                    raw = yaml.safe_load(config_file) or {}
        except Exception:
            logger.debug("Could not load gateway config from %s", config_path)
            raw = {}

    try:
        from hermes_cli import managed_scope

        raw = managed_scope.apply_managed_overlay(
            raw if isinstance(raw, dict) else {}
        )
    except Exception:
        pass
    if not isinstance(raw, dict):
        return {}

    try:
        from hermes_cli.config import _normalize_root_model_keys

        raw = _normalize_root_model_keys(raw)
    except Exception:
        pass
    return raw

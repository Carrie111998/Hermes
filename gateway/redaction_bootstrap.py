"""Early redaction env bridge for gateway startup.

The gateway package imports ``gateway.session`` from ``gateway.__init__``, and
that path imports ``agent.redact``. Since the redactor snapshots its env-backed
flags at import time, direct ``python -m gateway.run`` must bridge redaction
config before package exports pull in session.
"""

from __future__ import annotations

import os
from pathlib import Path


_BOOTSTRAP_RESULT: tuple[Path, Path] | None = None


def bridge_gateway_redaction_env() -> tuple[Path, Path]:
    """Load dotenv and bridge redaction config before ``agent.redact`` imports."""
    global _BOOTSTRAP_RESULT
    if _BOOTSTRAP_RESULT is not None:
        return _BOOTSTRAP_RESULT

    from hermes_constants import get_hermes_home
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.env_loader import load_hermes_dotenv

    hermes_home = get_hermes_home()
    load_hermes_dotenv(
        hermes_home=hermes_home,
        project_env=Path(__file__).resolve().parents[1] / ".env",
    )

    # Internal carrier: config.yaml is authoritative, with secure default on.
    os.environ["HERMES_REDACT_PHONE_NUMBERS"] = str(
        DEFAULT_CONFIG["privacy"]["redact_phone_numbers"]
    ).lower()

    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        _BOOTSTRAP_RESULT = (hermes_home, config_path)
        return _BOOTSTRAP_RESULT

    try:
        from hermes_cli.config import _expand_env_vars, read_user_config_raw

        cfg = read_user_config_raw(config_path)
        cfg = _expand_env_vars(cfg)
        if not isinstance(cfg, dict):
            cfg = {}
        try:
            from hermes_cli import managed_scope

            cfg = managed_scope.apply_managed_overlay(cfg)
        except Exception:
            _BOOTSTRAP_RESULT = (hermes_home, config_path)
            return _BOOTSTRAP_RESULT

        security_cfg = cfg.get("security", {})
        if isinstance(security_cfg, dict):
            redact = security_cfg.get("redact_secrets")
            if isinstance(redact, bool):
                os.environ["HERMES_REDACT_SECRETS"] = str(redact).lower()

        privacy_cfg = cfg.get("privacy", {})
        if isinstance(privacy_cfg, dict):
            redact_phone_numbers = privacy_cfg.get("redact_phone_numbers")
            if isinstance(redact_phone_numbers, bool):
                os.environ["HERMES_REDACT_PHONE_NUMBERS"] = str(
                    redact_phone_numbers
                ).lower()
    except Exception:
        pass

    _BOOTSTRAP_RESULT = (hermes_home, config_path)
    return _BOOTSTRAP_RESULT

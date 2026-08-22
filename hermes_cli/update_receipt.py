"""Structured update receipt generator for ``hermes update`` (#91277).

Records machine-readable records of every update run to:
  ~/.hermes/updates/receipt-<timestamp>.json
  ~/.hermes/updates/latest-receipt.json

Surfaces runtimes discovered (profiles, gateways, dashboards),
deployment kinds, steps executed, steps skipped, and per-step status so Desktop,
Dashboard, and automation can inspect update outcomes without parsing unstructured
terminal output.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


@dataclass
class UpdateStep:
    """Individual phase or action taken during the update."""

    name: str
    status: str  # "success", "failed", "skipped"
    duration_sec: float = 0.0
    details: str = ""
    error: str | None = None


@dataclass
class UpdateReceipt:
    """Complete machine-readable record of an update execution."""

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    deployment_kind: str = "git+venv"
    target_branch: str = "main"
    previous_commit: str | None = None
    updated_commit: str | None = None
    profiles_discovered: list[str] = field(default_factory=list)
    gateways_restarted: list[dict[str, Any]] = field(default_factory=list)
    steps: list[UpdateStep] = field(default_factory=list)
    status: str = "SUCCESS"  # "SUCCESS", "PARTIAL", "FAILED"
    error: str | None = None

    def add_step(
        self,
        name: str,
        status: str,
        duration_sec: float = 0.0,
        details: str = "",
        error: str | None = None,
    ) -> None:
        """Record a completed, skipped, or failed step."""
        self.steps.append(
            UpdateStep(
                name=name,
                status=status,
                duration_sec=round(duration_sec, 3),
                details=details,
                error=error,
            )
        )
        if status == "failed" and self.status == "SUCCESS":
            self.status = "PARTIAL"

    def write(self, hermes_home: Path | None = None) -> Path | None:
        """Write receipt to disk. Never raises exceptions."""
        try:
            home = hermes_home or get_hermes_home()
            updates_dir = home / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)

            ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            receipt_file = updates_dir / f"receipt-{ts_slug}.json"
            latest_file = updates_dir / "latest-receipt.json"

            data = asdict(self)
            payload = json.dumps(data, indent=2)

            receipt_file.write_text(payload, encoding="utf-8")
            latest_file.write_text(payload, encoding="utf-8")
            return receipt_file
        except Exception as e:
            logger.debug("Failed to write update receipt: %s", e)
            return None


def load_latest_update_receipt(hermes_home: Path | None = None) -> dict[str, Any] | None:
    """Load the most recent update receipt, or None if absent/corrupt."""
    try:
        home = hermes_home or get_hermes_home()
        latest_file = home / "updates" / "latest-receipt.json"
        if not latest_file.is_file():
            return None
        return json.loads(latest_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Failed to load latest update receipt: %s", e)
        return None

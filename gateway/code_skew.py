"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway is a single long-lived process; its ``sys.modules`` is frozen at
boot. If the checkout is updated underneath it (a manual ``git pull``, or the
window before ``hermes update``'s graceful restart fires), a first-time lazy
import on a new code path can resolve a freshly-pulled consumer module against a
stale cached dependency -> ImportError (see
``tests/test_stale_utils_module_import.py`` for the exact failure).

We snapshot the checkout revision at gateway startup and compare on demand, so
risky callers (e.g. ``/model`` switching) can refuse with a clear "restart the
gateway" message instead of crashing on a cryptic import error.

If the revision can't be read (non-git install, IO error), the boot snapshot
stays ``None`` and skew detection no-ops — it never produces a false positive.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None
_log = logging.getLogger(__name__)


def _fingerprint() -> str | None:
    """Return the identity of the code tree containing this module."""
    try:
        from hermes_cli.gateway_identity import identity_from_project

        return identity_from_project(_PROJECT_ROOT).fingerprint
    except Exception:
        return None


def _write_boot_fingerprint_file(fingerprint: str | None) -> None:
    if fingerprint is None:
        return
    try:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "gateway_boot_fingerprint"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(fingerprint, str) or not fingerprint:
            return
        record = {
            "schema": 1,
            "fingerprint": fingerprint,
            "release_path": str(_PROJECT_ROOT) if fingerprint.startswith("release:") else None,
            "pid": os.getpid(),
            "timestamp": time.time(),
        }
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    except Exception as exc:
        # Startup must remain non-fatal on non-git or read-only installs, but
        # an operator should still be able to see why the boot record is absent.
        _log.warning("could not write gateway boot fingerprint: %s", exc)


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()
    if _boot_fingerprint is None:
        return
    _write_boot_fingerprint_file(_boot_fingerprint)


def read_boot_record(home: Path | None = None) -> dict[str, object] | None:
    """Read the atomic boot record, accepting the legacy one-line format."""
    try:
        if home is None:
            from hermes_constants import get_hermes_home

            home = Path(get_hermes_home())
        path = Path(home) / "gateway_boot_fingerprint"
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"schema": 0, "fingerprint": raw, "release_path": None}
    if not isinstance(value, dict) or not isinstance(value.get("fingerprint"), str):
        return None
    return value


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    if sha and sha != "unresolved" and len(sha) > 10:
        return sha[:10]
    return sha or fingerprint


def detect_code_skew() -> tuple[str, str] | None:
    """Return ``(boot_rev, disk_rev)`` short labels if the checkout drifted
    since boot, else ``None``."""
    if _boot_fingerprint is None:
        return None
    current = _fingerprint()
    if current is None or current == _boot_fingerprint:
        return None
    return _short(_boot_fingerprint), _short(current)

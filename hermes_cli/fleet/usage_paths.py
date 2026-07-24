"""Profile-safe fleet usage capacity paths.

Library code never hardcodes a user-specific absolute path. Defaults resolve
through the active Hermes home so each profile keeps isolated capacity state.
"""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_hermes_home

# Relative to the active Hermes home. Config may override with an absolute path
# or another home-relative path; empty/None means this default.
DEFAULT_USAGE_RELATIVE = Path("fleet") / "usage-weekly.json"
COCKPIT_MIRROR_PATH = Path("C:/HermesBridge/usage-weekly.json")


def default_native_usage_path(*, home: Path | None = None) -> Path:
    """Return the canonical native usage file under the active Hermes home."""

    root = Path(home) if home is not None else get_hermes_home()
    return (root / DEFAULT_USAGE_RELATIVE).resolve()


def resolve_usage_path(
    configured: str | Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Resolve a configured usage path without baking in a user home default.

    - ``None`` / blank → ``{hermes_home}/fleet/usage-weekly.json``
    - absolute path → used as-is
    - relative path → joined under the active Hermes home
    """

    if configured is None:
        return default_native_usage_path(home=home)
    text = str(configured).strip()
    if not text:
        return default_native_usage_path(home=home)
    path = Path(text)
    if path.is_absolute():
        return path
    root = Path(home) if home is not None else get_hermes_home()
    return (root / path).resolve()

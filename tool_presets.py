"""Per-chat tool presets: load / save / resolve.

Presets are reusable, profile-scoped tool/skill selections stored in
``config.yaml`` under a top-level ``tool_presets`` list. Two virtual built-in
presets always exist even with zero user configuration:

  * ``"Chat-only"`` → ``enabled_toolsets: []`` (zero non-core tools)
  * ``"Full"``      → ``enabled_toolsets: null`` (profile / platform default)

This module is deliberately standalone (imported by ``tui_gateway/server.py``
as a thin wrapper) so the resolution logic stays unit-testable and off the
632KB hot file.

Empty-list-vs-None invariant: ``enabled_toolsets: []`` (chat-only) is a real,
falsy posture and must survive every round-trip. Nothing here uses
``x or default`` on a list; ``None`` means "no override / profile default".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Reserved virtual preset names. These are synthesized, never persisted as rows.
CHAT_ONLY = "Chat-only"
FULL = "Full"
RESERVED_NAMES = {CHAT_ONLY, FULL}

# The config.yaml key holding user presets.
_CONFIG_KEY = "tool_presets"

# The config.yaml key holding the profile's default preset for NEW chats.
# ``None`` / absent = no default (new chats fall through to the platform/coding
# posture). A stored value is the name of a built-in or user preset.
_DEFAULT_KEY = "default_tool_preset"

# Per-preset config fields (besides ``name``). All optional; absent == null.
_PRESET_FIELDS = ("enabled_toolsets", "disabled_tools", "allowed_tools", "disabled_skills")


def _load_cfg() -> Dict[str, Any]:
    from hermes_cli.config import load_config

    return load_config() or {}


def _persisted_rows(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return ALL persisted preset rows (including reserved-name overrides).

    Reserved names (``Chat-only``/``Full``) may be persisted here as *overrides*
    that customize the built-ins — the user is free to redefine them; they are
    never deletable, only resettable (see :func:`delete_preset`).
    """
    if cfg is None:
        cfg = _load_cfg()
    raw = cfg.get(_CONFIG_KEY)
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        out.append(_normalize_preset(entry))
    return out


def _override_for(name: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return the persisted override row for a reserved built-in, or None."""
    for row in _persisted_rows(cfg):
        if row["name"] == name:
            return row
    return None


def _raw_presets(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return the persisted NON-reserved (user) presets as a list of dicts."""
    return [row for row in _persisted_rows(cfg) if row["name"] not in RESERVED_NAMES]


def _normalize_list(value: Any) -> Optional[List[str]]:
    """Coerce a config value into ``list[str] | None``.

    ``None`` (or absent) stays ``None`` (no override). A list is normalized to
    stripped strings — **including an empty list**, which is a meaningful
    ``[]`` (chat-only / whitelist-nothing) and must NOT collapse to ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return None
    cleaned = [str(v).strip() for v in value if str(v).strip()]
    if value and not cleaned:
        # A non-empty input that strips down to [] silently becomes the
        # chat-only / whitelist-nothing posture — flag the likely accident.
        logger.warning(
            "tool_presets: list value %r resolved to [] after stripping whitespace; "
            "treating as an explicit empty selection",
            value,
        )
    return cleaned


def _normalize_preset(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return a preset dict with normalized, contract-shaped fields."""
    return {
        "name": str(entry.get("name") or "").strip(),
        "enabled_toolsets": _normalize_list(entry.get("enabled_toolsets")),
        "disabled_tools": _normalize_list(entry.get("disabled_tools")),
        "allowed_tools": _normalize_list(entry.get("allowed_tools")),
        "disabled_skills": _normalize_list(entry.get("disabled_skills")),
    }


def _virtual_presets() -> List[Dict[str, Any]]:
    """The two always-present built-in presets."""
    return [
        {
            "name": CHAT_ONLY,
            "enabled_toolsets": [],
            "disabled_tools": None,
            "allowed_tools": None,
            "disabled_skills": None,
            "builtin": True,
        },
        {
            "name": FULL,
            "enabled_toolsets": None,
            "disabled_tools": None,
            "allowed_tools": None,
            "disabled_skills": None,
            "builtin": True,
        },
    ]


def list_presets(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return every selectable preset (2 built-ins + user presets).

    Each entry carries ``builtin: bool``. Built-ins come first, then user
    presets in config order. A built-in with a persisted override reflects the
    user's customized selection (the fields the user set); its ``builtin`` flag
    stays ``True`` (non-deletable, only resettable).
    """
    if cfg is None:
        cfg = _load_cfg()
    presets: List[Dict[str, Any]] = []
    for base in _virtual_presets():
        override = _override_for(base["name"], cfg)
        if override is not None:
            # User has customized this built-in: use their fields verbatim.
            presets.append({
                "name": base["name"],
                "enabled_toolsets": override.get("enabled_toolsets"),
                "disabled_tools": override.get("disabled_tools"),
                "allowed_tools": override.get("allowed_tools"),
                "disabled_skills": override.get("disabled_skills"),
                "builtin": True,
            })
        else:
            presets.append(base)
    for p in _raw_presets(cfg):
        presets.append({**p, "builtin": False})
    return presets


def resolve_preset(name: Optional[str], cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Resolve a preset *name* to the runtime override lists.

    Returns a dict with the ``sessions.model_config`` shaped keys::

        {
          "enabled_toolsets": [] | [...] | None,
          "disabled_toolsets": None,          # presets don't set this axis
          "allowed_tool_names": [...] | None,
          "denied_tool_names":  [...] | None,
          "disabled_skills":    [...] | None,
          "tool_preset": "<name>",
        }

    Returns ``None`` when *name* is falsy or does not match any known preset
    (caller should fall back to explicit lists / "Custom").
    """
    if not name:
        return None
    name = str(name).strip()
    if name in RESERVED_NAMES:
        # Honor a user override of the built-in; otherwise use its default
        # (Chat-only → [] , Full → None = profile default).
        override = _override_for(name, cfg)
        if override is not None:
            return {
                "enabled_toolsets": override.get("enabled_toolsets"),
                "disabled_toolsets": None,
                "allowed_tool_names": override.get("allowed_tools"),
                "denied_tool_names": override.get("disabled_tools"),
                "disabled_skills": override.get("disabled_skills"),
                "tool_preset": name,
            }
        return {
            "enabled_toolsets": [] if name == CHAT_ONLY else None,
            "disabled_toolsets": None,
            "allowed_tool_names": None,
            "denied_tool_names": None,
            "disabled_skills": None,
            "tool_preset": name,
        }
    for p in _raw_presets(cfg):
        if p["name"] == name:
            return {
                "enabled_toolsets": p.get("enabled_toolsets"),
                "disabled_toolsets": None,
                "allowed_tool_names": p.get("allowed_tools"),
                "denied_tool_names": p.get("disabled_tools"),
                "disabled_skills": p.get("disabled_skills"),
                "tool_preset": name,
            }
    return None


def save_preset(preset: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Upsert a preset (by name). Returns the updated ``list_presets()``.

    User presets are created/updated freely. The reserved built-in names
    (``Chat-only``/``Full``) are also accepted here — saving one persists an
    *override* that customizes the built-in (it's up to the user). Built-ins
    remain non-deletable; :func:`delete_preset` resets them to default instead.
    The preset is normalized so only the contract-shaped fields persist.
    """
    from hermes_cli.config import load_config, save_config

    if not isinstance(preset, dict):
        raise ValueError("preset must be an object")
    name = str(preset.get("name") or "").strip()
    if not name:
        raise ValueError("preset name is required")

    normalized = _normalize_preset(preset)
    # Drop null fields so config.yaml stays terse; keep [] (meaningful).
    row: Dict[str, Any] = {"name": name}
    for field in _PRESET_FIELDS:
        val = normalized.get(field)
        if val is not None:
            row[field] = val

    cfg = load_config()
    existing = cfg.get(_CONFIG_KEY)
    rows: List[Dict[str, Any]] = [r for r in existing if isinstance(r, dict)] if isinstance(existing, list) else []
    replaced = False
    for i, r in enumerate(rows):
        if str(r.get("name") or "").strip() == name:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    cfg[_CONFIG_KEY] = rows
    save_config(cfg)
    return list_presets()


def get_default_preset(cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Return the profile's default preset name for NEW chats, or ``None``.

    ``None`` means no default is configured — a new chat falls through to the
    platform / coding posture (which is effectively "Full"). This is the same
    ``default_tool_preset`` value the desktop settings and ``session.create``
    read, exposed here so the resolution logic has one home.
    """
    if cfg is None:
        cfg = _load_cfg()
    name = str(cfg.get(_DEFAULT_KEY) or "").strip()
    return name or None


def set_default_preset(name: Optional[str]) -> Optional[str]:
    """Set (or clear) the profile's default preset for NEW chats.

    ``name`` = a preset name (built-in or user) to make every new chat start
    with it; falsy/``None`` clears the default so new chats fall through to the
    platform/coding posture. Persists to ``config.yaml``. Returns the stored
    value (``None`` when cleared). Does not validate the name — callers that
    want to reject unknown presets should check against :func:`list_presets`.
    """
    from hermes_cli.config import load_config, save_config

    name = str(name or "").strip()
    cfg = load_config()
    if name:
        cfg[_DEFAULT_KEY] = name
    else:
        cfg.pop(_DEFAULT_KEY, None)
    save_config(cfg)
    return get_default_preset(load_config())


def delete_preset(name: str) -> List[Dict[str, Any]]:
    """Delete a preset by name. Returns the updated ``list_presets()``.

    For a user preset this removes it. For a reserved built-in this removes any
    persisted *override*, resetting the built-in to its default (the built-in
    itself always remains available). An unknown name is a no-op.
    """
    from hermes_cli.config import load_config, save_config

    name = str(name or "").strip()
    if not name:
        return list_presets()

    cfg = load_config()
    existing = cfg.get(_CONFIG_KEY)
    if not isinstance(existing, list):
        return list_presets()
    rows = [
        r for r in existing
        if isinstance(r, dict) and str(r.get("name") or "").strip() != name
    ]
    cfg[_CONFIG_KEY] = rows
    save_config(cfg)
    return list_presets()

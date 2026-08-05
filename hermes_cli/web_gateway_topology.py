"""Gateway-topology readout for the dashboard.

Extracted from hermes_cli/web_server.py (god-file slice R2-B, epic #78791):
port-binding platform constants, per-profile gateway topology collection,
and the TTL cache over it.  Pure module: no routes, no app coupling.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_log = logging.getLogger("hermes_cli.web_gateway_topology")

# Host TCP ports each port-binding gateway platform listens on, as
# ``platform-name -> (config port key, adapter default)``.  Mirrors
# ``PORT_BINDING_PLATFORM_VALUES`` in gateway/config.py and each adapter's
# DEFAULT_PORT / DEFAULT_WEBHOOK_PORT constant.  Used only for the dashboard's
# gateway-topology readout — best-effort display data, not a bind source.
_PORT_BINDING_PLATFORM_PORTS: Dict[str, Tuple[str, int]] = {
    "webhook": ("port", 8644),
    "api_server": ("port", 8642),
    "msgraph_webhook": ("port", 8646),
    "feishu": ("webhook_port", 8765),
    "wecom_callback": ("port", 8645),
    "bluebubbles": ("webhook_port", 8645),
    "sms": ("webhook_port", 8080),
    "whatsapp_cloud": ("webhook_port", 8090),
    "line": ("port", 8646),
}

# Platform states that mean the adapter is NOT serving its port right now.
_PLATFORM_DEAD_STATES = frozenset({"fatal", "disconnected", "stopped"})


def _profile_platform_ports(profile_home: Path, runtime: Optional[dict]) -> Dict[str, int]:
    """Best-effort map of ``platform -> host TCP port`` for one profile's gateway.

    Reads the platforms the running gateway reported in its
    ``gateway_state.json`` and resolves each port-binding platform's port from
    the profile's ``config.yaml`` (top-level ``platforms:`` wins over
    ``gateway.platforms:``, matching ``load_gateway_config`` precedence),
    falling back to the adapter default.  Display-only: env-var port overrides
    (e.g. ``WEBHOOK_PORT`` in that profile's .env) are not resolved here.
    """
    platforms = (runtime or {}).get("platforms") or {}
    active = [
        name for name, state in platforms.items()
        if name in _PORT_BINDING_PLATFORM_PORTS
        and isinstance(state, dict)
        and state.get("state") not in _PLATFORM_DEAD_STATES
    ]
    if not active:
        return {}

    blocks: Dict[str, dict] = {}
    try:
        # Multi-profile probe: load_config() targets the ACTIVE profile's
        # home, so read the probed profile's file via the raw primitive.
        from hermes_cli.config import read_user_config_raw
        cfg = read_user_config_raw(profile_home / "config.yaml")
        gateway_cfg = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
        # gateway.platforms first, top-level platforms second — later wins,
        # matching the precedence in gateway.config.load_gateway_config().
        for src in ((gateway_cfg or {}).get("platforms"), cfg.get("platforms")):
            if not isinstance(src, dict):
                continue
            for plat_name, plat_block in src.items():
                if isinstance(plat_block, dict):
                    blocks.setdefault(plat_name, {}).update(plat_block)
    except Exception:
        blocks = {}

    ports: Dict[str, int] = {}
    for name in active:
        port_key, default_port = _PORT_BINDING_PLATFORM_PORTS[name]
        block = blocks.get(name) or {}
        extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
        raw = block.get(port_key, (extra or {}).get(port_key, default_port))
        try:
            ports[name] = int(raw)
        except (TypeError, ValueError):
            ports[name] = default_port
    return ports


def _collect_profile_gateway_topology() -> Dict[str, Any]:
    """Enumerate profiles and the gateways serving them for ``/api/status``.

    Returns ``{"profiles": [...], "gateway_mode": ..., "gateways": [...]}``:

    * ``profiles`` — every profile on the host (default + named), from
      ``profiles_to_serve(True)`` (the cheap enumeration chokepoint — no
      per-profile config reads or skill counts).
    * ``gateways`` — one entry per profile with a LIVE gateway process:
      ``{"profile", "ports", "served_profiles"?}``.  Liveness reuses
      ``_check_gateway_running`` so this agrees with the profiles sidebar.
    * ``gateway_mode`` — ``"multiplex"`` when the default gateway serves
      multiple profiles (gateway.multiplex_profiles), ``"single"`` for one
      live gateway, ``"multiple"`` for independent per-profile gateways,
      ``"none"`` when nothing is running.
    """
    try:
        from hermes_cli.profiles import _check_gateway_running, profiles_to_serve
        from gateway.status import read_runtime_status
        homes = profiles_to_serve(True)
    except Exception:
        _log.debug("profile/gateway topology enumeration failed", exc_info=True)
        return {"profiles": [], "gateway_mode": "unknown", "gateways": []}

    profile_names = [name for name, _home in homes]
    gateways: List[Dict[str, Any]] = []
    multiplex = False
    for name, home in homes:
        try:
            if not _check_gateway_running(home):
                continue
        except Exception:
            continue
        try:
            runtime = read_runtime_status(home / "gateway_state.json")
        except Exception:
            runtime = None
        served = [str(p) for p in ((runtime or {}).get("served_profiles") or [])]
        if name == "default" and len(served) > 1:
            multiplex = True
        entry: Dict[str, Any] = {
            "profile": name,
            "ports": _profile_platform_ports(home, runtime),
        }
        if served:
            entry["served_profiles"] = served
        gateways.append(entry)

    if multiplex:
        mode = "multiplex"
    elif len(gateways) > 1:
        mode = "multiple"
    elif len(gateways) == 1:
        mode = "single"
    else:
        mode = "none"

    return {"profiles": profile_names, "gateway_mode": mode, "gateways": gateways}


# /api/status is polled ~1/s by the desktop app while it waits for the backend
# (and again by the dashboard badge). Each uncached call above walks 7+ profile
# homes (yaml.safe_load with the pure-Python loader + psutil process-table
# probes + realpath walks) inside the default executor; concurrent polls pile
# up and hold the GIL for 14-16s, starving the event loop — the desktop WS
# never receives gateway.ready and boot fails ("event loop stalled ... GIL
# pressure suspected"). Topology changes on gateway start/stop, so a short TTL
# cache with a collapse lock keeps the scan to one per window. The cache also
# remembers which collector produced the entry: tests monkeypatch
# _collect_profile_gateway_topology per case, and the identity check keeps
# them hermetic without needing a reset hook (a swapped collector is a miss).
_TOPOLOGY_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None, "fn": None}
_TOPOLOGY_CACHE_LOCK = threading.Lock()
_TOPOLOGY_CACHE_TTL = 10.0


def _topology_cache_get(fn: Any) -> Optional[Dict[str, Any]]:
    if (
        _TOPOLOGY_CACHE["data"] is not None
        and _TOPOLOGY_CACHE["fn"] is fn
        and time.monotonic() - _TOPOLOGY_CACHE["ts"] < _TOPOLOGY_CACHE_TTL
    ):
        return _TOPOLOGY_CACHE["data"]
    return None


def _collect_profile_gateway_topology_cached() -> Dict[str, Any]:
    fn = _collect_profile_gateway_topology
    cached = _topology_cache_get(fn)
    if cached is not None:
        return cached
    with _TOPOLOGY_CACHE_LOCK:
        cached = _topology_cache_get(fn)
        if cached is not None:
            return cached
        data = fn()
        _TOPOLOGY_CACHE["data"] = data
        _TOPOLOGY_CACHE["fn"] = fn
        _TOPOLOGY_CACHE["ts"] = time.monotonic()
        return data


def _load_configured_gateway_platforms() -> set[str]:
    """Load connected platform names away from the asyncio event loop.

    The first ``load_gateway_config()`` call performs platform discovery and
    can take longer than Desktop's WebSocket connect timeout on Windows.  This
    helper is synchronous by design; ``get_status`` runs it in Starlette's
    worker pool so a concurrent ``/api/ws`` handshake can still complete.
    """
    from gateway.config import load_gateway_config

    gateway_config = load_gateway_config()
    return {platform.value for platform in gateway_config.get_connected_platforms()}

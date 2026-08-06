"""Display / agent config loaders (moved verbatim from server.py).

Wave-1 extraction (shard s2, cluster c17): the pure config-resolution
helpers the TUI surfaces use (approval mode, statusbar, mouse tracking,
reasoning, service tier, provider routing, toolsets, ...).  Bodies are
byte-identical to their pre-split server.py form; they are rebound onto
server.py's globals at install time - see method_ctx.py and register().
"""

import os
import types


_STATUSBAR_MODES = frozenset({"off", "top", "bottom"})



_APPROVAL_MODES = frozenset({"manual", "smart", "off"})



def _load_approval_mode() -> str:
    """Resolve the effective ``approvals.mode`` for the TUI surface.

    Delegates to the canonical resolver in ``tools.approval``
    (``_get_approval_mode``) so mode resolution cannot drift per surface —
    the same normalization, defaults, and config precedence the approval
    gate itself uses (see ``tools/approval.py``).

    Previously this re-read the config raw via ``_load_cfg`` +
    ``_deep_merge(DEFAULT_CONFIG, ...)`` and normalized locally, which
    could disagree with the gate's own view of the mode (e.g. the
    canonical ``hermes_cli.config.load_config`` path applies managed-scope
    overlays and ``${VAR}`` env expansion that the TUI's raw YAML read did
    not fully mirror).
    """
    from tools.approval import _get_approval_mode

    mode = _get_approval_mode()
    return mode if mode in _APPROVAL_MODES else "manual"



def _coerce_statusbar(raw) -> str:
    if raw is False:
        return "off"
    if isinstance(raw, str) and (s := raw.strip().lower()) in _STATUSBAR_MODES:
        return s
    return "top"



_MOUSE_TRACKING_ALIASES = {
    "0": "off",
    "1": "all",
    "all": "all",
    "any": "all",
    "button": "buttons",
    "buttons": "buttons",
    "click": "buttons",
    "false": "off",
    "full": "all",
    "no": "off",
    "off": "off",
    "on": "all",
    "scroll": "wheel",
    "true": "all",
    "wheel": "wheel",
    "yes": "all",
}



def _display_mouse_tracking(display: dict) -> str:
    """Resolve display.mouse_tracking to one of ``off|wheel|buttons|all``.

    Boolean values keep their legacy meaning (``True`` → ``all``, ``False`` →
    ``off``). The ``wheel`` preset (DEC 1000+1006) is the tmux-friendly
    subset — wheel + click only, no hover events to trigger prompt-row
    clipboard probes. Legacy ``tui_mouse`` is honored only when
    ``mouse_tracking`` is absent.
    """
    if not isinstance(display, dict):
        return "all"
    if "mouse_tracking" in display:
        raw = display.get("mouse_tracking")
    else:
        raw = display.get("tui_mouse", True)
    if raw is False or raw == 0:
        return "off"
    if raw is True or raw is None:
        return "all"
    if isinstance(raw, (int, float)):
        return "all"
    if isinstance(raw, str):
        return _MOUSE_TRACKING_ALIASES.get(raw.strip().lower(), "all")
    return "all"



def _load_reasoning_config(model: str = "") -> dict | None:
    """Load reasoning effort from config.yaml, respecting per-model overrides.

    Thin wrapper over the shared chokepoint
    :func:`hermes_constants.resolve_reasoning_config` (per-model override >
    global ``agent.reasoning_effort``; YAML boolean False = disabled).
    Closes #21256.
    """
    from hermes_constants import resolve_reasoning_config

    return resolve_reasoning_config(_load_cfg(), model)



def _load_service_tier() -> str | None:
    raw = (
        str((_load_cfg().get("agent") or {}).get("service_tier", "") or "")
        .strip()
        .lower()
    )
    if not raw or raw in {"normal", "default", "standard", "off", "none"}:
        return None
    if raw in {"fast", "priority", "on"}:
        return "priority"
    return None



def _load_provider_routing() -> dict:
    """OpenRouter provider-routing prefs from config.yaml (``provider_routing``).

    Parity with the messaging gateway (``gateway/run.py::_load_provider_routing``)
    and the classic CLI: without this the desktop/TUI backend builds agents with
    no routing prefs, so OpenRouter falls back to its default (effectively random)
    provider selection even when the user configured ``provider_routing``.
    """
    try:
        return _load_cfg().get("provider_routing", {}) or {}
    except Exception:
        return {}



def _load_show_reasoning() -> bool:
    # Fallback True — keep in sync with DEFAULT_CONFIG display.show_reasoning
    # (this loader reads the raw user YAML without the DEFAULT_CONFIG merge).
    return bool((_load_cfg().get("display") or {}).get("show_reasoning", True))



def _load_memory_notifications() -> str:
    """Self-improvement review notification mode from config.yaml.

    Parity with the messaging gateway (``gateway/run.py``) and the classic CLI:
    ``display.memory_notifications`` controls whether the background review's
    "💾 Self-improvement review: …" summary is surfaced. Without this the
    TUI/desktop backend always behaved as ``"on"`` and silently ignored a user
    who set ``off``. Accepts ``off`` / ``on`` (default) / ``verbose``; a bool is
    normalized for back-compat.
    """
    raw = (_load_cfg().get("display") or {}).get("memory_notifications")
    if isinstance(raw, bool):
        return "on" if raw else "off"
    return str(raw).lower() if raw else "on"



def _load_tool_progress_mode() -> str:
    env = os.environ.get("HERMES_TUI_TOOL_PROGRESS", "").strip().lower()
    if env in {"off", "new", "all", "verbose"}:
        return env
    raw = (_load_cfg().get("display") or {}).get("tool_progress", "all")
    if raw is False:
        return "off"
    if raw is True:
        return "all"
    mode = str(raw or "all").strip().lower()
    return mode if mode in {"off", "new", "all", "verbose"} else "all"



def _load_enabled_toolsets() -> list[str] | None:
    explicit = [
        item.strip()
        for item in os.environ.get("HERMES_TUI_TOOLSETS", "").split(",")
        if item.strip()
    ]
    cfg = None
    fallback_notice = None

    # Coding posture (base Hermes): with no explicit pin, collapse to the
    # coding toolset (+ enabled MCP servers) when sitting in a code workspace.
    # The desktop app and `hermes --tui` both land here. See
    # agent/coding_context.py. No config is loaded yet at this point, so we let
    # coding_selection() load it lazily (cli.py passes its already-resolved
    # CLI_CONFIG instead, purely to avoid a redundant read).
    if not explicit:
        try:
            from agent.coding_context import coding_selection

            selection = coding_selection(platform=_resolve_session_platform())
            if selection is not None:
                # Fold in `project` here too: this is a GUI-only resolver, and
                # the focus-mode coding posture returns before the fallback path
                # that normally adds it — without this the desktop loses the
                # project tools exactly when sitting in a repo (see below).
                return sorted({*selection, "project"})
        except Exception:
            pass

    try:
        from toolsets import validate_toolset
    except Exception:
        validate_toolset = None

    if explicit and validate_toolset is not None:
        built_in = [name for name in explicit if validate_toolset(name)]
        unresolved = [name for name in explicit if name not in built_in]

        if unresolved:
            try:
                from hermes_cli.plugins import discover_plugins

                discover_plugins()
                plugin_valid = [name for name in unresolved if validate_toolset(name)]
            except Exception:
                plugin_valid = []

            if plugin_valid:
                built_in.extend(plugin_valid)
                unresolved = [name for name in unresolved if name not in plugin_valid]

        if any(name in {"all", "*"} for name in built_in):
            ignored = [name for name in explicit if name not in {"all", "*"}]
            if ignored:
                print(
                    "[tui] HERMES_TUI_TOOLSETS=all enables every toolset; "
                    f"ignoring additional entries: {', '.join(ignored)}",
                    file=sys.stderr,
                    flush=True,
                )
            return None

        if not unresolved:
            return built_in

        mcp_names: set[str] = set()
        mcp_disabled: set[str] = set()
        try:
            from hermes_cli.config import read_raw_config
            from hermes_cli.tools_config import _parse_enabled_flag

            raw_cfg = read_raw_config()
            mcp_servers = (
                raw_cfg.get("mcp_servers")
                if isinstance(raw_cfg.get("mcp_servers"), dict)
                else {}
            )
            for name, server_cfg in mcp_servers.items():
                if not isinstance(server_cfg, dict):
                    continue
                if _parse_enabled_flag(server_cfg.get("enabled", True), default=True):
                    mcp_names.add(str(name))
                else:
                    mcp_disabled.add(str(name))
        except Exception:
            mcp_names = set()
            mcp_disabled = set()

        mcp_valid = [name for name in unresolved if name in mcp_names]
        disabled = [name for name in unresolved if name in mcp_disabled]
        unknown = [
            name
            for name in unresolved
            if name not in mcp_names and name not in mcp_disabled
        ]
        valid = built_in + mcp_valid

        if unknown:
            print(
                f"[tui] ignoring unknown HERMES_TUI_TOOLSETS entries: {', '.join(unknown)}",
                file=sys.stderr,
                flush=True,
            )
        if disabled:
            print(
                "[tui] ignoring disabled MCP servers in HERMES_TUI_TOOLSETS "
                "(set enabled: true in config.yaml to use): "
                f"{', '.join(disabled)}",
                file=sys.stderr,
                flush=True,
            )

        if valid:
            return valid

        fallback_notice = (
            "[tui] no valid HERMES_TUI_TOOLSETS entries; using configured CLI toolsets"
        )

    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        cfg = cfg if cfg is not None else load_config()

        # Runtime toolset resolution must include default MCP servers so the
        # agent can actually call them. Passing ``False`` here is the
        # config-editing variant — used when we need to persist a toolset
        # list without baking in implicit MCP defaults. Using the wrong
        # variant at agent creation time makes MCP tools silently missing
        # from the TUI. See PR #3252 for the original design split.
        enabled = _get_platform_tools(cfg, "cli", include_default_mcp_servers=True)
        if fallback_notice is not None:
            print(fallback_notice, file=sys.stderr, flush=True)
        if not enabled:
            return None
        # The desktop Project tools are off _HERMES_CORE_TOOLS (every other
        # platform would carry their schema for nothing), so the platform
        # recovery above — which keys off hermes-cli's tool universe — can't
        # surface them. This resolver runs ONLY in the desktop/TUI gateway, so
        # folding in the `project` toolset here is the gate that exposes them on
        # exactly the surface that can follow a project move.
        return sorted(enabled | {"project"})
    except Exception:
        if fallback_notice is not None:
            print(
                "[tui] no valid HERMES_TUI_TOOLSETS entries and configured CLI toolsets could not be loaded; enabling all toolsets",
                file=sys.stderr,
                flush=True,
            )
        return None



_MOVED = (
    "_load_approval_mode",
    "_coerce_statusbar",
    "_display_mouse_tracking",
    "_load_reasoning_config",
    "_load_service_tier",
    "_load_provider_routing",
    "_load_show_reasoning",
    "_load_memory_notifications",
    "_load_tool_progress_mode",
    "_load_enabled_toolsets",
)


_EXTRA = (
    "_STATUSBAR_MODES",
    "_APPROVAL_MODES",
    "_MOUSE_TRACKING_ALIASES",
)


def register(server) -> None:
    """Rebind this module's moved functions onto ``server``'s globals.

    Mirrors method_ctx.HandlerRegistry.install: the moved bodies close over
    server.py module globals (``_load_cfg``, ``_broadcast_global_event``, ...),
    so they are rebound with types.FunctionType onto the server namespace and
    assigned back as module attributes - callers keep ``server.<name>`` /
    plain-global resolution, and ``global`` statements inside the bodies keep
    mutating server.py state exactly as before the split.
    """
    g = vars(server)
    for _name in _MOVED:
        _fn = globals()[_name]
        _real = types.FunctionType(
            _fn.__code__, g, _fn.__name__, _fn.__defaults__, _fn.__closure__
        )
        _real.__kwdefaults__ = _fn.__kwdefaults__
        _real.__doc__ = _fn.__doc__
        _real.__dict__.update(_fn.__dict__)
        g[_name] = _real
    # State containers (e.g. _CHANGE_WATCHES) reference the moved probe
    # functions; swap the module-original objects for the rebound twins so
    # the containers keep working from the server namespace.
    _rebound = {id(globals()[n]): g[n] for n in _MOVED}

    def _remap(value):
        if isinstance(value, tuple):
            return tuple(_remap(v) for v in value)
        if isinstance(value, dict):
            return {k: _remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_remap(v) for v in value]
        if id(value) in _rebound:
            return _rebound[id(value)]
        return value

    for _name in _EXTRA:
        g[_name] = _remap(globals()[_name])

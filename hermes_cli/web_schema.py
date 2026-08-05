"""Schema/config rendering for the dashboard.

Extracted from hermes_cli/web_server.py (god-file slice R1-C6, epic #78791):
the config-schema helpers (_build_schema_from_config, _SCHEMA_OVERRIDES,
category merge/order, provider options) that render the dashboard's dynamic
config form.  Pure helper module: module-level functions, no app coupling.
"""

from typing import Any, Dict, List

from hermes_cli.config import cfg_get, DEFAULT_CONFIG, load_config

def _memory_provider_options() -> List[str]:
    """Discovered memory providers for the ``memory.provider`` select.

    Directory-scan only (no provider imports), so it's safe at module import
    time. ``""`` (built-in only) is always first; discovery failures degrade to
    the bundled defaults rather than dropping the field. The literal
    ``builtin`` alias is deliberately NOT offered — built-in memory is not a
    provider plugin, and ``_normalize_memory_provider_name`` already maps any
    legacy ``builtin``/``built-in``/``none`` value back to ``""`` (#49513).
    """
    options = [""]
    try:
        from plugins.memory import list_memory_provider_names

        options.extend(list_memory_provider_names())
    except Exception:
        options.extend(["honcho"])
    # Dedupe, preserve order
    return list(dict.fromkeys(options))


def _timezone_options() -> List[str]:
    """Return sorted IANA timezone identifiers, cached at import time."""
    try:
        import zoneinfo
        return sorted(zoneinfo.available_timezones()) or ["UTC"]
    except Exception:  # pragma: no cover
        return ["UTC"]


_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "timezone": {
        "type": "select",
        "description": "IANA timezone (e.g. America/New_York). Blank uses the system timezone.",
        "options": _timezone_options(),
        "searchable": True,
        "clearable": True,
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": _memory_provider_options(),
    },
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "vercel_sandbox", "singularity"],
    },
    "terminal.vercel_runtime": {
        "type": "select",
        "description": "Vercel Sandbox runtime",
        "options": ["node24", "node22", "python3.13"],  # sync with _SUPPORTED_VERCEL_RUNTIMES in terminal_tool.py
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "proxy.enabled": {
        "type": "boolean",
        "description": (
            "Docker-only egress credential firewall. Requires `hermes egress setup` "
            "and `hermes egress start`; Modal/SSH/Daytona are not wired yet."
        ),
        "category": "security",
    },
    "proxy.credential_source": {
        "type": "select",
        "description": "Where iron-proxy loads real upstream secrets at start time",
        "options": ["env", "bitwarden"],
        "category": "security",
    },
    "proxy.enforce_on_docker": {
        "type": "boolean",
        "description": "Refuse Docker sandboxes when egress is enabled but not configured/running",
        "category": "security",
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "xai", "minimax", "mistral", "gemini", "neutts", "kittentts", "piper"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        # "mistral" temporarily removed — mistralai PyPI package quarantined
        # (malicious 2.4.6 release on 2026-05-12). Restore once available.
        "options": ["local", "groq", "openai", "xai", "elevenlabs"],
    },
    "stt.local.model": {
        "type": "select",
        "description": "Local faster-whisper model size",
        "options": ["tiny", "base", "small", "medium", "large-v3"],
    },
    "stt.groq.model": {
        "type": "select",
        "description": "Groq Whisper model",
        "options": ["whisper-large-v3-turbo", "whisper-large-v3", "distil-whisper-large-v3-en"],
    },
    "stt.openai.model": {
        "type": "select",
        "description": "OpenAI transcription model",
        "options": ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-transcribe"],
    },
    "stt.elevenlabs.model_id": {
        "type": "select",
        "description": "ElevenLabs Scribe model",
        "options": ["scribe_v2", "scribe_v1"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "dashboard.theme": {
        "type": "select",
        "description": "Web dashboard visual theme",
        "options": ["default", "midnight", "ember", "mono", "cyberpunk", "rose"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["interrupt", "queue", "steer"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["manual", "smart", "off"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    },
    "updates.non_interactive_local_changes": {
        "type": "select",
        "description": (
            "When the chat app / gateway updates Hermes (no terminal prompt), "
            "what to do with uncommitted local source edits. 'stash' keeps them "
            "and re-applies them after the update; 'discard' throws them away. "
            "Terminal updates always ask, regardless of this setting."
        ),
        "options": ["stash", "discard"],
    },
    "updates.refresh_cua_driver": {
        "type": "boolean",
        "description": (
            "Refresh an already-installed cua-driver during hermes update. "
            "Disable this on non-admin macOS accounts where /Applications is "
            "not writable."
        ),
    },
    "browser.headed": {
        "type": "boolean",
        "description": "Run the local browser in headed mode (visible window). Also keeps the window open between turns; idle sessions are still reaped after browser.inactivity_timeout.",
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "dashboard": "display",
    "code_execution": "agent",
    "prompt_caching": "agent",
    "goals": "agent",
    "updates": "general",
    # `onboarding.profile_build` is the only schema-surfaced onboarding field
    # (`onboarding.seen` is an internal latch dict, not a user setting), so fold
    # it into the agent tab rather than spawning a one-field orphan category.
    "onboarding": "agent",
    # Only `telegram.reactions` currently lives under telegram — fold it in
    # with the other messaging-platform config (discord) so it isn't an
    # orphan tab of one field.
    "telegram": "discord",
    # `mcp.auto_reload_on_config_change` is the only schema-surfaced mcp
    # runtime field (server definitions live under mcp_servers, edited via
    # the MCP tab) — fold it into the agent tab rather than spawning a
    # one-field orphan category.
    "mcp": "agent",
    # `computer_use.cua_telemetry` is the only schema-surfaced computer_use
    # field — fold it into the agent tab rather than spawning a one-field
    # orphan category.
    "computer_use": "agent",
    # `telemetry.shared_metrics.enabled` is the only schema-surfaced telemetry
    # field — fold it into security alongside the other privacy-posture toggles.
    "telemetry": "security",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in {"_config_version"}:
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)

# Inject virtual fields that don't live in DEFAULT_CONFIG but are surfaced
# by the normalize/denormalize cycle.  Insert model_context_length right after
# the "model" key so it renders adjacent in the frontend.
_mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
_ordered_schema: Dict[str, Dict[str, Any]] = {}
for _k, _v in CONFIG_SCHEMA.items():
    _ordered_schema[_k] = _v
    if _k == "model":
        _ordered_schema["model_context_length"] = _mcl_entry
CONFIG_SCHEMA = _ordered_schema


def _is_command_provider_block(value: Any) -> bool:
    """Return True when *value* declares a command-type voice provider.

    Mirrors the runtime discriminators
    (``tools.tts_tool._is_command_provider_config`` /
    ``tools.transcription_tools._is_command_stt_provider_config``) and the
    desktop's ``isCommandProvider`` in
    ``apps/desktop/src/app/settings/helpers.ts``: ``type`` is OPTIONAL and
    case/space-insensitive (absent or normalizing to ``"command"``), and
    ``command`` MUST be a non-empty string. Built-in blocks (which carry
    ``voice``/``model`` and no ``command``) and the ``providers`` container
    itself are rejected.
    """
    if not isinstance(value, dict):
        return False
    ptype = str(value.get("type") or "").strip().lower()
    if ptype and ptype != "command":
        return False
    command = value.get("command")
    return isinstance(command, str) and bool(command.strip())


def _custom_provider_options(
    kind: str,
    builtin_names: List[str],
    cfg: Dict[str, Any],
) -> List[str]:
    """Return a merged provider option list without hard-coding vendor names.

    *kind* is ``"tts"`` or ``"stt"``. The result keeps the built-in display
    names first (original order — NOT re-sorted), then appends:

    1. Command-type providers declared under the canonical
       ``<kind>.providers.<name>`` location, plus the legacy top-level
       ``<kind>.<name>`` fallback — exactly the dual resolution the runtime
       performs in ``_get_named_provider_config`` /
       ``_get_named_stt_provider_config``. Names colliding with a RUNTIME
       built-in are excluded case-insensitively (the runtime rejects a
       built-in name as a command provider before any config lookup), so a
       ``providers.EDGE`` command block is not offered.
    2. Plugin-registered provider names from ``agent.tts_registry`` /
       ``agent.transcription_registry`` — opportunistic only: plugins
       register at runtime via ``ctx.register_tts_provider()``, and this
       process does not necessarily call ``discover_plugins()``, so the
       registry may legitimately be empty here. (There is no static
       ``provides: [tts]`` manifest convention to scan — real manifests only
       carry ``provides_tools``/``provides_hooks``.)
    3. The current ``<kind>.provider`` value when not already present — a
       custom name that only appears as the active provider stays
       selectable (matches desktop ``enumOptionsFor``'s current-value
       preservation).

    Guard semantics deliberately mirror
    ``apps/desktop/src/app/settings/helpers.ts:commandProviderNames`` so the
    backend schema (web dashboard) and the desktop client agree on which
    names are offered.
    """
    names = [str(n) for n in builtin_names]
    seen = {n.strip().lower() for n in names}

    # Guard against the RUNTIME built-in sets, not the display shortlist
    # above: the display list drifts from the runtime sets (e.g. omits
    # ``deepinfra``), and filtering on it would offer names the runtime
    # would never honour as command providers.
    if kind == "tts":
        from tools.tts_tool import BUILTIN_TTS_PROVIDERS as _runtime_builtins
    else:
        from tools.transcription_tools import BUILTIN_STT_PROVIDERS as _runtime_builtins

    def _add(name: Any) -> None:
        if not isinstance(name, str):
            return
        stripped = name.strip()
        key = stripped.lower()
        if stripped and key not in seen:
            names.append(stripped)
            seen.add(key)

    section = cfg.get(kind)
    if not isinstance(section, dict):
        section = {}

    # Canonical nested location first, then the legacy top-level fallback —
    # the same order the runtime resolves them in.
    candidate_blocks: List[Any] = []
    providers_map = section.get("providers")
    if isinstance(providers_map, dict):
        candidate_blocks.append(providers_map)
    candidate_blocks.append(
        {k: v for k, v in section.items() if k != "providers"}
    )
    for block in candidate_blocks:
        for name, value in block.items():
            if (
                isinstance(name, str)
                and name.strip().lower() not in _runtime_builtins
                and _is_command_provider_block(value)
            ):
                _add(name)

    # Plugin-registered providers (only populated when plugins are loaded in
    # this process). Registry names can never collide with built-ins — the
    # registries reject such registrations.
    try:
        if kind == "tts":
            from agent.tts_registry import list_providers as _list_voice_providers
        else:
            from agent.transcription_registry import list_providers as _list_voice_providers
        for _p in _list_voice_providers():
            _add(getattr(_p, "name", None))
    except Exception:  # pragma: no cover - registry import should not break schema
        pass

    # Current-value preservation (``cfg_get`` takes *keys*, not dotted paths).
    _add(cfg_get(cfg, kind, "provider"))

    return names


def _memory_provider_schema_options(cfg: Dict[str, Any]) -> List[str]:
    """Discovered memory providers for a per-request schema merge.

    Reuses the cheap directory scan of :func:`_memory_provider_options` and
    additionally preserves the currently-configured provider, so a value
    selected in config but not (yet) discoverable — e.g. a plugin removed from
    disk — never silently vanishes from the dropdown.
    """
    options = _memory_provider_options()

    from hermes_cli.web_server import _normalize_memory_provider_name

    memory = cfg.get("memory")
    configured = memory.get("provider") if isinstance(memory, dict) else None
    current = _normalize_memory_provider_name(configured)

    if current and current not in options:
        options = [*options, current]

    return options


def _schema_with_dynamic_provider_options() -> Dict[str, Dict[str, Any]]:
    """Return CONFIG_SCHEMA with per-request discovery-driven options merged.

    Some ``*.provider`` selects have options that are discovered at runtime
    (voice backends via the tts/stt registries + config.yaml command
    providers; memory providers via a plugin-dir scan). The module-level
    ``_SCHEMA_OVERRIDES`` freezes those lists at import time, so a provider
    installed after the server started never appears. This recomputes them at
    request time — reflecting the CURRENT config.yaml, the profile-scoped
    config when the request carries a ``profile`` param, and mid-session
    plugin installs — for every surface that reads the schema (desktop, CLI,
    dashboard), with no extra frontend round-trips.

    The module-level ``CONFIG_SCHEMA`` is never mutated; entries that change
    are shallow-copied onto a copied mapping.
    """
    try:
        cfg = load_config()
    except Exception:  # pragma: no cover - schema must survive config errors
        return CONFIG_SCHEMA

    overlay: Dict[str, Dict[str, Any]] = {}

    def merge(key: str, options: List[str]) -> None:
        entry = CONFIG_SCHEMA.get(key)

        if isinstance(entry, dict) and isinstance(entry.get("options"), list) and options != entry["options"]:
            overlay[key] = {**entry, "options": options}

    for kind in ("tts", "stt"):
        entry = CONFIG_SCHEMA.get(f"{kind}.provider")
        existing = entry.get("options") if isinstance(entry, dict) else None

        if isinstance(existing, list):
            merge(f"{kind}.provider", _custom_provider_options(kind, list(existing), cfg))

    merge("memory.provider", _memory_provider_schema_options(cfg))

    if not overlay:
        return CONFIG_SCHEMA

    return {**CONFIG_SCHEMA, **overlay}

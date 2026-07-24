"""Pure platform-configuration semantics shared by Gateway and readiness.

This module intentionally imports no platform adapter, plugin module, optional
SDK, or plugin entry point.  It answers one narrow question: whether static
configuration and credential *presence* are sufficient to prove that a
platform is configured.  Runtime observation and runtime connection remain
separate facts owned by ``gateway_state.json`` and the running Gateway.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import os
from pathlib import Path
from typing import Any

from hermes_cli.secret_validation import has_usable_secret


logger = logging.getLogger(__name__)


class StaticConfigurationState(str, Enum):
    """Import-free result for a platform configuration declaration."""

    CONFIGURED = "configured"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConfigurationEvidence:
    """One required value, available from config paths or environment aliases."""

    config_paths: tuple[str, ...] = ()
    env_vars: tuple[str, ...] = ()
    relative_files: tuple[str, ...] = ()
    json_file_paths: tuple[tuple[str, str], ...] = ()
    min_length: int = 1
    truthy: bool = False
    usable_secret: bool = False


@dataclass(frozen=True)
class PlatformConfigurationSpec:
    """Declarative static requirements for one platform.

    ``any_of`` is a tuple of alternatives.  Every evidence item inside one
    alternative must be satisfied; satisfying any complete alternative proves
    configuration.  A spec with no alternatives represents a credentialless
    platform whose explicit enable declaration is sufficient.
    """

    any_of: tuple[tuple[ConfigurationEvidence, ...], ...] = ()
    complete: bool = True
    implicit_enable: bool = True
    enable_env_var: str = ""


@dataclass(frozen=True)
class PlatformBlocks:
    """Merged platform declarations plus independently recoverable read errors."""

    blocks: dict[str, dict[str, Any]]
    source_errors: tuple[str, ...] = field(default_factory=tuple)


def _e(
    *config_paths: str,
    env: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    json_files: tuple[tuple[str, str], ...] = (),
    min_length: int = 1,
    truthy: bool = False,
    usable_secret: bool = False,
) -> ConfigurationEvidence:
    return ConfigurationEvidence(
        config_paths=tuple(config_paths),
        env_vars=env,
        relative_files=files,
        json_file_paths=json_files,
        min_length=min_length,
        truthy=truthy,
        usable_secret=usable_secret,
    )


def _one(*evidence: ConfigurationEvidence) -> PlatformConfigurationSpec:
    return PlatformConfigurationSpec(any_of=(tuple(evidence),))


def _alternatives(
    *groups: tuple[ConfigurationEvidence, ...],
) -> PlatformConfigurationSpec:
    return PlatformConfigurationSpec(any_of=tuple(groups))


# This is not a second status-only heuristic table.  GatewayConfig and the
# import-free status reader both consume these same immutable requirements.
# Adapter-specific runtime checks remain in adapters; static credential
# presence and non-secret shape live here.
BUILTIN_PLATFORM_SPECS: dict[str, PlatformConfigurationSpec] = {
    "telegram": _one(_e("token", "extra.token", env=("TELEGRAM_BOT_TOKEN",))),
    "discord": _one(_e("token", env=("DISCORD_BOT_TOKEN",))),
    # WhatsApp's historical enrollment decision belongs to its bundled
    # callback (enabled-with-extras or WHATSAPP_ENABLED). There is no complete
    # declarative equivalent for the YAML "any extras" branch, so readiness
    # stays conservative rather than redefining that callback's contract.
    "whatsapp": PlatformConfigurationSpec(complete=False),
    "whatsapp_cloud": _one(
        _e(
            "extra.phone_number_id",
            "phone_number_id",
            env=("WHATSAPP_CLOUD_PHONE_NUMBER_ID",),
        ),
        _e(
            "extra.access_token",
            "access_token",
            env=("WHATSAPP_CLOUD_ACCESS_TOKEN",),
        ),
    ),
    "slack": _one(_e("token", env=("SLACK_BOT_TOKEN",))),
    "signal": _one(_e("extra.http_url", "http_url", env=("SIGNAL_HTTP_URL",))),
    "mattermost": _one(
        _e("token", env=("MATTERMOST_TOKEN",)),
        _e("extra.url", "url", env=("MATTERMOST_URL",)),
    ),
    "matrix": _one(
        _e("extra.homeserver", "homeserver", env=("MATRIX_HOMESERVER",)),
        _e(
            "token",
            "extra.password",
            "password",
            env=("MATRIX_ACCESS_TOKEN", "MATRIX_PASSWORD"),
        ),
    ),
    "homeassistant": _one(_e("token", env=("HASS_TOKEN",))),
    "email": _one(_e("extra.address", "address", env=("EMAIL_ADDRESS",))),
    "sms": _one(
        _e("extra.account_sid", "account_sid", env=("TWILIO_ACCOUNT_SID",)),
        _e("api_key", "extra.auth_token", env=("TWILIO_AUTH_TOKEN",)),
    ),
    "dingtalk": _one(
        _e("extra.client_id", "client_id", env=("DINGTALK_CLIENT_ID",)),
        _e(
            "extra.client_secret",
            "client_secret",
            env=("DINGTALK_CLIENT_SECRET",),
        ),
    ),
    "api_server": _one(
        _e(
            "extra.key",
            "key",
            env=("API_SERVER_KEY",),
            min_length=16,
            usable_secret=True,
        )
    ),
    "webhook": PlatformConfigurationSpec(
        implicit_enable=False,
        enable_env_var="WEBHOOK_ENABLED",
    ),
    "msgraph_webhook": PlatformConfigurationSpec(
        any_of=(
            (
                _e(
                    "extra.client_state",
                    "client_state",
                    env=("MSGRAPH_WEBHOOK_CLIENT_STATE",),
                ),
            ),
        ),
        implicit_enable=False,
        enable_env_var="MSGRAPH_WEBHOOK_ENABLED",
    ),
    "feishu": _one(
        _e("extra.app_id", "app_id", env=("FEISHU_APP_ID",)),
        _e("extra.app_secret", "app_secret", env=("FEISHU_APP_SECRET",)),
    ),
    "wecom": _one(
        _e("extra.bot_id", "bot_id", env=("WECOM_BOT_ID",)),
        _e("extra.secret", "secret", env=("WECOM_SECRET",)),
    ),
    "wecom_callback": _alternatives(
        (
            _e(
                "extra.corp_id",
                "corp_id",
                env=("WECOM_CALLBACK_CORP_ID",),
            ),
            _e(
                "extra.corp_secret",
                "corp_secret",
                env=("WECOM_CALLBACK_CORP_SECRET",),
            ),
        ),
        (_e("extra.apps", "apps"),),
    ),
    "weixin": _one(
        _e("extra.account_id", "account_id", env=("WEIXIN_ACCOUNT_ID",)),
        _e("token", "extra.token", env=("WEIXIN_TOKEN",)),
    ),
    "bluebubbles": _one(
        _e(
            "extra.server_url",
            "server_url",
            env=("BLUEBUBBLES_SERVER_URL",),
        ),
        _e("extra.password", "password", env=("BLUEBUBBLES_PASSWORD",)),
    ),
    "qqbot": _one(
        _e("extra.app_id", "app_id", env=("QQ_APP_ID",)),
        _e(
            "extra.client_secret",
            "client_secret",
            env=("QQ_CLIENT_SECRET",),
        ),
    ),
    "yuanbao": _one(
        _e(
            "extra.app_id",
            "app_id",
            env=("YUANBAO_APP_ID", "YUANBAO_APP_KEY"),
        ),
        _e(
            "extra.app_secret",
            "app_secret",
            env=("YUANBAO_APP_SECRET",),
        ),
    ),
    "relay": _one(
        _e("extra.relay_url", "extra.url", "relay_url", "url", env=("GATEWAY_RELAY_URL",))
    ),
    "irc": _one(
        _e("extra.server", "server", env=("IRC_SERVER",)),
        _e("extra.channel", "channel", env=("IRC_CHANNEL",)),
    ),
    "line": _one(
        _e(
            "extra.channel_access_token",
            "channel_access_token",
            env=("LINE_CHANNEL_ACCESS_TOKEN",),
        ),
        _e(
            "extra.channel_secret",
            "channel_secret",
            env=("LINE_CHANNEL_SECRET",),
        ),
    ),
    "teams": _one(
        _e("extra.client_id", "client_id", env=("TEAMS_CLIENT_ID",)),
        _e(
            "extra.client_secret",
            "client_secret",
            env=("TEAMS_CLIENT_SECRET",),
        ),
        _e("extra.tenant_id", "tenant_id", env=("TEAMS_TENANT_ID",)),
    ),
    "ntfy": _one(_e("extra.topic", "topic", env=("NTFY_TOPIC",))),
    "simplex": _one(_e("extra.ws_url", "ws_url", env=("SIMPLEX_WS_URL",))),
    "google_chat": _alternatives(
        (
            _e(
                "extra.http_events_url",
                "http_events_url",
                env=("GOOGLE_CHAT_HTTP_EVENTS_URL",),
            ),
        ),
        (
            _e(
                "extra.project_id",
                "project_id",
                env=("GOOGLE_CHAT_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"),
            ),
            _e(
                "extra.subscription_name",
                "subscription_name",
                env=(
                    "GOOGLE_CHAT_SUBSCRIPTION_NAME",
                    "GOOGLE_CHAT_SUBSCRIPTION",
                ),
            ),
        ),
    ),
    "photon": _one(
        _e(
            "extra.project_id",
            "project_id",
            env=("PHOTON_PROJECT_ID",),
            json_files=(
                (
                    "auth.json",
                    "credential_pool.photon_project.0.spectrum_project_id",
                ),
                (
                    "auth.json",
                    "credential_pool.photon_project.0.project_id",
                ),
            ),
        ),
        _e(
            "extra.project_secret",
            "project_secret",
            env=("PHOTON_PROJECT_SECRET",),
            json_files=(
                (
                    "auth.json",
                    "credential_pool.photon_project.0.project_secret",
                ),
            ),
        ),
    ),
    "raft": _alternatives(
        (_e("extra.bridge_token", "bridge_token"),),
        (_e("extra.profile", "profile", env=("RAFT_PROFILE",)),),
    ),
}


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off", "none", "null"})
_MISSING = object()


def _mapping_or_attrs_get(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (IndexError, TypeError, ValueError):
                return _MISSING
            continue
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            current = getattr(current, part, _MISSING)
            if current is _MISSING:
                return _MISSING
    return current


def _present(value: Any, evidence: ConfigurationEvidence) -> bool:
    if value is _MISSING or value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        if evidence.usable_secret:
            return has_usable_secret(text, min_length=evidence.min_length)
        if evidence.truthy:
            return text.lower() in _TRUTHY
        return len(text) >= evidence.min_length
    if evidence.truthy:
        return bool(value)
    if isinstance(value, (Mapping, list, tuple, set)):
        return bool(value)
    return len(str(value).strip()) >= evidence.min_length


def _evidence_is_present(
    config: Any,
    evidence: ConfigurationEvidence,
    getenv: Callable[[str], Any],
    home: Path | None,
) -> bool:
    for path in evidence.config_paths:
        if _present(_mapping_or_attrs_get(config, path), evidence):
            return True
    for name in evidence.env_vars:
        if _present(getenv(name), evidence):
            return True
    if home is not None:
        for relative in evidence.relative_files:
            try:
                if (home / relative).is_file():
                    return True
            except OSError:
                continue
        loaded_json: dict[Path, Any] = {}
        for relative, path in evidence.json_file_paths:
            file_path = home / relative
            if file_path not in loaded_json:
                try:
                    with file_path.open(encoding="utf-8") as handle:
                        loaded_json[file_path] = json.load(handle)
                except Exception:
                    loaded_json[file_path] = _MISSING
            if _present(
                _mapping_or_attrs_get(loaded_json[file_path], path),
                evidence,
            ):
                return True
    return False


def _explicit_enabled(config: Any) -> tuple[bool, bool]:
    """Return ``(was_explicit, enabled)`` for raw blocks or PlatformConfig."""
    if isinstance(config, Mapping):
        if "enabled" not in config:
            return False, False
        value = config.get("enabled")
    else:
        value = getattr(config, "enabled", False)
        return True, bool(value)

    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUTHY:
            return True, True
        if token in _FALSY:
            return True, False
        return True, False
    return True, bool(value)


def evaluate_static_configuration(
    config: Any,
    spec: PlatformConfigurationSpec | None,
    *,
    getenv: Callable[[str], Any] = os.getenv,
    home: Path | None = None,
) -> StaticConfigurationState:
    """Evaluate one declaration without executing plugin or adapter code."""
    if spec is None or not spec.complete:
        return StaticConfigurationState.UNKNOWN

    was_explicit, enabled = _explicit_enabled(config)
    if was_explicit and not enabled:
        return StaticConfigurationState.DISABLED
    if not was_explicit and spec.enable_env_var:
        raw_enable = getenv(spec.enable_env_var)
        if raw_enable is not None:
            token = str(raw_enable).strip().lower()
            if token in _FALSY:
                return StaticConfigurationState.DISABLED
            if token in _TRUTHY:
                was_explicit, enabled = True, True

    if not spec.any_of:
        if was_explicit and enabled:
            return StaticConfigurationState.CONFIGURED
        return StaticConfigurationState.DISABLED

    configured = any(
        all(_evidence_is_present(config, item, getenv, home) for item in group)
        for group in spec.any_of
    )
    if not configured:
        return StaticConfigurationState.DISABLED
    if was_explicit and enabled:
        return StaticConfigurationState.CONFIGURED
    if spec.implicit_enable:
        return StaticConfigurationState.CONFIGURED
    return StaticConfigurationState.DISABLED


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; overlay values win at every leaf."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(previous, value)
        else:
            merged[key] = value
    return merged


def _merge_platform_map(
    target: dict[str, dict[str, Any]],
    source: Any,
) -> None:
    if not isinstance(source, Mapping):
        return
    for raw_name, raw_block in source.items():
        if not isinstance(raw_name, str) or not isinstance(raw_block, Mapping):
            continue
        name = raw_name.strip()
        if not name:
            continue
        target[name] = deep_merge(target.get(name, {}), raw_block)


def _load_json_source(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle) or {}
        if not isinstance(loaded, dict):
            raise TypeError("root must be a mapping")
        return loaded, None
    except Exception as exc:
        logger.warning("Failed to load %s for platform status: %s", path, exc)
        return {}, path.name


def _load_yaml_source(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        import yaml

        with path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise TypeError("root must be a mapping")
        from hermes_cli import managed_scope

        overlaid = managed_scope.apply_managed_overlay(loaded)
        if not isinstance(overlaid, dict):
            raise TypeError("managed overlay root must be a mapping")
        return overlaid, None
    except Exception as exc:
        logger.warning("Failed to load %s for platform status: %s", path, exc)
        return {}, path.name


def load_platform_blocks(
    home: Path,
    *,
    candidate_names: Iterable[str] = (),
) -> PlatformBlocks:
    """Load and deep-merge platform declarations without plugin resolution."""
    legacy, legacy_error = _load_json_source(home / "gateway.json")
    yaml_cfg, yaml_error = _load_yaml_source(home / "config.yaml")
    blocks: dict[str, dict[str, Any]] = {}

    _merge_platform_map(blocks, legacy.get("platforms"))

    gateway_cfg = yaml_cfg.get("gateway")
    if isinstance(gateway_cfg, Mapping):
        _merge_platform_map(blocks, gateway_cfg.get("platforms"))
    _merge_platform_map(blocks, yaml_cfg.get("platforms"))

    names = set(BUILTIN_PLATFORM_SPECS)
    names.update(name for name in candidate_names if isinstance(name, str))
    if isinstance(gateway_cfg, Mapping):
        for name in names:
            nested = gateway_cfg.get(name)
            if isinstance(nested, Mapping):
                _merge_platform_map(blocks, {name: nested})
    for name in names:
        direct = yaml_cfg.get(name)
        # Top-level ``telegram:``, ``matrix:``, etc. sections are adapter
        # policy/config-bridge inputs in the canonical loader, not arbitrary
        # PlatformConfig blocks.  The one shared enrollment field consumed
        # there is ``enabled``; credential/extra evidence must come from
        # gateway.json, platforms.*, gateway.platforms.*, gateway.<name>, or
        # environment.  Merging the whole direct block here would let status
        # recognize shapes that the full loader discards.
        if isinstance(direct, Mapping) and "enabled" in direct:
            _merge_platform_map(blocks, {name: {"enabled": direct["enabled"]}})

    errors = tuple(
        error for error in (legacy_error, yaml_error) if error is not None
    )
    return PlatformBlocks(blocks=blocks, source_errors=errors)


def load_static_platform_states(
    home: Path,
    candidate_names: Iterable[str],
    *,
    specs: Mapping[str, PlatformConfigurationSpec] = BUILTIN_PLATFORM_SPECS,
    getenv: Callable[[str], Any] = os.getenv,
) -> dict[str, StaticConfigurationState]:
    """Classify runtime candidate names from static configuration evidence."""
    candidates = tuple(dict.fromkeys(str(name).strip() for name in candidate_names))
    loaded = load_platform_blocks(home, candidate_names=candidates)
    states: dict[str, StaticConfigurationState] = {}
    for name in candidates:
        spec = specs.get(name)
        if spec is None:
            states[name] = StaticConfigurationState.UNKNOWN
            continue
        block = loaded.blocks.get(name, {})
        state = evaluate_static_configuration(
            block,
            spec,
            getenv=getenv,
            home=home,
        )
        if (
            state is StaticConfigurationState.DISABLED
            and loaded.source_errors
            and not block
        ):
            state = StaticConfigurationState.UNKNOWN
        states[name] = state
    return states

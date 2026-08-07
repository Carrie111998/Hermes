"""Agent cache-busting config methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the cache-busting config cluster: the honcho identity-key extractors
and the agent-config signature used to decide cached-agent reuse.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``cls._CACHE_BUSTING_CONFIG_KEYS`` / ``cls._HONCHO_CACHE_BUSTING_KEYS`` /
``cls._HONCHO_CACHE_BUSTING_MEMO`` stay defined on ``GatewayRunner`` and
resolve via the MRO.
"""


from __future__ import annotations

from typing import Any, Dict

from hermes_cli.config import cfg_get


class GatewayAgentCacheConfigMixin:

    @classmethod
    def _empty_honcho_cache_busting_config(cls) -> dict[str, Any]:
        return {key: None for key in cls._HONCHO_CACHE_BUSTING_KEYS}

    @classmethod
    def _extract_honcho_cache_busting_config(cls) -> dict[str, Any]:
        """Extract Honcho identity keys, memoized by honcho.json mtime."""
        try:
            from plugins.memory.honcho.client import HonchoClientConfig, resolve_config_path

            path = resolve_config_path()
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            memo_key = (str(path), mtime_ns)
            cached = cls._HONCHO_CACHE_BUSTING_MEMO.get(memo_key)
            if cached is not None:
                return dict(cached)

            hcfg = HonchoClientConfig.from_global_config(config_path=path)
            aliases = hcfg.user_peer_aliases or {}
            values = {
                "honcho.peer_name": hcfg.peer_name,
                "honcho.ai_peer": hcfg.ai_peer,
                "honcho.pin_peer_name": bool(hcfg.pin_peer_name),
                "honcho.runtime_peer_prefix": hcfg.runtime_peer_prefix or "",
                "honcho.user_peer_aliases": sorted(aliases.items()) if isinstance(aliases, dict) else [],
            }
            cls._HONCHO_CACHE_BUSTING_MEMO = {memo_key: values}
            return dict(values)
        except Exception:
            return cls._empty_honcho_cache_busting_config()

    @classmethod
    def _extract_cache_busting_config(cls, user_config: dict | None) -> dict:
        """Pull values that must bust the cached agent.

        Returns a flat dict keyed by 'section.key'.  Missing config keys and
        non-dict sections yield None values, which still contribute to the
        signature (so 'absent' vs 'present-and-null' differ).

        The live tool registry generation is included too.  MCP reloads and
        dynamic MCP tool-list changes mutate the registry without necessarily
        changing config.yaml.  Cached AIAgent instances freeze their tool
        schemas at construction time, so a registry generation change must
        rebuild the agent before the next turn.
        """
        out: Dict[str, Any] = {}
        cfg = user_config if isinstance(user_config, dict) else {}
        for section, key in cls._CACHE_BUSTING_CONFIG_KEYS:
            section_val = cfg.get(section)
            if section == "checkpoints" and isinstance(section_val, bool):
                # Preserve legacy ``checkpoints: true`` behavior.  A live
                # toggle must still rebuild the cached agent.
                out[f"{section}.{key}"] = section_val if key == "enabled" else None
            elif isinstance(section_val, dict):
                out[f"{section}.{key}"] = section_val.get(key)
            else:
                out[f"{section}.{key}"] = None
        try:
            from tools.registry import registry

            out["tools.registry_generation"] = getattr(registry, "_generation", None)
        except Exception:
            out["tools.registry_generation"] = None

        # Honcho identity-mapping keys live in honcho.json, not user_config.
        # Only read that file when Honcho is the active memory provider.
        provider = cfg_get(cfg, "memory", "provider")
        if isinstance(provider, str) and provider.lower() == "honcho":
            out.update(cls._extract_honcho_cache_busting_config())
        else:
            out.update(cls._empty_honcho_cache_busting_config())

        return out

    @staticmethod
    def _agent_config_signature(
        model: str,
        runtime: dict,
        enabled_toolsets: list,
        ephemeral_prompt: str,
        cache_keys: dict | None = None,
        user_id: str | None = None,
        user_id_alt: str | None = None,
        skip_context_files: bool = False,
    ) -> str:
        """Compute a stable string key from agent config values.

        When this signature changes between messages, the cached AIAgent is
        discarded and rebuilt.  When it stays the same, the cached agent is
        reused — preserving the frozen system prompt and tool schemas for
        prompt cache hits.

        ``cache_keys`` is an optional flat dict of additional config values
        that should invalidate the cache when they change.  Callers pass
        the output of ``_extract_cache_busting_config(user_config)`` so
        edits to model.context_length / compression.* in config.yaml are
        picked up on the next gateway message without a manual restart.

        ``user_id`` and ``user_id_alt`` are the runtime user identities
        carried by the current message's gateway source.  They participate
        in the cache key because the Honcho memory provider freezes them
        into ``HonchoSessionManager`` at first-message init (see
        ``plugins/memory/honcho/__init__.py::_do_session_init``).  Without
        them in the signature, a shared-thread session_key (one in which
        ``build_session_key`` intentionally omits the participant ID,
        e.g. ``thread_sessions_per_user=False``) would reuse the cached
        AIAgent across distinct users, causing the second user's messages
        to be attributed to the first user's resolved Honcho peer.  This
        broke #27371's per-user-peer contract in multi-user gateways.
        Per-user agent rebuilds in shared threads trade prompt-cache
        warmth for correct memory attribution.
        """
        import hashlib, json as _j

        # Fingerprint the FULL credential string instead of using a short
        # prefix. OAuth/JWT-style tokens frequently share a common prefix
        # (e.g. "eyJhbGci"), which can cause false cache hits across auth
        # switches if only the first few characters are considered.
        _api_key = str(runtime.get("api_key", "") or "")
        _api_key_fingerprint = hashlib.sha256(_api_key.encode()).hexdigest() if _api_key else ""

        _cache_keys_sorted = sorted((cache_keys or {}).items())

        blob = _j.dumps(
            [
                model,
                _api_key_fingerprint,
                runtime.get("base_url", ""),
                runtime.get("provider", ""),
                runtime.get("requested_provider", ""),
                runtime.get("api_mode", ""),
                sorted(enabled_toolsets) if enabled_toolsets else [],
                # reasoning_config excluded — it's set per-message on the
                # cached agent and doesn't affect system prompt or tools.
                ephemeral_prompt or "",
                _cache_keys_sorted,
                str(user_id or ""),
                str(user_id_alt or ""),
                # skip_context_files changes the agent's frozen system prompt
                # (context files in vs out) — a toggled config edit must
                # rebuild the cached agent, not silently reuse it.
                bool(skip_context_files),
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


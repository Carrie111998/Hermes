"""Codex model discovery from API, local cache, and config."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import os

logger = logging.getLogger(__name__)

# Last-known ChatGPT Codex gate for GPT-5.6 models. Used only when discovery
# has not yet supplied a per-model ``minimal_client_version``. Not a forever
# pin — live /models + local models_cache are preferred.
_CODEX_CLI_COMPAT_FLOOR = "0.144.0"

# Process cache populated from live /models probes and ~/.codex/models_cache.json.
_minimal_client_versions: Dict[str, str] = {}
# Top-level ``client_version`` from the local Codex CLI models cache (the CLI
# that fetched the cache), used when per-model mins are absent.
_local_codex_cli_version: Optional[str] = None
_hydrated_from_local_cache = False
_live_min_version_probe_done = False


def _parse_semver_tuple(value: str) -> Optional[Tuple[int, int, int]]:
    """Parse a dotted semver prefix into (major, minor, patch)."""
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _max_semver(*versions: str) -> str:
    """Return the highest parseable semver among ``versions`` (floor on empty)."""
    best = _CODEX_CLI_COMPAT_FLOOR
    best_tuple = _parse_semver_tuple(best) or (0, 0, 0)
    for raw in versions:
        parsed = _parse_semver_tuple(raw or "")
        if parsed is None:
            continue
        if parsed > best_tuple:
            best_tuple = parsed
            best = ".".join(str(part) for part in parsed)
    return best


def remember_codex_minimal_client_versions(entries: Iterable[object]) -> None:
    """Update the process cache from Codex /models (or models_cache) entries."""
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        min_ver = item.get("minimal_client_version")
        if not isinstance(min_ver, str) or not min_ver.strip():
            continue
        if _parse_semver_tuple(min_ver) is None:
            continue
        _minimal_client_versions[slug.strip()] = min_ver.strip()


def remember_local_codex_cli_version(version: Optional[str]) -> None:
    """Remember the Codex CLI version that produced a local models cache."""
    global _local_codex_cli_version
    if not isinstance(version, str) or not version.strip():
        return
    if _parse_semver_tuple(version) is None:
        return
    _local_codex_cli_version = version.strip()


def _hydrate_minimal_versions_from_local_cache() -> None:
    """Load per-model mins + CLI version from ``~/.codex/models_cache.json`` once."""
    global _hydrated_from_local_cache
    if _hydrated_from_local_cache:
        return
    _hydrated_from_local_cache = True
    codex_home_str = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    cache_path = Path(codex_home_str).expanduser() / "models_cache.json"
    if not cache_path.exists():
        return
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    remember_local_codex_cli_version(raw.get("client_version"))
    entries = raw.get("models")
    if isinstance(entries, list):
        remember_codex_minimal_client_versions(entries)


def ensure_codex_minimal_client_versions(access_token: Optional[str] = None) -> None:
    """Best-effort warm of the minimal-client-version cache before inference.

    Order: already-warm process cache → local models_cache.json → one live
    /models probe when a token is provided and no per-model mins are known yet
    (local CLI ``client_version`` alone is only a resolve fallback). Failures
    are non-fatal; callers fall back to ``_CODEX_CLI_COMPAT_FLOOR``.
    """
    global _live_min_version_probe_done
    if _minimal_client_versions:
        return
    _hydrate_minimal_versions_from_local_cache()
    if _minimal_client_versions:
        return
    if _live_min_version_probe_done:
        return
    if access_token and isinstance(access_token, str) and access_token.strip():
        _live_min_version_probe_done = True
        _fetch_models_from_api(access_token)


def resolve_codex_compat_client_version(model: Optional[str] = None) -> str:
    """CLI-compat version to advertise on Codex inference requests.

    Prefer the selected model's catalog ``minimal_client_version``. When the
    model is unknown, use the max across cached models, then the local Codex
    CLI cache's ``client_version``, then the known GPT-5.6 floor. Always at
    least the floor.
    """
    _hydrate_minimal_versions_from_local_cache()
    if isinstance(model, str) and model.strip():
        known = _minimal_client_versions.get(model.strip())
        if known:
            return _max_semver(_CODEX_CLI_COMPAT_FLOOR, known)

    candidates: List[str] = [_CODEX_CLI_COMPAT_FLOOR]
    if _minimal_client_versions:
        candidates.extend(_minimal_client_versions.values())
    if _local_codex_cli_version:
        candidates.append(_local_codex_cli_version)
    return _max_semver(*candidates)


def reset_codex_compat_version_cache_for_tests() -> None:
    """Clear process caches (tests only)."""
    global _local_codex_cli_version, _hydrated_from_local_cache, _live_min_version_probe_done
    _minimal_client_versions.clear()
    _local_codex_cli_version = None
    _hydrated_from_local_cache = False
    _live_min_version_probe_done = False

DEFAULT_CODEX_MODELS: List[str] = [
    # GPT-5.6 series (Sol/Terra/Luna + -pro high-effort modes) — GA 2026-07-09
    # (previewed 2026-06-26).
    "gpt-5.6-sol",
    "gpt-5.6-sol-pro",
    "gpt-5.6-terra",
    "gpt-5.6-terra-pro",
    "gpt-5.6-luna",
    "gpt-5.6-luna-pro",
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    # gpt-5.3-codex-spark is in research preview and is exposed *only* via
    # the Codex CLI / OAuth backend (chatgpt.com/backend-api/codex/models)
    # for ChatGPT Pro subscribers. It is NOT available in the public OpenAI
    # API, so it intentionally stays out of the "openai" provider catalog
    # in hermes_cli/models.py — only the openai-codex (OAuth) provider
    # surfaces it. The Codex backend reports ``supported_in_api: false`` for
    # this slug; that flag describes API availability, not Codex backend
    # availability, so the fetch/cache code paths below intentionally do
    # not filter on it. PR #12994 removed this entry on the assumption it
    # was unsupported — that was wrong; restored here. Keep it in the
    # curated fallback so Pro users still see Spark in `/model` when live
    # discovery is unavailable (offline first run, transient API failure).
    "gpt-5.3-codex-spark",
    # NOTE: gpt-5.2-codex / gpt-5.1-codex-max / gpt-5.1-codex-mini were
    # previously listed here but the chatgpt.com Codex backend returns
    # HTTP 400 "The '<model>' model is not supported when using Codex with
    # a ChatGPT account." for all three on every ChatGPT Pro account we've
    # tested (verified live 2026-05-27). Keeping them in the fallback list
    # leaked dead slugs into /model when live discovery was unavailable
    # (transient API failure, first-run before refresh) and surfaced HTTP 400
    # crashes on selection. The Codex CLI public catalog still references
    # these slugs, which is why they survived previously — but those entries
    # describe the public OpenAI API, not the OAuth-backed Codex backend
    # Hermes uses. Removed here. If OpenAI re-enables them on Codex backend,
    # live discovery will pick them up automatically via _fetch_models_from_api.
]

_FORWARD_COMPAT_TEMPLATE_MODELS: List[tuple[str, tuple[str, ...]]] = [
    ("gpt-5.6-sol", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-sol-pro", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-terra", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-terra-pro", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-luna", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-luna-pro", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.5", ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex")),
    ("gpt-5.4-mini", ("gpt-5.3-codex",)),
    ("gpt-5.4", ("gpt-5.3-codex",)),
    # Surface Spark whenever any compatible Codex template is present so
    # accounts hitting the live endpoint with an older lineup still see
    # Spark in the picker. Backend gates real availability by ChatGPT Pro
    # entitlement; Hermes does not.
    ("gpt-5.3-codex-spark", ("gpt-5.3-codex",)),
]


def _add_forward_compat_models(model_ids: List[str]) -> List[str]:
    """Add Clawdbot-style synthetic forward-compat Codex models.

    If a newer Codex slug isn't returned by live discovery, surface it when an
    older compatible template model is present. This mirrors Clawdbot's
    synthetic catalog / forward-compat behavior for GPT-5 Codex variants.
    """
    ordered: List[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id not in seen:
            ordered.append(model_id)
            seen.add(model_id)

    for synthetic_model, template_models in _FORWARD_COMPAT_TEMPLATE_MODELS:
        if synthetic_model in seen:
            continue
        if any(template in seen for template in template_models):
            ordered.append(synthetic_model)
            seen.add(synthetic_model)

    return ordered


def _extract_chatgpt_account_id(access_token: str) -> Optional[str]:
    """Best-effort extraction of ``chatgpt_account_id`` from the OAuth JWT.

    The Codex backend requires the ``ChatGPT-Account-Id`` header for the
    per-account catalog. Without it, ``GET /backend-api/codex/models``
    returns ``{"models":[]}`` (HTTP 200) — which masquerades as "no
    models available" and silently degrades the picker to the curated
    fallback list. The request-side path in ``auxiliary_client.py``
    already extracts the same claim; this mirrors that logic here so the
    probe sees the same catalog the request path will actually use.

    Returns ``None`` on any parse error — the probe then degrades
    gracefully to the unauthenticated fallback list instead of crashing.
    """
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = (
            claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
            if isinstance(claims, dict)
            else None
        )
        return acct_id if isinstance(acct_id, str) and acct_id else None
    except Exception:
        return None


def _fetch_models_from_api(access_token: str) -> List[str]:
    """Fetch available models from the Codex API. Returns visible models sorted by priority."""
    try:
        import httpx
        headers = {"Authorization": f"Bearer {access_token}"}
        acct_id = _extract_chatgpt_account_id(access_token)
        if acct_id:
            headers["ChatGPT-Account-Id"] = acct_id
        resp = httpx.get(
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        entries = data.get("models", []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.debug("Failed to fetch Codex models from API: %s", exc)
        return []

    # Remember mins from the same payload used for slug discovery so inference
    # headers track OpenAI's per-model gate instead of a hardcoded CLI release.
    if isinstance(entries, list):
        remember_codex_minimal_client_versions(entries)
    if isinstance(data, dict):
        remember_local_codex_cli_version(data.get("client_version"))

    sortable = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        # Codex CLI's catalog uses ``supported_in_api`` for the public OpenAI
        # API, not for the OAuth-backed Codex backend that this provider uses.
        # Some valid Codex CLI models (for example gpt-5.3-codex-spark) are
        # marked false here but are still accepted by the Codex route.
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug))

    sortable.sort(key=lambda x: (x[0], x[1]))
    return _add_forward_compat_models([slug for _, slug in sortable])


def _read_default_model(codex_home: Path) -> Optional[str]:
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib
    except Exception:
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _read_cache_models(codex_home: Path) -> List[str]:
    cache_path = codex_home / "models_cache.json"
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if isinstance(raw, dict):
        remember_local_codex_cli_version(raw.get("client_version"))

    entries = raw.get("models") if isinstance(raw, dict) else None
    sortable = []
    if isinstance(entries, list):
        remember_codex_minimal_client_versions(entries)
        for item in entries:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            # Do not filter on ``supported_in_api`` here.  It describes the
            # public OpenAI API, while Hermes openai-codex talks to the same
            # OAuth-backed Codex backend as Codex CLI.
            visibility = item.get("visibility")
            if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
                continue
            priority = item.get("priority")
            rank = int(priority) if isinstance(priority, (int, float)) else 10_000
            sortable.append((rank, slug))

    sortable.sort(key=lambda item: (item[0], item[1]))
    deduped: List[str] = []
    for _, slug in sortable:
        if slug not in deduped:
            deduped.append(slug)
    return deduped


def get_codex_model_ids(access_token: Optional[str] = None) -> List[str]:
    """Return available Codex model IDs, trying API first, then local sources.
    
    Resolution order: API (live, if token provided) > config.toml default >
    local cache > hardcoded defaults.
    """
    codex_home_str = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    codex_home = Path(codex_home_str).expanduser()
    ordered: List[str] = []

    # Try live API if we have a token
    if access_token:
        api_models = _fetch_models_from_api(access_token)
        if api_models:
            return _add_forward_compat_models(api_models)

    # Fall back to local sources
    default_model = _read_default_model(codex_home)
    if default_model:
        ordered.append(default_model)

    for model_id in _read_cache_models(codex_home):
        if model_id not in ordered:
            ordered.append(model_id)

    for model_id in DEFAULT_CODEX_MODELS:
        if model_id not in ordered:
            ordered.append(model_id)

    return _add_forward_compat_models(ordered)

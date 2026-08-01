"""1Password Secrets Manager (onepassword-sdk) integration.

Hermes pulls API keys from 1Password at process startup so they don't
have to live in plaintext in ``~/.hermes/.env``.

Design summary
--------------

* The ``onepassword-sdk`` Python package (``import onepassword``) is used
  as the primary access method.  It authenticates via a 1Password
  Service Account token (``ops_...``).
* The service account token is read from the env var named in
  ``secrets.onepassword.service_account_token_env``
  (default: ``OP_SERVICE_ACCOUNT_TOKEN``).
* A specific item is targeted via ``secrets.onepassword.vault``
  (vault title; empty = search all accessible vaults) and
  ``secrets.onepassword.item`` (item title; required).
* Each field in the item is mapped to an env var: the field label is
  uppercased and spaces/hyphens are replaced with underscores.  An
  optional ``secrets.onepassword.field_mapping`` dict lets you override
  individual mappings (field label → env var name).
* Results are cached in-process for ``cache_ttl_seconds`` seconds so
  back-to-back ``hermes`` invocations don't hammer the API.
* Failures NEVER block Hermes startup.  Missing SDK, bad token, or item
  not found all emit a one-line warning and continue with whatever
  credentials ``.env`` already had.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# How long to wait for individual SDK calls, in seconds.
SDK_TIMEOUT_SECONDS = 30

# Strings passed to Client.authenticate() to identify this integration.
_OP_INTEGRATION_NAME = "hermes-agent"
_OP_INTEGRATION_VERSION = "1.0.0"

# In-process cache: (vault_name, item_title, token, field_mapping) → _CachedFetch
# The token and field_mapping are part of the key (not hashed — this is an
# in-memory dict key, never logged or persisted) so that a rotated service
# account token or a changed field mapping can never serve stale secrets
# fetched under a different identity/mapping for the rest of the TTL.
_CacheKey = Tuple[str, str, str, Tuple[Tuple[str, str], ...]]
_CACHE: Dict[_CacheKey, "_CachedFetch"] = {}

# Env var names that must never be auto-injected from 1Password vault fields.
# These variables can be used to hijack subprocess execution via interpreter
# hooks, dynamic linker preloads, or shell startup files.
_DANGEROUS_ENV_VARS: frozenset = frozenset({
    "BASH_ENV", "ENV",
    "GIT_SSH_COMMAND", "GIT_SSH", "GIT_EXEC_PATH", "GIT_PROXY_COMMAND", "GIT_ASKPASS",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "NODE_OPTIONS", "NODE_PATH",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERSITE",
    "RUBYOPT", "RUBYLIB",
    "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS",
    "PERL5OPT", "PERL5LIB",
    "CDPATH",
})


@dataclass
class _CachedFetch:
    secrets: Dict[str, str]
    fetched_at: float

    def is_fresh(self, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            return False
        return (time.time() - self.fetched_at) < ttl_seconds


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Outcome of a single 1Password secrets pull."""

    secrets: Dict[str, str] = field(default_factory=dict)
    applied: List[str] = field(default_factory=list)    # set into os.environ
    skipped: List[str] = field(default_factory=list)    # already set, not overridden
    warnings: List[str] = field(default_factory=list)   # non-fatal issues
    error: Optional[str] = None                         # fatal: nothing was fetched
    sdk_available: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# SDK availability
# ---------------------------------------------------------------------------


def _check_sdk_available() -> bool:
    """Return True if the ``onepassword`` Python SDK is importable."""
    try:
        import onepassword  # noqa: F401
        return True
    except ImportError:
        return False


def _ensure_sdk() -> None:
    """Raise :class:`RuntimeError` if the SDK is not available."""
    if not _check_sdk_available():
        raise RuntimeError(
            "The 'onepassword' Python SDK is not installed.  "
            "Run `hermes secrets onepassword install` or "
            "`pip install onepassword-sdk` to install it."
        )


def install_onepassword_sdk(*, force: bool = False, skip_gate: bool = False) -> str:
    """Install the ``onepassword-sdk`` package via pip.

    Returns a short version string on success.  Raises :class:`ImportError`
    if the lazy install gate is disabled, or :class:`RuntimeError` on pip
    failure — callers in the auto-install path catch these.

    Args:
        force: Re-install even if the SDK is already present.
        skip_gate: When True, bypass the HERMES_DISABLE_LAZY_INSTALLS gate.
            Pass ``True`` only from explicit user commands (e.g.
            ``hermes secrets onepassword install``) so that users can install
            the SDK even in environments where auto-installs are disabled.
            The auto-install path inside ``apply_onepassword_secrets()`` should
            leave this at the default ``False`` so the gate is still respected.
    """
    import subprocess  # noqa: PLC0415 — lazy import

    if _check_sdk_available() and not force:
        import onepassword  # noqa: F401
        return _sdk_version()

    # Honor the same lazy install gate used by the rest of the codebase
    # (tools.lazy_deps._allow_lazy_installs / HERMES_DISABLE_LAZY_INSTALLS),
    # unless the caller explicitly opted out of the gate (skip_gate=True).
    if not skip_gate:
        try:
            from tools.lazy_deps import _allow_lazy_installs  # noqa: PLC0415
            _lazy_ok = _allow_lazy_installs()
        except ImportError:
            # Fallback: read the env var directly if the module isn't importable.
            _dis = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS", "").lower()
            _lazy_ok = _dis not in ("1", "true", "yes")

        if not _lazy_ok:
            raise ImportError(
                "1Password SDK auto-install is disabled (HERMES_DISABLE_LAZY_INSTALLS). "
                "Install manually: pip install 'onepassword-sdk>=0.1.0,<0.2.0'"
            )

    pkg = "onepassword-sdk>=0.1.0,<0.2.0"
    pip_args = ["--quiet"]
    if force:
        pip_args.append("--force-reinstall")
    pip_args.append(pkg)

    try:
        from hermes_cli.tools_config import _pip_install  # noqa: PLC0415
        result = _pip_install(pip_args, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"pip install failed: {type(exc).__name__}") from None

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"pip install onepassword-sdk failed: {err}")

    return _sdk_version()


def _sdk_version() -> str:
    """Return the installed onepassword-sdk version string, or 'unknown'."""
    try:
        import importlib.metadata
        return importlib.metadata.version("onepassword-sdk")
    except Exception:  # noqa: BLE001
        try:
            import onepassword  # noqa: F401  # type: ignore[import-not-found]
            return "installed"
        except ImportError:
            return "not installed"


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------


_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _field_label_to_env_name(label: str) -> str:
    """Convert a 1Password field label to a valid env var name.

    Rules applied in order:
      1. Uppercase the whole string (ASCII case-folding only).
      2. Replace spaces and hyphens with underscores.
      3. Strip any character outside ``[A-Z0-9_]`` — including non-ASCII
         letters (e.g. ``clé`` → ``CL``, not a Unicode-derived name), since
         POSIX shell variable names and HTTP header interpolation both
         require pure ASCII.
    """
    name = label.encode("ascii", errors="ignore").decode("ascii").upper()
    name = re.sub(r"[ \-]", "_", name)
    name = re.sub(r"[^A-Z0-9_]", "", name)
    return name


def _is_valid_env_name(name: str) -> bool:
    """Return True if ``name`` is a valid ASCII POSIX env-var name."""
    return bool(_ENV_NAME_RE.match(name))


# ---------------------------------------------------------------------------
# Async fetch core
# ---------------------------------------------------------------------------


async def _fetch_secrets_async(
    *,
    token: str,
    vault_name: str,
    item_title: str,
    field_mapping: Dict[str, str],
) -> Tuple[Dict[str, str], List[str]]:
    """Authenticate to 1Password, find the target item, return field values.

    Returns ``(secrets_dict, warnings_list)``.  Raises :class:`RuntimeError`
    for fatal conditions (vault not found, item not found, auth failure).
    """
    from onepassword.client import Client  # noqa: PLC0415  # type: ignore[import-not-found]

    client = await asyncio.wait_for(
        Client.authenticate(
            auth=token,
            integration_name=_OP_INTEGRATION_NAME,
            integration_version=_OP_INTEGRATION_VERSION,
        ),
        timeout=SDK_TIMEOUT_SECONDS,
    )

    # ------------------------------------------------------------------ vaults
    all_vaults = await asyncio.wait_for(client.vaults.list_all(), timeout=SDK_TIMEOUT_SECONDS)
    if vault_name:
        matching_vaults = [v for v in all_vaults if v.title == vault_name]
        if not matching_vaults:
            raise RuntimeError(
                f"Vault {vault_name!r} not found.  "
                f"Accessible vaults: {[v.title for v in all_vaults]}"
            )
        if len(matching_vaults) > 1:
            # 1Password permits duplicate vault titles across accounts.
            # Silently picking the first one risks pulling credentials from
            # the wrong vault — fail loud instead.
            raise RuntimeError(
                f"Vault name {vault_name!r} is ambiguous: "
                f"{len(matching_vaults)} vaults share this title "
                f"(ids: {[v.id for v in matching_vaults]}).  "
                "Rename one of the vaults, or contact 1Password admin to "
                "resolve the duplicate."
            )
        vault_ids = [matching_vaults[0].id]
    else:
        vault_ids = [v.id for v in all_vaults]

    if not vault_ids:
        raise RuntimeError(
            "No vaults are accessible to this service account.  "
            "Check vault permissions in the 1Password admin console."
        )

    # ------------------------------------------------------------------ item
    # Collect every overview matching item_title across all candidate vaults
    # first — 1Password permits duplicate item titles within a vault, so
    # picking the first match found would silently risk injecting
    # credentials from the wrong item.
    matching_overviews: List[Tuple[str, object]] = []  # (vault_id, overview)
    for vault_id in vault_ids:
        item_overviews = await asyncio.wait_for(client.items.list_all(vault_id=vault_id), timeout=SDK_TIMEOUT_SECONDS)
        for overview in item_overviews:
            if not item_title or overview.title == item_title:
                matching_overviews.append((vault_id, overview))

    if not matching_overviews:
        if item_title:
            raise RuntimeError(
                f"Item {item_title!r} not found in 1Password "
                f"(searched {len(vault_ids)} vault(s))."
            )
        return {}, ["No items found in the accessible 1Password vault(s)."]

    if item_title and len(matching_overviews) > 1:
        raise RuntimeError(
            f"Item title {item_title!r} is ambiguous: "
            f"{len(matching_overviews)} items share this title across the "
            f"searched vault(s) (ids: {[ov.id for _, ov in matching_overviews]}).  "
            "Rename one of the items so the title is unique, or narrow "
            "secrets.onepassword.vault to a single vault."
        )

    target_vault_id, target_overview = matching_overviews[0]
    target_item = await asyncio.wait_for(
        client.items.get(vault_id=target_vault_id, item_id=target_overview.id),
        timeout=SDK_TIMEOUT_SECONDS,
    )

    # ------------------------------------------------------------------ fields
    # First pass: build label → env_name mapping and label → value for all
    # non-empty fields with valid env var names.
    field_to_env: Dict[str, str] = {}   # field label → env var name
    field_values: Dict[str, str] = {}   # field label → secret value
    warnings: List[str] = []

    for fld in target_item.fields:
        label = fld.title
        value = fld.value
        if not isinstance(value, str) or not value:
            continue

        # Apply explicit override first; fall back to derived name.
        if field_mapping and label in field_mapping:
            env_name = field_mapping[label]
        else:
            env_name = _field_label_to_env_name(label)

        if not _is_valid_env_name(env_name):
            warnings.append(
                f"Skipping field {label!r}: derived env var name "
                f"{env_name!r} is not a valid identifier"
            )
            continue

        if env_name in _DANGEROUS_ENV_VARS:
            logger.warning(
                "1Password: skipping field — env var %s is in the "
                "process-control blocklist and will not be auto-injected",
                env_name,
            )
            continue

        field_to_env[label] = env_name
        field_values[label] = value

    # Detect collisions: two fields that normalize to the same env var name.
    env_name_sources: Dict[str, str] = {}  # env_name → first field label
    collisions: set = set()
    for field_name, env_name in field_to_env.items():
        if env_name in env_name_sources:
            logger.warning(
                "1Password field mapping collision: '%s' and '%s' both map to '%s'; skipping both",
                env_name_sources[env_name], field_name, env_name,
            )
            collisions.add(env_name)
        else:
            env_name_sources[env_name] = field_name

    # Build final secrets dict, excluding colliding env names.
    secrets: Dict[str, str] = {
        field_to_env[label]: value
        for label, value in field_values.items()
        if field_to_env[label] not in collisions
    }

    return secrets, warnings


# ---------------------------------------------------------------------------
# Synchronous public fetch
# ---------------------------------------------------------------------------


def fetch_onepassword_secrets(
    *,
    token: str,
    vault_name: str = "",
    item_title: str = "",
    field_mapping: Optional[Dict[str, str]] = None,
    cache_ttl_seconds: float = 300,
    use_cache: bool = True,
) -> Tuple[Dict[str, str], List[str]]:
    """Pull secrets from 1Password.

    Returns ``(secrets_dict, warnings_list)``.

    Raises :class:`RuntimeError` for fatal conditions (SDK not installed,
    auth failure, item not found).  Callers in the env_loader path catch
    this and emit a single warning; callers in the CLI setup wizard let it
    propagate so the user sees a clear error.
    """
    if not token:
        raise RuntimeError("1Password service account token is empty")

    fm_for_key = tuple(sorted((field_mapping or {}).items()))
    cache_key: _CacheKey = (vault_name, item_title, token, fm_for_key)
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached and cached.is_fresh(cache_ttl_seconds):
            return cached.secrets, []

    _ensure_sdk()

    fm = field_mapping or {}

    try:
        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if has_running_loop:
            # Deliberately not a `with` block: ThreadPoolExecutor.__exit__
            # calls shutdown(wait=True), which would block this call — and
            # therefore the caller's executor thread — until the worker
            # actually finishes, defeating the timeout below entirely if
            # the SDK call hangs past it. shutdown(wait=False) lets the
            # worker finish (or leak, on true hang) in the background while
            # this call returns control immediately on timeout.
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(
                asyncio.run,
                _fetch_secrets_async(
                    token=token,
                    vault_name=vault_name,
                    item_title=item_title,
                    field_mapping=fm,
                ),
            )
            try:
                secrets, warnings = future.result(timeout=SDK_TIMEOUT_SECONDS + 5)
            except concurrent.futures.TimeoutError:
                pool.shutdown(wait=False)
                raise RuntimeError("1Password fetch timed out") from None
            else:
                pool.shutdown(wait=False)
        else:
            secrets, warnings = asyncio.run(
                _fetch_secrets_async(
                    token=token,
                    vault_name=vault_name,
                    item_title=item_title,
                    field_mapping=fm,
                )
            )
    except RuntimeError:
        # Re-raise RuntimeError as-is (our own error messages).
        raise
    except Exception as exc:  # noqa: BLE001
        # Use only the exception type name — not str(exc) — to avoid
        # propagating token data that may appear in the SDK's error message
        # (CodeQL py/clear-text-logging-sensitive-data taint path).
        raise RuntimeError(f"1Password SDK error: {type(exc).__name__}") from None

    _CACHE[cache_key] = _CachedFetch(secrets=secrets, fetched_at=time.time())
    return secrets, warnings


# ---------------------------------------------------------------------------
# Public entry point — called from hermes_cli.env_loader
# ---------------------------------------------------------------------------


def apply_onepassword_secrets(
    config: dict,
    home_path: Path,
    previously_managed: Optional[set] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Pull secrets from 1Password and inject them into ``os.environ``.

    Called by ``_apply_external_secret_sources()`` in env_loader after
    the dotenv files have loaded.  Parameters come from the
    ``secrets.onepassword.*`` section of ``config.yaml``.

    ``previously_managed`` should contain only env var names that are
    *currently still* set to the value 1Password last injected for them
    (the caller is responsible for excluding names a user has since
    overridden locally, e.g. by editing ``.env``) — this function trusts
    the set at face value both for refresh-without-override_existing and
    for removal-on-disappearance below.

    Returns ``(applied, removed)``: ``applied`` maps every env var actually
    set to the masked value ``"***"`` (real values are never logged).
    ``removed`` lists env vars that were unset because their 1Password
    field disappeared from the item (only vars in ``previously_managed``
    are eligible for removal — untouched vars from other sources are
    never removed). Returns ``({}, [])`` on any failure.

    This function never raises — failures emit a ``logger.warning`` and
    return ``({}, [])``.
    """
    token_env = config.get("service_account_token_env", "OP_SERVICE_ACCOUNT_TOKEN")
    vault_name = config.get("vault", "")
    item_title = config.get("item", "")
    field_mapping: Dict[str, str] = config.get("field_mapping") or {}
    override_existing = bool(config.get("override_existing", False))
    cache_ttl = float(config.get("cache_ttl_seconds", 300))
    auto_install = bool(config.get("auto_install", True))

    token = os.environ.get(token_env, "").strip()
    if not token:
        logger.warning(
            "secrets.onepassword.enabled is true but the service account "
            "token env var is not set.  Run `hermes secrets onepassword setup`."
        )
        return {}, []

    if not item_title:
        logger.warning(
            "secrets.onepassword.item is not configured.  "
            "Run `hermes secrets onepassword setup`."
        )
        return {}, []

    # Auto-install the SDK if requested and not present.
    if auto_install and not _check_sdk_available():
        # Check the lazy install gate before attempting pip — respect
        # HERMES_DISABLE_LAZY_INSTALLS / security.allow_lazy_installs config.
        try:
            from tools.lazy_deps import _allow_lazy_installs  # noqa: PLC0415
            _lazy_ok = _allow_lazy_installs()
        except ImportError:
            _dis = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS", "").lower()
            _lazy_ok = _dis not in ("1", "true", "yes")

        if not _lazy_ok:
            logger.warning(
                "1Password SDK is not installed and auto-install is disabled "
                "(HERMES_DISABLE_LAZY_INSTALLS). "
                "Install manually: pip install 'onepassword-sdk>=0.1.0,<0.2.0'"
            )
            return {}, []

        try:
            install_onepassword_sdk()
        except Exception as exc:  # noqa: BLE001
            # exc is from pip install — safe to log in full (no token data).
            logger.warning("1Password SDK auto-install failed: %s", exc)
            return {}, []

    try:
        secrets, warnings = fetch_onepassword_secrets(
            token=token,
            vault_name=vault_name,
            item_title=item_title,
            field_mapping=field_mapping,
            cache_ttl_seconds=cache_ttl,
        )
    except RuntimeError as exc:
        # Log only the exception type, not the message, to avoid CodeQL
        # clear-text-logging alert (the message may contain token data that
        # flowed through the SDK call).
        logger.warning(
            "1Password secrets fetch failed (%s) — run "
            "`hermes secrets onepassword status` for details",
            type(exc).__name__,
        )
        return {}, []

    if warnings:
        logger.warning(
            "1Password: %d field(s) skipped during secrets fetch "
            "(run `hermes secrets onepassword status` for details)",
            len(warnings),
        )

    applied: Dict[str, str] = {}
    for key, value in secrets.items():
        if key == token_env:
            # Never let 1Password override the token used to authenticate.
            continue
        if not override_existing and os.environ.get(key):
            # Always refresh keys that 1Password previously injected so that
            # credential rotation takes effect without requiring override_existing.
            if previously_managed is None or key not in previously_managed:
                continue
        os.environ[key] = value
        applied[key] = "***"

    # A field that vanished from the item (deleted or renamed in 1Password)
    # is absent from `secrets`.  Remove the stale env var rather than
    # leaving the old credential active until process restart — but only
    # for names we know we previously injected ourselves (never touch a
    # var some other source now owns).
    removed: List[str] = []
    if previously_managed:
        for key in sorted(previously_managed - set(secrets.keys())):
            if key == token_env:
                continue
            if key in os.environ:
                del os.environ[key]
                removed.append(key)

    if applied:
        logger.debug("1Password: applied %d secret(s)", len(applied))
    if removed:
        logger.debug("1Password: removed %d stale secret(s)", len(removed))

    return applied, removed


# ---------------------------------------------------------------------------
# Status helper — used by the CLI status command
# ---------------------------------------------------------------------------


def get_onepassword_status(config: dict, home_path: Path, *, check_connection: bool = True) -> dict:
    """Return a dict describing current configuration, SDK, and connection health.

    Used by ``hermes secrets onepassword status`` to populate the table.

    When ``check_connection`` is True (the default) and the SDK is available
    and a token/item are configured, this performs a real (uncached) fetch
    to surface the actual failure category — revoked token, vault/item not
    found, ambiguous match, network error, etc. — rather than only reporting
    static config presence.  Pass ``check_connection=False`` for callers that
    just want the cheap static snapshot (e.g. tests, or code paths that
    already fetch separately).
    """
    token_env = config.get("service_account_token_env", "OP_SERVICE_ACCOUNT_TOKEN")
    vault = config.get("vault", "")
    item = config.get("item", "")
    token = os.environ.get(token_env, "").strip()
    sdk_available = _check_sdk_available()

    connection_ok: Optional[bool] = None
    connection_error: Optional[str] = None
    if check_connection and sdk_available and token and item:
        try:
            fetch_onepassword_secrets(
                token=token,
                vault_name=vault,
                item_title=item,
                field_mapping=config.get("field_mapping") or {},
                use_cache=False,
            )
            connection_ok = True
        except RuntimeError as exc:
            connection_ok = False
            # str(exc) here is one of our own RuntimeError messages (vault/item
            # not found, ambiguous match, timeout, or a redacted SDK error
            # type name) — never raw SDK exception text — so it's safe to
            # surface directly, unlike the taint path in apply_*().
            connection_error = str(exc)

    return {
        "enabled": bool(config.get("enabled")),
        "sdk_available": sdk_available,
        "sdk_version": _sdk_version(),
        "token_env": token_env,
        "token_set": bool(token),
        "vault": vault or "(search all vaults)",
        "item": item or "(unset)",
        "override_existing": bool(config.get("override_existing", False)),
        "cache_ttl_seconds": config.get("cache_ttl_seconds", 300),
        "auto_install": bool(config.get("auto_install", True)),
        "field_mapping": config.get("field_mapping") or {},
        "connection_ok": connection_ok,
        "connection_error": connection_error,
    }


# ---------------------------------------------------------------------------
# Test hook — flush the cache between test cases
# ---------------------------------------------------------------------------


def _reset_cache_for_tests() -> None:
    _CACHE.clear()

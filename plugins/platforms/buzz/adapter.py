"""
Buzz Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to a Buzz community relay
(Block's open-source human+agent collaboration platform, built on the
Nostr protocol) and relays messages to/from the Hermes agent.

The adapter shells out to the ``buzz`` CLI for request/response operations
and uses an authenticated Nostr WebSocket subscription for inbound delivery.
``transport=auto`` falls back to CLI polling when streaming is unavailable.

Configuration in config.yaml::

    gateway:
      platforms:
        buzz:
          enabled: true
          extra:
            relay_url: https://mycommunity.communities.buzz.xyz
            channels:                  # channel UUIDs to watch (empty = all joined)
              - ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd
            home_channel: ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd
            transport: auto            # WebSocket first; polling fallback
            poll_interval: 4           # seconds between fallback poll sweeps
            cli_path: ""               # path to the buzz binary (default: PATH, then ~/bin/buzz)
            credentials_file: ""       # JSON file holding the nsec (fallback for BUZZ_PRIVATE_KEY)
            allowed_users: []          # empty = allow all; entries are hex pubkeys or npubs
            mention_required_users: [] # senders that must carry a real mention
            mention_aliases: {}        # outbound @alias -> pubkey mapping
            max_agent_hops: 0          # consecutive agent reply limit; 0 = unlimited

Or via environment variables (overrides config.yaml):
    BUZZ_RELAY_URL, BUZZ_CHANNELS, BUZZ_HOME_CHANNEL, BUZZ_POLL_INTERVAL,
    BUZZ_CLI_PATH, BUZZ_CREDENTIALS_FILE, BUZZ_ALLOWED_USERS,
    BUZZ_ALLOW_ALL_USERS, BUZZ_MENTION_REQUIRED_USERS, BUZZ_MENTION_ALIASES,
    BUZZ_MAX_AGENT_HOPS

The only secret is BUZZ_PRIVATE_KEY (nsec or hex) — it belongs in
``~/.hermes/.env``.  It is passed to the CLI via the subprocess
environment and is never logged.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from markdown_it import MarkdownIt

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)

_INVOCATION_MARKDOWN = MarkdownIt("commonmark", {"html": True})
_INVOCATION_URL_RE = re.compile(
    r"(?i)(?<![@A-Za-z0-9+.-])(?:[A-Za-z][A-Za-z0-9+.-]*:(?://)?|www\.)[^\s<>]+"
)
_INVOCATION_HIDDEN_BOUNDARY = "\u200d"
_INVOCATION_CHARACTER_REFERENCE_RE = re.compile(
    r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);"
)


class _RawHtmlInvocationTextParser(HTMLParser):
    """Exclude text enclosed by any raw HTML element from invocations."""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: List[str] = []
        self._hidden_depth = 0
        self._parts: List[str] = []

    def add_text(self, text: str) -> None:
        if not self._hidden_depth:
            self._parts.append(text)

    def add_hidden_boundary(self) -> None:
        if not self._hidden_depth:
            self._parts.append(_INVOCATION_HIDDEN_BOUNDARY)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        normalized = tag.lower()
        if normalized in self._VOID_TAGS:
            if not self._hidden_depth:
                self._parts.append(_INVOCATION_HIDDEN_BOUNDARY)
            return
        if not self._hidden_depth:
            self._parts.append(_INVOCATION_HIDDEN_BOUNDARY)
        self._stack.append(normalized)
        self._hidden_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        normalized = tag.lower()
        if normalized in self._VOID_TAGS:
            self.handle_starttag(normalized, attrs)
            return
        # HTML ignores ``/>`` on non-void elements. Keep the context open so
        # malformed ``<tag/>@Alias`` fails closed.
        self.handle_starttag(normalized, attrs)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if not self._stack or self._stack[-1] != normalized:
            if not self._hidden_depth:
                self._parts.append(_INVOCATION_HIDDEN_BOUNDARY)
            return
        self._stack.pop()
        self._hidden_depth -= 1
        if not self._hidden_depth:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.add_text(data)

    def handle_comment(self, data: str) -> None:
        self.add_hidden_boundary()

    def handle_decl(self, decl: str) -> None:
        self.add_hidden_boundary()

    def unknown_decl(self, data: str) -> None:
        # ``HTMLParser`` routes CDATA and other marked sections here. Treat
        # every omitted declaration as a source boundary so it cannot splice
        # fragments into a configured alias.
        self.add_hidden_boundary()

    def handle_pi(self, data: str) -> None:
        self.add_hidden_boundary()

    def handle_entityref(self, name: str) -> None:
        self.add_hidden_boundary()

    def handle_charref(self, name: str) -> None:
        self.add_hidden_boundary()

    @property
    def text(self) -> str:
        return "".join(self._parts)

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform


# Buzz chat messages are Nostr kind 9 events.  ``buzz messages get`` also
# returns housekeeping kinds (joins, canvas updates, …) — only kind 9 is
# dispatched to the agent.
_CHAT_KIND = 9
# How many events to request per poll / seed call.
_FETCH_LIMIT = 50
# Bound on the per-channel de-dupe set (events, not bytes).
_SEEN_CAP = 500
# Re-run DM discovery (``dms list`` plus the channels-list fallback) every
# N poll sweeps to pick up conversations opened mid-run.
_DM_DISCOVERY_EVERY = 5

_DEFAULT_POLL_INTERVAL = 4.0
_MIN_POLL_INTERVAL = 1.0
_CLI_TIMEOUT = 30.0
_MAX_AGENT_HOPS = 1000

# WebSocket transport (NIP-42 authenticated Nostr subscription).
# kind 44100 is Buzz's channel-membership event — used for live DM discovery.
_WS_AUTH_TIMEOUT = 20.0
_WS_MAX_MESSAGE_BYTES = 2_000_000
_WS_MEMBERSHIP_KIND = 44100
_WS_MEMBERSHIP_SUB_ID = "hermes-buzz-membership"

# Where to look for a credentials JSON (keys: nsec / private_key_hex) when
# BUZZ_PRIVATE_KEY is not set.  Module-level so tests can point it at a tmpdir.
_DEFAULT_CREDENTIALS_DIR = Path("~/.config/buzz").expanduser()


def _load_nostr_auth():
    """Import the sibling nostr_auth module in a loader-agnostic way.

    The adapter is imported both as a package module
    (``plugins.platforms.buzz.adapter``) and as a bare single-file module by
    the test plugin loader, where relative imports have no parent package.
    """
    try:
        from . import nostr_auth  # type: ignore[no-redef]

        return nostr_auth
    except ImportError:
        import importlib.util

        path = Path(__file__).with_name("nostr_auth.py")
        spec = importlib.util.spec_from_file_location("plugin_adapter_buzz_nostr_auth", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


# ---------------------------------------------------------------------------
# bech32 (BIP-173) helpers — used to convert between npub and hex pubkeys so
# mention detection and allow-lists accept either form.  Pure stdlib.
# ---------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: List[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits: int, tobits: int, pad: bool = True) -> Optional[List[int]]:
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def hex_to_npub(pubkey_hex: str) -> Optional[str]:
    """Encode a 64-char hex pubkey as an ``npub1…`` bech32 string."""
    try:
        raw = bytes.fromhex(pubkey_hex)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    data = _convertbits(raw, 8, 5)
    if data is None:
        return None
    values = _bech32_hrp_expand("npub") + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return "npub1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


def npub_to_hex(npub: str) -> Optional[str]:
    """Decode an ``npub1…`` bech32 string to a 64-char hex pubkey."""
    npub = npub.strip().lower()
    if not npub.startswith("npub1"):
        return None
    data_part = npub[len("npub1"):]
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError:
        return None
    if _bech32_polymod(_bech32_hrp_expand("npub") + data) != 1:
        return None
    decoded = _convertbits(data[:-6], 5, 8, pad=False)
    if decoded is None or len(decoded) != 32:
        return None
    return bytes(decoded).hex()


def _normalize_user_ref(ref: str) -> Optional[str]:
    """Normalize a user reference (hex pubkey or npub) to lowercase hex."""
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    if ref.startswith("npub1"):
        return npub_to_hex(ref)
    if re.fullmatch(r"[0-9a-f]{64}", ref):
        return ref
    return None


# ---------------------------------------------------------------------------
# buzz-cli invocation helpers
# ---------------------------------------------------------------------------

def _resolve_cli_path(configured: str = "") -> str:
    """Resolve the buzz CLI binary path portably.

    Order: explicit config value → ``buzz`` on PATH → ``~/bin/buzz``.
    Returns "" when nothing is found so callers can raise a config error.
    """
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.is_file() else ""
    found = shutil.which("buzz")
    if found:
        return found
    fallback = Path.home() / "bin" / "buzz"
    return str(fallback) if fallback.is_file() else ""


def _resolve_private_key(extra: Optional[dict] = None) -> str:
    """Resolve the Nostr private key: env first, then a credentials JSON.

    NEVER log the return value.
    """
    key = _get_scoped_secret("BUZZ_PRIVATE_KEY", "").strip()
    if key:
        return key
    configured = os.getenv("BUZZ_CREDENTIALS_FILE", "").strip() or (extra or {}).get("credentials_file", "")
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        try:
            candidates = sorted(_DEFAULT_CREDENTIALS_DIR.glob("*credentials*.json"))
        except OSError:
            candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for field in ("nsec", "private_key_hex", "private_key"):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


async def _exec_buzz(
    cli_path: str,
    args: List[str],
    *,
    relay_url: str,
    private_key: str,
    input_text: Optional[str] = None,
    timeout: float = _CLI_TIMEOUT,
) -> Tuple[int, str, str]:
    """Run the buzz CLI with an argument list (never a shell) and return
    ``(returncode, stdout, stderr)``.

    The private key travels via the subprocess environment only — it never
    appears in argv, so process listings and error logs stay clean.
    """
    env = os.environ.copy()
    env["BUZZ_RELAY_URL"] = relay_url
    env["BUZZ_PRIVATE_KEY"] = private_key
    proc = await asyncio.create_subprocess_exec(
        cli_path,
        *args,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_text.encode("utf-8") if input_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", json.dumps({"error": "timeout", "message": f"buzz {args[0] if args else ''} timed out after {timeout}s"})
    return (
        proc.returncode if proc.returncode is not None else 4,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _cli_error_message(stderr: str, returncode: int) -> str:
    """Extract the human-readable message from the CLI's JSON error contract.

    stderr is ``{"error": "<category>", "message": "<detail>"}`` on failure;
    fall back to the raw (stripped) stderr when it isn't JSON.
    """
    text = (stderr or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("message"):
            return f"{data.get('error', 'error')}: {data['message']} (exit {returncode})"
    except ValueError:
        pass
    return text or f"buzz CLI failed with exit code {returncode}"


def _parse_json_list(stdout: str) -> List[dict]:
    """Parse CLI stdout expected to be a JSON array of objects."""
    try:
        data = json.loads(stdout or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Buzz Adapter
# ---------------------------------------------------------------------------

class BuzzAdapter(BasePlatformAdapter):
    """WebSocket-first Buzz adapter with a polling fallback.

    Instantiated by the adapter_factory passed to register_platform().
    """

    def __init__(self, config, **kwargs):
        platform = Platform("buzz")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}
        self._extra = extra
        self._config_errors: List[str] = []

        # Connection settings (env vars override config.yaml)
        self.relay_url = (os.getenv("BUZZ_RELAY_URL") or extra.get("relay_url", "")).strip()
        self.cli_path = _resolve_cli_path(
            os.getenv("BUZZ_CLI_PATH", "").strip() or str(extra.get("cli_path", "") or "")
        )

        # Channels to watch: env csv > extra list/csv; empty = all joined channels
        raw_channels = os.getenv("BUZZ_CHANNELS") or extra.get("channels", [])
        if isinstance(raw_channels, str):
            raw_channels = raw_channels.split(",")
        self.channels: List[str] = [c.strip() for c in raw_channels if isinstance(c, str) and c.strip()]

        self.home_channel = (os.getenv("BUZZ_HOME_CHANNEL") or str(extra.get("home_channel", "") or "")).strip()

        try:
            interval = float(os.getenv("BUZZ_POLL_INTERVAL") or extra.get("poll_interval", _DEFAULT_POLL_INTERVAL))
        except (TypeError, ValueError):
            interval = _DEFAULT_POLL_INTERVAL
        self.poll_interval = max(_MIN_POLL_INTERVAL, interval)

        # Whether channel messages must @mention the agent to get a response.
        # Defaults to True (respond only when addressed). Set False to make the
        # agent respond to every message in a watched channel. DMs always
        # dispatch regardless. Env (BUZZ_REQUIRE_MENTION) overrides config.yaml.
        _rm_raw = os.getenv("BUZZ_REQUIRE_MENTION")
        if _rm_raw is None:
            _rm_cfg = extra.get("require_mention", True)
        else:
            _rm_cfg = _rm_raw
        self.require_mention = str(_rm_cfg).strip().lower() not in ("false", "0", "no", "off")

        # Inbound transport: "auto" (WebSocket with poll fallback, default),
        # "websocket" (require WS; fail connect when it can't authenticate),
        # or "poll" (CLI polling only). Env (BUZZ_TRANSPORT) overrides
        # config.yaml.
        _transport = (
            os.getenv("BUZZ_TRANSPORT") or str(extra.get("transport", "auto") or "auto")
        ).strip().lower()
        self.transport = _transport if _transport in ("auto", "websocket", "poll") else "auto"

        # Auth: entries may be hex pubkeys or npubs; normalized to hex
        raw_allowed = os.getenv("BUZZ_ALLOWED_USERS") or extra.get("allowed_users", [])
        if isinstance(raw_allowed, str):
            raw_allowed = raw_allowed.split(",")
        self._allowed_pubkeys: set = {
            normalized
            for entry in raw_allowed
            if isinstance(entry, str) and (normalized := _normalize_user_ref(entry))
        }

        # Selected senders (normally other agent identities) must address this
        # profile explicitly even when ambient human messages are allowed with
        # require_mention=false. Entries may be hex pubkeys or npubs.
        mention_required_env = os.getenv("BUZZ_MENTION_REQUIRED_USERS")
        raw_mention_required = (
            mention_required_env
            if mention_required_env is not None
            else extra.get("mention_required_users", [])
        )
        if isinstance(raw_mention_required, str):
            raw_mention_required = raw_mention_required.split(",")
        if not isinstance(raw_mention_required, (list, tuple, set)):
            self._config_errors.append("mention_required_users must be a list or CSV")
            raw_mention_required = []
        self._mention_required_pubkeys: set = set()
        for entry in raw_mention_required:
            normalized = _normalize_user_ref(entry) if isinstance(entry, str) else None
            if not normalized:
                self._config_errors.append("mention_required_users contains an invalid identity")
                continue
            self._mention_required_pubkeys.add(normalized)

        # Outbound aliases ensure model-authored @names become real Nostr
        # p-tags. Config accepts a mapping or CSV entries like Alias=pubkey.
        aliases_env = os.getenv("BUZZ_MENTION_ALIASES")
        aliases_explicit = aliases_env is not None or "mention_aliases" in extra
        raw_aliases = aliases_env if aliases_env is not None else extra.get("mention_aliases", {})
        if aliases_explicit and (
            raw_aliases is None
            or not isinstance(raw_aliases, (dict, str))
            or (isinstance(raw_aliases, str) and not raw_aliases.strip())
        ):
            self._config_errors.append("mention_aliases must be a non-empty mapping or CSV")
        if isinstance(raw_aliases, dict):
            alias_entries = list(raw_aliases.items())
        else:
            alias_entries = []
            saw_alias_csv_item = False
            for item in str(raw_aliases or "").split(","):
                if not item.strip():
                    continue
                saw_alias_csv_item = True
                alias, separator, user_ref = item.partition("=")
                if not separator:
                    self._config_errors.append("mention_aliases contains an invalid entry")
                    continue
                alias_entries.append((alias, user_ref))
            if isinstance(raw_aliases, str) and not saw_alias_csv_item:
                self._config_errors.append("mention_aliases contains no alias entries")
        self._mention_aliases: Dict[str, str] = {}
        for alias, user_ref in alias_entries:
            raw_alias = alias.strip() if isinstance(alias, str) else ""
            if raw_alias.startswith("@"):
                raw_alias = raw_alias[1:]
            normalized_alias = raw_alias.lower()
            normalized_user = _normalize_user_ref(user_ref) if isinstance(user_ref, str) else None
            if (
                not raw_alias
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", raw_alias, re.ASCII)
                or not normalized_user
            ):
                self._config_errors.append("mention_aliases contains an invalid alias or identity")
                continue
            if normalized_alias in self._mention_aliases:
                self._config_errors.append("mention_aliases contains duplicate case-insensitive aliases")
                continue
            self._mention_aliases[normalized_alias] = normalized_user

        # Bound consecutive agent-authored reply hops. Zero keeps the legacy
        # unlimited behavior; configured mutual-agent deployments should use a
        # small positive value.
        max_hops_env = os.getenv("BUZZ_MAX_AGENT_HOPS")
        max_hops_explicit = max_hops_env is not None or "max_agent_hops" in extra
        max_hops_raw = max_hops_env if max_hops_env is not None else extra.get("max_agent_hops", 0)
        parsed_max_hops = 0
        max_hops_valid = False
        if isinstance(max_hops_raw, int) and not isinstance(max_hops_raw, bool):
            parsed_max_hops = max_hops_raw
            max_hops_valid = 0 <= parsed_max_hops <= _MAX_AGENT_HOPS
        elif isinstance(max_hops_raw, str) and re.fullmatch(r"[0-9]+", max_hops_raw.strip()):
            try:
                parsed_max_hops = int(max_hops_raw.strip())
                max_hops_valid = parsed_max_hops <= _MAX_AGENT_HOPS
            except ValueError:
                max_hops_valid = False
        if not max_hops_valid and max_hops_explicit:
            self._config_errors.append(
                f"max_agent_hops must be an integer between 0 and {_MAX_AGENT_HOPS}"
            )
            parsed_max_hops = 0
        self.max_agent_hops = parsed_max_hops
        if self._mention_required_pubkeys and self.max_agent_hops == 0:
            self._config_errors.append(
                "max_agent_hops must be positive when mention_required_users is configured"
            )

        # Secret — resolved lazily (never at import/registration time and
        # never logged).  connect() re-resolves it to fail fast with a clear
        # error when it is missing.
        self._private_key: str = ""

        # Identity — filled in by connect() from ``buzz users get``
        self._self_pubkey: str = ""
        self._self_npub: str = ""
        self._display_name: str = ""

        # Runtime state
        self._poll_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_ready: Optional[asyncio.Event] = None
        self._ws_active = False  # True while the WS loop owns inbound delivery
        self._membership_since = 0
        self._lock_key: Optional[str] = None
        # channel_id -> {"chat_type", "last_ts", "seen": OrderedDict[event_id, None]}
        self._channel_state: Dict[str, dict] = {}
        self._channel_names: Dict[str, str] = {}
        # channel_id -> raw ``channels list`` entry; drives DM-vs-channel
        # classification (see _may_reclassify_as_dm).
        self._channel_meta: Dict[str, dict] = {}
        self._user_names: Dict[str, str] = {}
        # event id -> consecutive agent reply hops; bounded like seen ids.
        self._agent_hops: OrderedDict[str, int] = OrderedDict()
        # channel id -> (latest accepted agent event id, hop count). This
        # supplies a conservative reply parent when the gateway loses reply
        # metadata while queuing concurrent inbound messages.
        self._active_agent_events: Dict[str, Tuple[str, int]] = {}
        self._poll_count = 0

    @property
    def name(self) -> str:
        return "Buzz"

    # ── buzz-cli plumbing ─────────────────────────────────────────────────

    async def _run_cli(self, args: List[str], *, input_text: Optional[str] = None) -> Tuple[int, str, str]:
        if not self._private_key:
            self._private_key = _resolve_private_key(self._extra)
        return await _exec_buzz(
            self.cli_path,
            args,
            relay_url=self.relay_url,
            private_key=self._private_key,
            input_text=input_text,
        )

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Verify credentials, seed high-water marks, and start inbound transport."""
        if self._config_errors:
            message = "; ".join(dict.fromkeys(self._config_errors))
            logger.error("Buzz configuration invalid: %s", message)
            self._set_fatal_error("config_invalid", message, retryable=False)
            return False
        if not self.relay_url:
            logger.error("Buzz: relay URL must be configured")
            self._set_fatal_error("config_missing", "BUZZ_RELAY_URL must be set", retryable=False)
            return False
        if not self.cli_path:
            logger.error("Buzz: buzz CLI binary not found (set BUZZ_CLI_PATH or put 'buzz' on PATH)")
            self._set_fatal_error("cli_missing", "buzz CLI binary not found", retryable=False)
            return False
        self._private_key = _resolve_private_key(self._extra)
        if not self._private_key:
            logger.error("Buzz: no private key (set BUZZ_PRIVATE_KEY or a credentials file)")
            self._set_fatal_error("config_missing", "BUZZ_PRIVATE_KEY must be set", retryable=False)
            return False

        # Learn our own identity: pubkey drives self-echo suppression and
        # display name drives channel mention gating.
        code, out, err = await self._run_cli(["users", "get"])
        if code != 0:
            message = _cli_error_message(err, code)
            logger.error("Buzz: failed to fetch own profile from %s — %s", self.relay_url, message)
            self._set_fatal_error("connect_failed", message, retryable=code == 2)
            return False
        profiles = _parse_json_list(out)
        if not profiles or not profiles[0].get("pubkey"):
            logger.error("Buzz: 'users get' returned no profile — is the key a member of this community?")
            self._set_fatal_error("connect_failed", "buzz users get returned no profile", retryable=True)
            return False
        self._self_pubkey = str(profiles[0]["pubkey"]).lower()
        self._display_name = str(profiles[0].get("display_name") or "").strip()
        self._self_npub = hex_to_npub(self._self_pubkey) or ""

        # Prevent two profiles from driving the same Buzz identity on the
        # same relay (duplicate replies, split de-dupe state). Mirrors the
        # IRC adapter's scoped-lock pattern.
        try:
            from gateway.status import acquire_scoped_lock

            lock_key = f"{self.relay_url}:{self._self_pubkey}"
            if not acquire_scoped_lock("buzz", lock_key):
                logger.error(
                    "Buzz: identity %s… on %s already in use by another profile",
                    self._self_pubkey[:8],
                    self.relay_url,
                )
                self._set_fatal_error(
                    "lock_conflict", "Buzz identity in use by another profile", retryable=False
                )
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None  # status module not available (e.g. tests)

        # Map channel ids to names and pick the watch set.
        code, out, err = await self._run_cli(["channels", "list"])
        if code != 0:
            message = _cli_error_message(err, code)
            logger.error("Buzz: failed to list channels — %s", message)
            self._set_fatal_error("connect_failed", message, retryable=code == 2)
            return False
        listed = _parse_json_list(out)
        self._channel_names = {
            str(ch.get("channel_id")): str(ch.get("name") or ch.get("channel_id"))
            for ch in listed
            if ch.get("channel_id")
        }
        for ch in listed:
            if ch.get("channel_id"):
                self._channel_meta[str(ch["channel_id"])] = ch
        watch = self.channels or list(self._channel_names)
        if not watch:
            logger.error("Buzz: no channels to watch (configure BUZZ_CHANNELS or join a channel)")
            self._set_fatal_error("config_missing", "no Buzz channels to watch", retryable=False)
            return False

        # Seed high-water marks from the newest events so a (re)start never
        # replays channel history into the agent.
        for channel_id in watch:
            await self._seed_channel(channel_id, chat_type="group")
        await self._discover_dms(seed=True)

        # Inbound transport: prefer the NIP-42-authenticated WebSocket
        # subscription (push, near-zero latency); fall back to CLI polling
        # when the WS can't be established (transport="auto") or when the
        # user pinned transport="poll".
        transport_used = "poll"
        if self.transport in ("auto", "websocket"):
            if await self._start_websocket():
                transport_used = "websocket"
            elif self.transport == "websocket":
                self._set_fatal_error(
                    "ws_auth_failed",
                    "Buzz WebSocket transport did not authenticate (transport=websocket)",
                    retryable=True,
                )
                await self.disconnect()
                return False
        if transport_used == "poll":
            self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        logger.info(
            "Buzz: connected to %s as %s, watching %d channel(s) via %s%s",
            self.relay_url,
            self._display_name or self._self_npub[:16],
            len(self._channel_state),
            transport_used,
            "" if transport_used == "websocket" else f", poll interval {self.poll_interval:.1f}s",
        )
        return True

    async def disconnect(self) -> None:
        """Stop the inbound transport and drop runtime state."""
        self._mark_disconnected()
        lock_key = getattr(self, "_lock_key", None)
        if lock_key:
            try:
                from gateway.status import release_scoped_lock

                release_scoped_lock("buzz", lock_key)
            except Exception:
                pass
            self._lock_key = None
        self._ws_active = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        self._channel_state = {}
        self._active_agent_events = {}
        self._poll_count = 0

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not content:
            return SendResult(success=False, error="Empty message")
        args = ["messages", "send", "--channel", str(chat_id), "--content", "-"]
        mentions = self._explicit_mentions(content)
        for pubkey in mentions:
            args += ["--mention", pubkey]
        reply_target = self._outbound_reply_target(
            str(chat_id),
            mentions,
            reply_to or (metadata or {}).get("thread_id"),
        )
        if reply_target:
            args += ["--reply-to", str(reply_target)]
        code, out, err = await self._run_cli(args, input_text=content)
        if code != 0:
            return SendResult(
                success=False,
                error=_cli_error_message(err, code),
                retryable=code == 2,
            )
        try:
            data = json.loads(out or "{}")
        except ValueError:
            data = {}
        event_id = data.get("event_id")
        if event_id:
            # Belt-and-braces echo suppression: the poll loop already skips
            # our own pubkey, but marking the id seen makes de-dupe explicit.
            self._mark_seen(str(chat_id), str(event_id))
            if self.max_agent_hops:
                parent_hops = self._outbound_parent_hops(str(chat_id), reply_target)
                self._remember_agent_hop(str(event_id), parent_hops + 1)
        return SendResult(
            success=bool(data.get("accepted", True)),
            message_id=str(event_id) if event_id else None,
            raw_response=data,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Buzz has no typing indicator API — no-op."""
        pass

    async def send_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction to a message via buzz-cli.

        Returns True on success, False on failure. Errors are logged but not
        raised — reactions are best-effort and should never block the main
        message flow.
        """
        if not self.cli_path or not emoji or not message_id:
            return False
        # buzz-cli: `reactions add --event <64-char hex event id> --emoji <e>`.
        # The event id IS the message_id we recorded on dispatch; channel is
        # not a parameter to this subcommand.
        args = [
            "reactions", "add",
            "--event", str(message_id),
            "--emoji", emoji,
        ]
        code, _out, err = await self._run_cli(args)
        if code != 0:
            logger.debug(
                "Buzz: reaction add failed for message %s in %s — %s",
                message_id[:12], chat_id, _cli_error_message(err, code),
            )
            return False
        return True

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image: local files upload via --file, URLs go as a link."""
        local = Path(image_url).expanduser() if not image_url.startswith(("http://", "https://")) else None
        if local is not None and local.is_file():
            args = [
                "messages", "send",
                "--channel", str(chat_id),
                "--file", str(local),
                "--content", "-",
            ]
            mentions = self._explicit_mentions(caption or "")
            for pubkey in mentions:
                args += ["--mention", pubkey]
            reply_target = self._outbound_reply_target(
                str(chat_id),
                mentions,
                reply_to or (metadata or {}).get("thread_id"),
            )
            if reply_target:
                args += ["--reply-to", str(reply_target)]
            code, out, err = await self._run_cli(args, input_text=caption or "")
            if code != 0:
                return SendResult(success=False, error=_cli_error_message(err, code), retryable=code == 2)
            try:
                data = json.loads(out or "{}")
            except ValueError:
                data = {}
            event_id = data.get("event_id")
            if event_id:
                self._mark_seen(str(chat_id), str(event_id))
                if self.max_agent_hops:
                    parent_hops = self._outbound_parent_hops(str(chat_id), reply_target)
                    self._remember_agent_hop(str(event_id), parent_hops + 1)
            return SendResult(
                success=bool(data.get("accepted", True)),
                message_id=str(event_id) if event_id else None,
                raw_response=data,
            )
        # Markdown renders in Buzz, so a URL arrives as a clickable image link.
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_id = str(chat_id)
        state = self._channel_state.get(chat_id)
        chat_type = state["chat_type"] if state else "group"
        name = self._channel_names.get(chat_id)
        if name is None and self.cli_path:
            code, out, _err = await self._run_cli(["channels", "get", "--channel", chat_id])
            if code == 0:
                try:
                    data = json.loads(out or "{}")
                    if isinstance(data, dict) and data.get("name"):
                        name = str(data["name"])
                        self._channel_names[chat_id] = name
                except ValueError:
                    pass
        return {"name": name or chat_id, "type": chat_type, "chat_id": chat_id}

    # ── Inbound: WebSocket transport (NIP-42 authenticated) ──────────────
    #
    # Push transport contributed in PR #73636 by @ScaleLeanChris, adapted to
    # dispatch through the same _handle_event() machinery as the poll loop so
    # de-dupe, mention gating, DM latching, and the allow-list behave
    # identically on both transports.

    def _websocket_url(self) -> str:
        parsed = urlsplit(self.relay_url.strip())
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        if scheme not in ("ws", "wss") or not parsed.netloc:
            raise ValueError("Buzz relay URL must use http(s) or ws(s)")
        return urlunsplit((scheme, parsed.netloc, parsed.path or "", parsed.query, ""))

    async def _start_websocket(self) -> bool:
        """Start the WS loop; True when it authenticates within the timeout."""
        try:
            import websockets  # noqa: F401  (availability probe)

            self._websocket_url()
        except Exception as e:
            logger.info("Buzz: WebSocket transport unavailable (%s); falling back to polling", e)
            return False
        self._ws_ready = asyncio.Event()
        self._membership_since = int(time.time())
        self._ws_task = asyncio.create_task(self._websocket_loop())
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=_WS_AUTH_TIMEOUT + 5)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Buzz: WebSocket did not authenticate in time")
            self._ws_active = False
            if self._ws_task and not self._ws_task.done():
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
            self._ws_task = None
            return False
        return True

    async def _authenticate_websocket(self, websocket) -> None:
        """NIP-42: wait for the relay's AUTH challenge, answer with a signed
        kind-22242 event (plus the optional NIP-OA owner-attestation tag from
        BUZZ_AUTH_TAG), and wait for the OK acknowledgment."""
        build_auth_event = _load_nostr_auth().build_auth_event

        raw = await asyncio.wait_for(websocket.recv(), timeout=_WS_AUTH_TIMEOUT)
        message = json.loads(raw)
        if not isinstance(message, list) or len(message) < 2 or message[0] != "AUTH":
            raise ConnectionError("Buzz relay did not send a NIP-42 AUTH challenge")
        event = build_auth_event(
            private_key=self._private_key,
            challenge=str(message[1]),
            relay_url=self._websocket_url(),
            auth_tag_json=os.getenv("BUZZ_AUTH_TAG", ""),
        )
        await websocket.send(json.dumps(["AUTH", event], separators=(",", ":")))
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=_WS_AUTH_TIMEOUT)
            response = json.loads(raw)
            if not isinstance(response, list) or not response:
                continue
            if response[0] == "OK" and len(response) >= 4 and response[1] == event["id"]:
                if response[2] is True:
                    return
                raise ConnectionError(f"Buzz WebSocket AUTH rejected: {response[3]}")
            if response[0] in ("NOTICE", "CLOSED"):
                detail = response[-1] if len(response) > 1 else "authentication failed"
                raise ConnectionError(f"Buzz WebSocket AUTH failed: {detail}")

    async def _send_channel_subscription(self, websocket, subscription_id: str, channel_id: str) -> None:
        state = self._channel_state.get(channel_id) or {}
        since = max(int(state.get("last_ts") or time.time()) - 1, 0)
        request = [
            "REQ",
            subscription_id,
            {"kinds": [_CHAT_KIND], "#h": [channel_id], "since": since},
        ]
        await websocket.send(json.dumps(request, separators=(",", ":")))

    async def _subscribe_websocket(self, websocket) -> Dict[str, Optional[str]]:
        """Subscribe to every watched conversation plus membership events
        (kind 44100 p-tagged to us) for live DM discovery."""
        subscriptions: Dict[str, Optional[str]] = {}
        for index, channel_id in enumerate(list(self._channel_state)):
            subscription_id = f"hermes-buzz-{index}"
            subscriptions[subscription_id] = channel_id
            await self._send_channel_subscription(websocket, subscription_id, channel_id)
        if self._self_pubkey:
            request = [
                "REQ",
                _WS_MEMBERSHIP_SUB_ID,
                {
                    "kinds": [_WS_MEMBERSHIP_KIND],
                    "#p": [self._self_pubkey],
                    "since": max(self._membership_since - 1, 0),
                },
            ]
            await websocket.send(json.dumps(request, separators=(",", ":")))
            subscriptions[_WS_MEMBERSHIP_SUB_ID] = None
        return subscriptions

    async def _handle_membership_event(self, websocket, subscriptions: Dict[str, Optional[str]], event: dict) -> None:
        """A membership event p-tagged to us: rediscover conversations and
        subscribe to any new ones (fresh DMs dispatch from their beginning)."""
        self._membership_since = max(self._membership_since, int(event.get("created_at") or 0))
        before = set(self._channel_state)
        await self._discover_dms(seed=False)
        for channel_id in self._channel_state:
            if channel_id in before:
                continue
            subscription_id = f"hermes-buzz-dm-{len(subscriptions)}"
            subscriptions[subscription_id] = channel_id
            await self._send_channel_subscription(websocket, subscription_id, channel_id)
            logger.info("Buzz: subscribed to new conversation %s", channel_id)

    async def _websocket_loop(self) -> None:
        """Persistent authenticated subscription with bounded reconnect
        backoff. Events route through _handle_event() — identical semantics
        to the poll loop. On reconnect, per-channel `since` filters resume
        from the last observed timestamps (same-second overlap de-duped by
        event id)."""
        import websockets

        backoff = 1.0
        try:
            while True:
                try:
                    async with websockets.connect(
                        self._websocket_url(),
                        open_timeout=_WS_AUTH_TIMEOUT,
                        close_timeout=5,
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=_WS_MAX_MESSAGE_BYTES,
                    ) as websocket:
                        await self._authenticate_websocket(websocket)
                        subscriptions = await self._subscribe_websocket(websocket)
                        self._ws_active = True
                        if self._ws_ready is not None:
                            self._ws_ready.set()
                        backoff = 1.0
                        async for raw in websocket:
                            try:
                                message = json.loads(raw)
                            except (ValueError, TypeError):
                                logger.warning("Buzz: ignoring malformed WebSocket frame")
                                continue
                            if not isinstance(message, list) or not message:
                                continue
                            if message[0] == "EVENT" and len(message) >= 3:
                                subscription_id = str(message[1])
                                event = message[2]
                                if not isinstance(event, dict):
                                    continue
                                if subscription_id == _WS_MEMBERSHIP_SUB_ID:
                                    await self._handle_membership_event(websocket, subscriptions, event)
                                    continue
                                channel_id = subscriptions.get(subscription_id)
                                state = self._channel_state.get(channel_id or "")
                                if channel_id and state is not None:
                                    await self._handle_event(channel_id, state, event)
                                    self._trim_seen(state)
                            elif message[0] == "CLOSED":
                                detail = message[-1] if len(message) > 2 else "subscription closed"
                                raise ConnectionError(str(detail))
                            elif message[0] == "NOTICE":
                                logger.warning("Buzz: relay notice: %s", message[-1])
                        self._ws_active = False
                        self._active_agent_events = {}
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._ws_active = False
                    self._active_agent_events = {}
                    logger.warning("Buzz: WebSocket disconnected; retrying in %.1fs: %s", backoff, e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            self._ws_active = False
            self._active_agent_events = {}

    # ── Inbound polling ───────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll every watched channel for new events until cancelled."""
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                self._poll_count += 1
                try:
                    if self._poll_count % _DM_DISCOVERY_EVERY == 0:
                        await self._discover_dms(seed=False)
                    for channel_id in list(self._channel_state):
                        await self._poll_channel(channel_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Buzz: poll sweep failed", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def _seed_channel(self, channel_id: str, chat_type: str) -> None:
        """Initialize a channel's high-water mark from its newest events."""
        state = {"chat_type": chat_type, "last_ts": 0, "seen": OrderedDict()}
        self._channel_state[channel_id] = state
        code, out, err = await self._run_cli(
            ["messages", "get", "--channel", channel_id, "--limit", str(_FETCH_LIMIT)]
        )
        if code != 0:
            logger.warning(
                "Buzz: could not seed channel %s — %s", channel_id, _cli_error_message(err, code)
            )
            # Fall back to "now" so a transiently unreadable channel does not
            # replay its whole history once it becomes readable.
            state["last_ts"] = int(time.time())
            return
        events = self._causal_event_order(_parse_json_list(out))
        for event in events:
            event_id = event.get("id")
            created_at = int(event.get("created_at") or 0)
            if event_id:
                state["seen"][str(event_id)] = None
                self._record_agent_hop(event)
            state["last_ts"] = max(state["last_ts"], created_at)
            # History is never dispatched, but it still classifies: a DM that
            # leaked in via ``channels list`` latches to chat_type="dm" here,
            # so it bypasses the mention gate from the very first poll.
            self._maybe_latch_dm(channel_id, state, event)
        self._trim_seen(state)

    async def _discover_dms(self, *, seed: bool) -> None:
        """Watch DM conversations.  New ones found mid-run dispatch from their
        beginning (a fresh conversation has no history worth suppressing);
        ones present at startup are seeded like channels.

        ``dms list`` is only a best-effort source: on some hosted relays it
        returns ``[]`` even when DM conversations exist (#68871).  Those DMs
        DO surface in ``channels list`` as entries named "DM" with an empty
        description, so that listing is scanned as a fallback.  Fallback
        finds are watched as ``group`` and latch to ``dm`` via p-tag
        detection (_is_direct_message_event) rather than trusting the name
        alone to unlock the mention-free DM path.
        """
        code, out, _err = await self._run_cli(["dms", "list"])
        if code == 0:
            for dm in _parse_json_list(out):
                dm_id = str(dm.get("dm_id") or "")
                if not dm_id or dm_id in self._channel_state:
                    continue
                if seed:
                    await self._seed_channel(dm_id, chat_type="dm")
                else:
                    self._channel_state[dm_id] = {"chat_type": "dm", "last_ts": 0, "seen": OrderedDict()}
                self._channel_names.setdefault(dm_id, "DM")

        code, out, _err = await self._run_cli(["channels", "list"])
        if code != 0:
            return
        for ch in _parse_json_list(out):
            ch_id = str(ch.get("channel_id") or "")
            if not ch_id:
                continue
            self._channel_meta[ch_id] = ch
            self._channel_names.setdefault(ch_id, str(ch.get("name") or ch_id))
            if ch_id in self._channel_state or not self._may_reclassify_as_dm(ch_id):
                continue
            if seed:
                await self._seed_channel(ch_id, chat_type="group")
            else:
                self._channel_state[ch_id] = {"chat_type": "group", "last_ts": 0, "seen": OrderedDict()}

    async def _poll_channel(self, channel_id: str) -> None:
        state = self._channel_state.get(channel_id)
        if state is None:
            return
        args = ["messages", "get", "--channel", channel_id, "--limit", str(_FETCH_LIMIT)]
        if state["last_ts"]:
            # Nostr `since` is inclusive: same-second events are re-fetched
            # and de-duped by id below.
            args += ["--since", str(state["last_ts"])]
        code, out, err = await self._run_cli(args)
        if code != 0:
            logger.debug(
                "Buzz: poll of channel %s failed — %s", channel_id, _cli_error_message(err, code)
            )
            return
        events = self._causal_event_order(_parse_json_list(out))
        for event in events:
            await self._handle_event(channel_id, state, event)
        self._trim_seen(state)

    async def _handle_event(self, channel_id: str, state: dict, event: dict) -> None:
        """De-dupe, filter, and dispatch a single ``messages get`` event."""
        event_id = str(event.get("id") or "")
        created_at = int(event.get("created_at") or 0)
        if not event_id or event_id in state["seen"]:
            return
        state["seen"][event_id] = None
        state["last_ts"] = max(state["last_ts"], created_at)

        if int(event.get("kind") or 0) != _CHAT_KIND:
            return
        pubkey = str(event.get("pubkey") or "").lower()
        content = event.get("content")
        if not pubkey or not isinstance(content, str) or not content.strip():
            return
        agent_hops = self._record_agent_hop(event)

        # Suppress self-echo: never dispatch our own messages back to the agent.
        if pubkey == self._self_pubkey:
            return

        # Reclassify a leaked DM before gating so its first un-mentioned
        # message both latches the conversation and dispatches.
        self._maybe_latch_dm(channel_id, state, event)

        is_dm = state["chat_type"] == "dm"

        # Adapter-level allow-list (the gateway applies BUZZ_ALLOWED_USERS /
        # BUZZ_ALLOW_ALL_USERS centrally as well; empty list = no filter here).
        if self._allowed_pubkeys and pubkey not in self._allowed_pubkeys:
            logger.debug("Buzz: ignoring message from unauthorized pubkey %s…", pubkey[:8])
            return

        # Per-sender mention policy supports ambient human messages alongside
        # explicitly directed agent-to-agent work. A real Nostr p-tag and a
        # visible mention are both required, so presentation-only @text cannot
        # invoke another agent and structural DM p-tags do not bypass the gate.
        if pubkey in self._mention_required_pubkeys:
            if self.max_agent_hops and agent_hops > self.max_agent_hops:
                logger.warning(
                    "Buzz: dropping agent reply at hop %s (limit %s)",
                    agent_hops,
                    self.max_agent_hops,
                )
                return
            if not self._event_mentions_self(event) or not self._has_visible_self_mention(content):
                return
        # In shared channels, respond only when addressed — unless
        # require_mention is disabled, in which case respond to every message.
        # DMs from ordinary allowed users always dispatch.
        elif not is_dm and self.require_mention and not self._is_mentioned(content):
            return

        if pubkey in self._mention_required_pubkeys:
            # The most recently accepted agent event is the active response
            # parent even when it starts a fresh lower-hop chain. Keeping the
            # previous highest-hop event would misthread the new response.
            self._active_agent_events[channel_id] = (event_id, agent_hops)
        else:
            # A fresh human instruction starts a new interaction root.
            self._active_agent_events.pop(channel_id, None)

        # Strip a leading @mention so slash commands (@Chip /whoami ->
        # /whoami) and clean prompts are recognized. DM messages often still
        # open with "@Chip" even though no mention is required there, so the
        # strip applies to both chat types.
        dispatch_text = self._strip_mention(content)

        await self._dispatch_message(
            text=dispatch_text,
            chat_id=channel_id,
            chat_type="dm" if is_dm else "group",
            user_id=pubkey,
            user_name=await self._resolve_user_name(pubkey),
            message_id=event_id,
            created_at=created_at,
        )

    # ── DM classification (issue #68871) ──────────────────────────────────
    #
    # ``buzz dms list`` returns [] on some hosted relays even when DM
    # conversations exist, so DMs leak in via ``channels list`` and get
    # watched as chat_type="group" — which wrongly puts them behind the
    # channel mention gate.  Classification therefore keys off the Nostr
    # tags of the messages themselves.  Observed on a live hosted relay:
    #
    #   * every message another user sends IN A DM carries a structural
    #     ["p", <our pubkey>] tag, even when the text never mentions us
    #     (recipient addressing);
    #   * in a real channel, a ["p", <our pubkey>] tag appears only when the
    #     text visibly @mentions us (typed mention, with or without a reply
    #     ["e", ...] tag) — never on plain broadcasts.
    #
    # So "p-tagged to self WITHOUT a visible mention in the content" is the
    # DM discriminator: in a channel that combination does not occur, and a
    # channel reply/mention that p-tags us is excluded because the mention
    # is right there in the text.  As a second, independent guard, a
    # conversation whose ``channels list`` metadata looks like a real
    # community channel (real name / non-empty description) is never
    # reclassified at all, whereas relay-materialized DMs are always named
    # "DM" with an empty description.  Nothing is lost while unlatched: a
    # DM message that DOES mention us dispatches through the mention gate
    # anyway, so the latch flips exactly on the first message that needs it.

    def _may_reclassify_as_dm(self, channel_id: str) -> bool:
        """True when the conversation's metadata does not rule out a DM.

        Known real community channels (real name or non-empty description in
        ``channels list``) must never turn into DMs just because a message
        p-tags us.  A conversation with no metadata at all is trusted only
        when the user did not explicitly configure it as a watched channel.
        """
        meta = self._channel_meta.get(channel_id)
        if meta is None:
            return channel_id not in self.channels
        name = str(meta.get("name") or "").strip()
        description = str(meta.get("description") or "").strip()
        return name == "DM" and not description

    def _is_direct_message_event(self, channel_id: str, event: dict) -> bool:
        """True when ``event`` is shaped like a direct message to us: a chat
        message from another user, p-tagged to our pubkey, whose content does
        NOT visibly mention us — i.e. the p-tag is structural DM addressing,
        not the artifact of a typed @mention (see block comment above)."""
        if not self._self_pubkey or not self._may_reclassify_as_dm(channel_id):
            return False
        if int(event.get("kind") or 0) != _CHAT_KIND:
            return False
        pubkey = str(event.get("pubkey") or "").lower()
        if not pubkey or pubkey == self._self_pubkey:
            return False
        tags = event.get("tags")
        if not isinstance(tags, list):
            return False
        p_tagged_to_self = any(
            isinstance(tag, (list, tuple))
            and len(tag) > 1
            and tag[0] == "p"
            and str(tag[1]).lower() == self._self_pubkey
            for tag in tags
        )
        if not p_tagged_to_self:
            return False
        content = event.get("content")
        return isinstance(content, str) and not self._is_mentioned(content)

    def _maybe_latch_dm(self, channel_id: str, state: dict, event: dict) -> None:
        """Latch a group conversation to chat_type="dm" once any direct
        message is seen; the classification then sticks so subsequent
        un-mentioned messages in the conversation dispatch too."""
        if state["chat_type"] == "dm" or not self._is_direct_message_event(channel_id, event):
            return
        state["chat_type"] = "dm"
        self._channel_names.setdefault(channel_id, "DM")
        logger.info("Buzz: conversation %s reclassified as DM (message p-tagged to self)", channel_id)

    def _is_mentioned(self, content: str) -> bool:
        """True when the message addresses this agent (npub, hex, or name)."""
        lowered = content.lower()
        if self._self_pubkey and self._self_pubkey in lowered:
            return True
        if self._self_npub and self._self_npub in lowered:
            return True
        if self._display_name:
            pattern = rf"(?<!\w)@?{re.escape(self._display_name.lower())}(?![\w.-])"
            if re.search(pattern, lowered):
                return True
        for alias, pubkey in self._mention_aliases.items():
            if pubkey != self._self_pubkey:
                continue
            pattern = rf"(?<!\w)@?{re.escape(alias)}(?![\w.-])"
            if re.search(pattern, lowered):
                return True
        return False

    @staticmethod
    def _is_alias_continuation(char: str) -> bool:
        """Return whether ``char`` can visually continue an alias token."""
        if not char:
            return False
        category = unicodedata.category(char)
        return (
            char in "_.-"
            or ("a" + char).isidentifier()
            or category[0] in {"L", "M", "N"}
            or category in {"Cf", "Pc"}
        )

    @staticmethod
    def _is_alias_path_or_address_join(
        content: str, match: re.Match[str]
    ) -> bool:
        """Reject aliases joined directly to URI/path/address syntax."""
        if match.start() > 0 and content[match.start() - 1] in "/\\+?#=&;%:":
            return True
        if match.end() >= len(content):
            return False
        following = content[match.end()]
        if following in "/\\+=&%":
            return True
        if following in "?#;:" and match.end() + 1 < len(content):
            return not content[match.end() + 1].isspace()
        return False

    @classmethod
    def _alias_match(
        cls, content: str, alias: str, *, leading: bool = False
    ) -> Optional[re.Match[str]]:
        """Find one exact ASCII alias with Unicode-safe token boundaries."""
        pattern = re.compile(rf"@{re.escape(alias)}", re.IGNORECASE | re.ASCII)
        for match in pattern.finditer(content):
            if leading and match.start() != 0:
                return None
            if not leading and match.start() > 0:
                previous = content[match.start() - 1]
                if previous == "@" or cls._is_alias_continuation(previous):
                    continue
            if match.end() < len(content) and cls._is_alias_continuation(
                content[match.end()]
            ):
                continue
            if cls._is_alias_path_or_address_join(content, match):
                continue
            return match
        return None

    @staticmethod
    def _invocation_text(content: str) -> str:
        """Return visible Markdown prose eligible to contain invocation aliases."""
        # Character references are decoded into ordinary text by CommonMark.
        # Replace their source ranges first so they cannot synthesize an alias
        # such as ``@Se&#108;f`` or ``&#64;Self`` after parsing.
        source = _INVOCATION_CHARACTER_REFERENCE_RE.sub(
            _INVOCATION_HIDDEN_BOUNDARY, content
        )
        parser = _RawHtmlInvocationTextParser()
        blockquote_depth = 0

        for token in _INVOCATION_MARKDOWN.parse(source):
            if token.type == "blockquote_open":
                blockquote_depth += 1
                parser.add_hidden_boundary()
                continue
            if token.type == "blockquote_close":
                blockquote_depth = max(0, blockquote_depth - 1)
                parser.add_text("\n")
                continue
            if blockquote_depth:
                continue
            if token.type in {"fence", "code_block"}:
                parser.add_hidden_boundary()
                parser.add_text("\n")
                continue
            if token.type == "html_block":
                parser.feed(token.content)
                parser.add_text("\n")
                continue
            if token.type != "inline" or not token.children:
                continue

            autolink_depth = 0
            for child in token.children:
                if child.type == "link_open" and child.markup == "autolink":
                    autolink_depth += 1
                    parser.add_hidden_boundary()
                    continue
                if child.type == "link_close" and child.markup == "autolink":
                    autolink_depth = max(0, autolink_depth - 1)
                    parser.add_hidden_boundary()
                    continue
                if autolink_depth:
                    continue
                if child.type == "text":
                    parser.add_text(child.content)
                elif child.type in {"softbreak", "hardbreak"}:
                    parser.add_text("\n")
                elif child.type in {"code_inline", "image"}:
                    parser.add_hidden_boundary()
                elif child.type == "html_inline":
                    parser.feed(child.content)
            parser.add_text("\n")

        parser.close()
        return _INVOCATION_URL_RE.sub("", parser.text)

    def _has_visible_self_mention(self, content: str) -> bool:
        """Require an exact configured self @alias outside code and quotes."""
        invocation_text = self._invocation_text(content)
        return any(
            self._alias_match(invocation_text, alias)
            for alias, pubkey in self._mention_aliases.items()
            if pubkey == self._self_pubkey
        )

    def _event_mentions_self(self, event: dict) -> bool:
        """Return True only when the event carries a Nostr p-tag for us."""
        if not self._self_pubkey:
            return False
        tags = event.get("tags")
        if not isinstance(tags, list):
            return False
        return any(
            isinstance(tag, (list, tuple))
            and len(tag) > 1
            and tag[0] == "p"
            and str(tag[1]).lower() == self._self_pubkey
            for tag in tags
        )

    def _explicit_mentions(self, content: str) -> List[str]:
        """Resolve configured @aliases outside code and quotes to pubkeys."""
        invocation_text = self._invocation_text(content)
        mentions = []
        for alias, pubkey in sorted(self._mention_aliases.items(), key=lambda item: -len(item[0])):
            if self._alias_match(invocation_text, alias) and pubkey not in mentions:
                mentions.append(pubkey)
        return mentions

    def _outbound_parent_hops(self, chat_id: str, reply_target: Optional[str]) -> int:
        """Return known parent hops, including retained active-state metadata."""
        target = str(reply_target or "")
        if target in self._agent_hops:
            return self._agent_hops[target]
        active = self._active_agent_events.get(chat_id)
        if active and active[0] == target:
            return active[1]
        if target and self.max_agent_hops:
            return self.max_agent_hops
        return 0

    def _outbound_reply_target(
        self,
        chat_id: str,
        mentions: List[str],
        requested: Optional[str],
    ) -> Optional[str]:
        """Preserve chain ancestry when queued replies lose gateway metadata.

        A human message clears the active agent chain. Therefore an explicit
        outbound agent mention with no supplied reply id can safely attach to
        the latest accepted agent event in the channel instead of resetting
        the structural hop counter.
        """
        if not self.max_agent_hops or not mentions:
            return str(requested) if requested else None
        if requested:
            return str(requested)
        active = self._active_agent_events.get(chat_id)
        return active[0] if active else None

    @staticmethod
    def _reply_parent_id(event: dict) -> str:
        tags = event.get("tags")
        if not isinstance(tags, list):
            return ""
        fallback = ""
        for tag in tags:
            if not isinstance(tag, (list, tuple)) or len(tag) < 2 or tag[0] != "e":
                continue
            event_id = str(tag[1])
            if len(tag) > 3 and tag[3] == "reply":
                return event_id
            fallback = event_id
        return fallback

    def _causal_event_order(self, events: List[dict]) -> List[dict]:
        """Order fetched events by time and reply ancestry (parent before child)."""
        stable = sorted(events, key=lambda event: int(event.get("created_at") or 0))
        by_id = {str(event.get("id")): event for event in stable if event.get("id")}
        ordered: List[dict] = []
        visited: set[str] = set()
        active: set[str] = set()

        def visit(event: dict) -> None:
            event_id = str(event.get("id") or "")
            if event_id and event_id in visited:
                return
            if event_id and event_id in active:
                return
            if event_id:
                active.add(event_id)
            parent = by_id.get(self._reply_parent_id(event))
            if parent is not None:
                visit(parent)
            if event_id:
                active.discard(event_id)
                visited.add(event_id)
            ordered.append(event)

        for event in stable:
            visit(event)
        return ordered

    def _remember_agent_hop(self, event_id: str, hops: int) -> int:
        if event_id:
            self._agent_hops[event_id] = hops
            self._agent_hops.move_to_end(event_id)
            while len(self._agent_hops) > _SEEN_CAP:
                self._agent_hops.popitem(last=False)
        return hops

    def _record_agent_hop(self, event: dict) -> int:
        """Track consecutive agent replies so mutual conversations are bounded."""
        event_id = str(event.get("id") or "")
        pubkey = str(event.get("pubkey") or "").lower()
        is_agent = pubkey in self._mention_required_pubkeys or (
            bool(self.max_agent_hops) and pubkey == self._self_pubkey
        )
        if not is_agent:
            return self._remember_agent_hop(event_id, 0)
        parent_id = self._reply_parent_id(event)
        if parent_id and parent_id not in self._agent_hops:
            # A pushed/replayed child can arrive before its parent. Never cache
            # a guessed low value that weakens a configured chain limit.
            unknown_parent_hops = self.max_agent_hops + 1 if self.max_agent_hops else 1
            return self._remember_agent_hop(event_id, unknown_parent_hops)
        return self._remember_agent_hop(event_id, self._agent_hops.get(parent_id, 0) + 1)

    def _strip_mention(self, content: str) -> str:
        """Remove a leading @mention of this agent so the remaining text can be
        recognized as a slash command or clean prompt.

        Mirrors the Discord adapter, which strips its own ``<@id>`` mention
        before dispatch. Without this a channel message like ``@Chip /whoami``
        arrives with a leading ``@Chip``; the gateway's ``is_command()`` checks
        ``text.lstrip().startswith("/")`` and never fires the command. Only a
        LEADING mention is stripped (case-insensitive); mentions mid-sentence
        are left intact so normal prose is unaffected.
        """
        text = content.strip()
        self_aliases = []
        for alias, pubkey in self._mention_aliases.items():
            if pubkey == self._self_pubkey:
                self_aliases.append(alias)
        for alias in sorted(self_aliases, key=len, reverse=True):
            match = self._alias_match(text, alias, leading=True)
            if match:
                return text[match.end() :].lstrip(" \t:,").strip()

        # Preserve the permissive legacy stripping behavior only when no
        # configured self alias exists. Protected peer dispatch never uses
        # these alternate identity forms for authorization.
        candidates = []
        if self_aliases:
            return text
        if self._display_name:
            candidates.append(re.escape(self._display_name))
        if self._self_npub:
            candidates.append(re.escape(self._self_npub))
        if self._self_pubkey:
            candidates.append(re.escape(self._self_pubkey))
        if not candidates:
            return text
        # Optional leading '@', one of the identity forms, optional trailing
        # ':' or ',' and surrounding whitespace. Prefer longer candidates and
        # require an alias-token boundary so @bot never strips @bot-2.
        alternatives = "|".join(sorted(set(candidates), key=len, reverse=True))
        pattern = rf"^@?(?:{alternatives})(?![\w.-])[\s:,]*"
        stripped = re.sub(
            pattern,
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        return stripped.strip()

    async def _resolve_user_name(self, pubkey: str) -> str:
        """Resolve a pubkey to a display name (cached; falls back to npub prefix).

        Failures are cached too (negative caching): without it, every message
        from a profile-less pubkey re-runs ``users get`` each poll sweep,
        which amplifies badly when several adapter instances poll in one
        process.
        """
        cached = self._user_names.get(pubkey)
        if cached is not None:
            return cached
        name = ""
        code, out, _err = await self._run_cli(["users", "get", "--pubkey", pubkey])
        if code == 0:
            profiles = _parse_json_list(out)
            if profiles:
                name = str(profiles[0].get("display_name") or "").strip()
        if not name:
            name = (hex_to_npub(pubkey) or pubkey)[:16]
        self._user_names[pubkey] = name
        return name

    @staticmethod
    def _trim_seen(state: dict) -> None:
        seen = state["seen"]
        while len(seen) > _SEEN_CAP:
            seen.popitem(last=False)

    def _mark_seen(self, channel_id: str, event_id: str) -> None:
        state = self._channel_state.get(channel_id)
        if state is not None:
            state["seen"][event_id] = None
            self._trim_seen(state)

    async def _dispatch_message(
        self,
        text: str,
        chat_id: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        message_id: str,
        created_at: int,
    ) -> None:
        """Build a MessageEvent and hand it to the base class handler."""
        if not self._message_handler:
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=self._channel_names.get(chat_id, chat_id),
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            timestamp=datetime.fromtimestamp(created_at) if created_at else datetime.now(),
        )

        await self.handle_message(event)
        
        # Add a "seen" reaction after dispatching — signals to the user that
        # their message was received and is being processed.
        try:
            await self.send_reaction(chat_id, message_id, "👀")
        except Exception:
            logger.debug("Buzz: reaction failed for message %s", message_id[:12], exc_info=True)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check if Buzz is configured: a relay URL plus a resolvable key."""
    if not os.getenv("BUZZ_RELAY_URL", "").strip():
        return False
    return bool(_resolve_private_key())


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    relay = os.getenv("BUZZ_RELAY_URL") or extra.get("relay_url", "")
    return bool(relay and _resolve_private_key(extra))


def is_connected(config) -> bool:
    """Check whether Buzz is configured (env or config.yaml)."""
    return validate_config(config)


def _apply_yaml_config(yaml_cfg: dict, buzz_cfg: dict) -> Optional[dict]:
    """Translate ``config.yaml`` ``buzz.extra`` keys into ``BUZZ_*`` env vars.

    Implements the ``apply_yaml_config_fn`` contract.  ``check_requirements``
    and the adapter's connect path read configuration from the environment, so
    a config.yaml-only setup (no ``BUZZ_*`` env vars beyond the secret) would
    otherwise fail the ``check_fn`` gate and be silently skipped at gateway
    startup.  This hook bridges the ``extra`` block into env, mirroring the
    Slack/Telegram pattern.  Env vars win over YAML — every assignment is
    guarded by ``not os.getenv(...)`` so explicit env overrides survive a
    config.yaml update.  ``BUZZ_PRIVATE_KEY`` is a secret and stays in ``.env``;
    it is never sourced from config.yaml here.
    """
    extra = buzz_cfg.get("extra", buzz_cfg) or {}
    if not isinstance(extra, dict):
        return None
    _str_keys = {
        "relay_url": "BUZZ_RELAY_URL",
        "cli_path": "BUZZ_CLI_PATH",
        "home_channel": "BUZZ_HOME_CHANNEL",
        "transport": "BUZZ_TRANSPORT",
    }
    for src, env in _str_keys.items():
        val = extra.get(src)
        if val and not os.getenv(env):
            os.environ[env] = str(val)
    interval = extra.get("poll_interval")
    if interval is not None and not os.getenv("BUZZ_POLL_INTERVAL"):
        os.environ["BUZZ_POLL_INTERVAL"] = str(interval)
    channels = extra.get("channels")
    if channels is not None and not os.getenv("BUZZ_CHANNELS"):
        if isinstance(channels, (list, tuple)):
            channels = ",".join(str(c) for c in channels)
        os.environ["BUZZ_CHANNELS"] = str(channels)
    allowed = extra.get("allowed_users")
    if allowed is not None and not os.getenv("BUZZ_ALLOWED_USERS"):
        if isinstance(allowed, (list, tuple)):
            allowed = ",".join(str(a) for a in allowed)
        os.environ["BUZZ_ALLOWED_USERS"] = str(allowed)
    if "allow_all_users" in extra and not os.getenv("BUZZ_ALLOW_ALL_USERS"):
        os.environ["BUZZ_ALLOW_ALL_USERS"] = str(extra["allow_all_users"]).lower()
    if "require_mention" in extra and not os.getenv("BUZZ_REQUIRE_MENTION"):
        os.environ["BUZZ_REQUIRE_MENTION"] = str(extra["require_mention"]).lower()
    return None


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called BEFORE adapter construction so env-only setups show up in
    ``hermes gateway status`` and ``get_connected_platforms()``.  Returns
    ``None`` when Buzz isn't minimally configured.

    The special ``home_channel`` key is handled by the core hook — it becomes
    a proper ``HomeChannel`` on the ``PlatformConfig``.
    """
    relay = os.getenv("BUZZ_RELAY_URL", "").strip()
    if not relay or not _resolve_private_key():
        return None
    seed: dict = {"relay_url": relay}
    channels = os.getenv("BUZZ_CHANNELS", "").strip()
    if channels:
        seed["channels"] = [c.strip() for c in channels.split(",") if c.strip()]
    interval = os.getenv("BUZZ_POLL_INTERVAL", "").strip()
    if interval:
        try:
            seed["poll_interval"] = float(interval)
        except ValueError:
            pass
    cli_path = os.getenv("BUZZ_CLI_PATH", "").strip()
    if cli_path:
        seed["cli_path"] = cli_path
    # Home channel for deliver=buzz cron jobs; defaults to the first watched
    # channel so env-only setups get a sensible target without extra config.
    home = os.getenv("BUZZ_HOME_CHANNEL", "").strip() or (seed.get("channels") or [""])[0]
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("BUZZ_HOME_CHANNEL_NAME", home),
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """One-shot send without a live adapter (out-of-process cron delivery).

    Used by ``tools/send_message_tool`` when ``hermes cron`` runs separately
    from the gateway process.  Without this hook, ``deliver=buzz`` cron jobs
    fail with ``No live adapter for platform 'buzz'``.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    policy = BuzzAdapter(pconfig)
    if policy._config_errors:
        return {"error": f"Buzz standalone send: invalid configuration ({'; '.join(dict.fromkeys(policy._config_errors))})"}
    relay = (os.getenv("BUZZ_RELAY_URL") or extra.get("relay_url", "")).strip()
    private_key = _resolve_private_key(extra)
    cli_path = _resolve_cli_path(
        os.getenv("BUZZ_CLI_PATH", "").strip() or str(extra.get("cli_path", "") or "")
    )
    if not relay or not private_key:
        return {"error": "Buzz standalone send: BUZZ_RELAY_URL and BUZZ_PRIVATE_KEY must be configured"}
    if not cli_path:
        return {"error": "Buzz standalone send: buzz CLI binary not found"}
    target = (chat_id or "").strip() or (os.getenv("BUZZ_HOME_CHANNEL") or str(extra.get("home_channel", "") or "")).strip()
    if not target:
        return {"error": "Buzz standalone send: no target channel (set BUZZ_HOME_CHANNEL)"}

    args = ["messages", "send", "--channel", target, "--content", "-"]
    for pubkey in policy._explicit_mentions(message):
        args += ["--mention", pubkey]
    if thread_id:
        args += ["--reply-to", str(thread_id)]
    for path in media_files or []:
        args += ["--file", str(path)]
    try:
        code, out, err = await _exec_buzz(
            cli_path, args, relay_url=relay, private_key=private_key, input_text=message
        )
    except asyncio.CancelledError:
        raise
    except OSError as e:
        return {"error": f"Buzz standalone send failed to launch CLI: {e}"}
    if code != 0:
        return {"error": f"Buzz standalone send failed: {_cli_error_message(err, code)}"}
    try:
        data = json.loads(out or "{}")
    except ValueError:
        data = {}
    return {"success": True, "message_id": str(data.get("event_id") or "")}


def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for the Buzz platform.

    Lazy-imports ``hermes_cli.setup`` helpers so the plugin stays importable
    in non-CLI contexts (gateway runtime, tests).
    """
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Buzz")
    existing_relay = get_env_value("BUZZ_RELAY_URL")
    if existing_relay:
        print_info(f"Buzz: already configured (relay: {existing_relay})")
        if not prompt_yes_no("Reconfigure Buzz?", False):
            return

    print_info("Connect Hermes to a Buzz community (Block's Nostr-based human+agent platform).")
    print_info("   Requires the buzz CLI binary and a Nostr key that is a community member.")
    print()

    relay = prompt(
        "Relay URL (e.g. https://mycommunity.communities.buzz.xyz)",
        default=existing_relay or "",
    )
    if not relay:
        print_warning("Relay URL is required — skipping Buzz setup")
        return
    save_env_value("BUZZ_RELAY_URL", relay.strip())

    key = prompt("Nostr private key (nsec or hex; leave blank to keep current)", password=True)
    if key:
        save_env_value("BUZZ_PRIVATE_KEY", key.strip())
    elif not _resolve_private_key():
        print_warning("No private key configured — set BUZZ_PRIVATE_KEY before starting the gateway")

    channels = prompt(
        "Channel UUIDs to watch (comma-separated, empty = all joined channels)",
        default=get_env_value("BUZZ_CHANNELS") or "",
    )
    if channels:
        save_env_value("BUZZ_CHANNELS", channels.replace(" ", ""))

    home = prompt(
        "Home channel UUID for cron/notification delivery (optional)",
        default=get_env_value("BUZZ_HOME_CHANNEL") or "",
    )
    if home:
        save_env_value("BUZZ_HOME_CHANNEL", home.strip())

    print()
    print_info("🔒 Access control: restrict who can talk to the agent")
    allow_all = prompt_yes_no("Allow all community members to talk to the agent?", False)
    if allow_all:
        save_env_value("BUZZ_ALLOW_ALL_USERS", "true")
        save_env_value("BUZZ_ALLOWED_USERS", "")
        print_warning("⚠️  Open access — anyone in the community can command the agent.")
    else:
        save_env_value("BUZZ_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed users (comma-separated npubs or hex pubkeys, empty to deny everyone)",
            default=get_env_value("BUZZ_ALLOWED_USERS") or "",
        )
        save_env_value("BUZZ_ALLOWED_USERS", allowed.replace(" ", "") if allowed else "")

    print()
    print_success("Buzz configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="buzz",
        label="Buzz",
        adapter_factory=lambda cfg: BuzzAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"],
        install_hint="Requires the buzz CLI binary (https://github.com/block/buzz) on PATH or at BUZZ_CLI_PATH",
        setup_fn=interactive_setup,
        # Env-driven auto-configuration: seeds PlatformConfig.extra with
        # relay/channels/poll interval + home_channel so env-only setups show
        # up in gateway status without instantiating the adapter.
        env_enablement_fn=_env_enablement,
        # Bridge config.yaml buzz.extra -> BUZZ_* env vars so check_fn and the
        # env-driven connect path work for config.yaml-only setups (secret stays
        # in .env). Without this the check_fn gate skips Buzz at startup.
        apply_yaml_config_fn=_apply_yaml_config,
        # Cron home-channel delivery support (deliver=buzz).
        cron_deliver_env_var="BUZZ_HOME_CHANNEL",
        # Out-of-process cron delivery.  Without this hook, deliver=buzz
        # cron jobs fail with "No live adapter" when cron runs separately
        # from the gateway.
        standalone_sender_fn=_standalone_send,
        # Auth env vars for _is_user_authorized() integration
        allowed_users_env="BUZZ_ALLOWED_USERS",
        allow_all_env="BUZZ_ALLOW_ALL_USERS",
        # Display
        emoji="🐝",
        # Buzz identities are pubkeys, not phone numbers
        pii_safe=False,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are collaborating in a Buzz workspace (Block's Nostr-based "
            "human+agent platform). Markdown IS supported. Users address you "
            "by @-mentioning your name or npub in channels; direct messages "
            "reach you without a mention. Keep responses conversational."
        ),
    )

"""Regex-based secret redaction for logs and tool output.

Applies pattern matching to mask API keys, tokens, and credentials
before they reach log files, verbose output, or gateway logs.

Short tokens (< 18 chars) are fully masked. Longer tokens preserve
the first 6 and last 4 characters for debuggability.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# Sensitive query-string parameter names (case-insensitive exact match).
# Ported from nearai/ironclaw#2529 — catches tokens whose values don't match
# any known vendor prefix regex (e.g. opaque tokens, short OAuth codes).
_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "auth",
    "jwt",
    "session",
    "secret",
    "key",
    "code",           # OAuth authorization codes
    "signature",      # pre-signed URL signatures
    "x-amz-signature",
})

# Sensitive form-urlencoded / JSON body key names (case-insensitive exact match).
# Exact match, NOT substring — "token_count" and "session_id" must NOT match.
# Ported from nearai/ironclaw#2529.
_SENSITIVE_BODY_KEYS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "auth",
    "jwt",
    "secret",
    "private_key",
    "authorization",
    "key",
})

# Snapshot at import time so runtime env mutations (e.g. LLM-generated
# `export HERMES_REDACT_SECRETS=true`) cannot enable/disable redaction
# mid-session.  OFF by default — user must opt in via
# `security.redact_secrets: true` in config.yaml (bridged to this env var
# in hermes_cli/main.py and gateway/run.py) or `HERMES_REDACT_SECRETS=true`
# in ~/.hermes/.env.
_REDACT_ENABLED = os.getenv("HERMES_REDACT_SECRETS", "").lower() in ("1", "true", "yes", "on")

# Known API key prefixes -- match the prefix + contiguous token chars
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",      # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # Codex encrypted tokens
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",             # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",            # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",         # DigitalOcean OAuth
    r"am_[A-Za-z0-9_-]{10,}",           # AgentMail API key
    r"sk_[A-Za-z0-9_]{10,}",            # ElevenLabs TTS key (sk_ underscore, not sk- dash)
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily search API key
    r"exa_[A-Za-z0-9]{10,}",            # Exa search API key
    r"gsk_[A-Za-z0-9]{10,}",            # Groq Cloud API key
    r"syt_[A-Za-z0-9]{10,}",            # Matrix access token
    r"retaindb_[A-Za-z0-9]{10,}",       # RetainDB API key
    r"hsk-[A-Za-z0-9]{10,}",            # Hindsight API key
    r"mem0_[A-Za-z0-9]{10,}",           # Mem0 Platform API key
    r"brv_[A-Za-z0-9]{10,}",            # ByteRover API key
]

# ENV assignment patterns: KEY=value where KEY contains a secret-like name
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2",
)

# JSON field patterns: "apiKey": "value", "token": "value", etc.
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# Authorization headers
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*Bearer\s+)(\S+)",
    re.IGNORECASE,
)

# Bare `Bearer <token>` with no `Authorization:` prefix. Credentials reach
# agent context as prose far more often than as literal headers -- pasted curl
# snippets, "use Bearer <tok> to authenticate", API docs quoted into chat.
# Found by the Phase 9 / B5 canary suite: a bare bearer token matching no
# vendor prefix was surviving every matcher.
_BEARER_RE = re.compile(
    r"\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})",
    re.IGNORECASE,
)

# Telegram bot tokens: bot<digits>:<token> or <digits>:<token>,
# where token part is restricted to [-A-Za-z0-9_] and length >= 30
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})",
)

# Private key blocks: -----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# Database connection strings: protocol://user:PASSWORD@host
# Catches postgres, mysql, mongodb, redis, amqp URLs and redacts the password
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)

# JWT tokens: header.payload[.signature] — always start with "eyJ" (base64 for "{")
# Matches 1-part (header only), 2-part (header.payload), and full 3-part JWTs.
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}"           # Header (always starts with eyJ)
    r"(?:\.[A-Za-z0-9_=-]{4,}){0,2}"   # Optional payload and/or signature
)

# Discord user/role mentions: <@123456789012345678> or <@!123456789012345678>
# Snowflake IDs are 17-20 digit integers that resolve to specific Discord accounts.
_DISCORD_MENTION_RE = re.compile(r"<@!?(\d{17,20})>")

# E.164 phone numbers: +<country><number>, 7-15 digits
# Negative lookahead prevents matching hex strings or identifiers
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

# URLs containing query strings — matches `scheme://...?...[# or end]`.
# Used to scan text for URLs whose query params may contain secrets.
# Ported from nearai/ironclaw#2529.
_URL_WITH_QUERY_RE = re.compile(
    r"(https?|wss?|ftp)://"          # scheme
    r"([^\s/?#]+)"                    # authority (may include userinfo)
    r"([^\s?#]*)"                     # path
    r"\?([^\s#]+)"                    # query (required)
    r"(#\S*)?",                       # optional fragment
)

# URLs containing userinfo — `scheme://user:password@host` for ANY scheme
# (not just DB protocols already covered by _DB_CONNSTR_RE above).
# Catches things like `https://user:token@api.example.com/v1/foo`.
_URL_USERINFO_RE = re.compile(
    r"(https?|wss?|ftp)://([^/\s:@]+):([^/\s@]+)@",
)

# Form-urlencoded body detection: conservative — only applies when the entire
# text looks like a query string (k=v&k=v pattern with no newlines).
_FORM_BODY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*)+$"
)

# Compile known prefix patterns into one alternation
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def mask_secret(
    value: str,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """Mask a secret for display, preserving ``head`` and ``tail`` characters.

    Canonical helper for display-time redaction across Hermes — used by
    ``hermes config``, ``hermes status``, ``hermes dump``, and anywhere
    a secret needs to be shown truncated for debuggability while still
    keeping the bulk hidden.

    Args:
        value:       The secret to mask. ``None``/empty returns ``empty``.
        head:        Leading characters to preserve. Default 4.
        tail:        Trailing characters to preserve. Default 4.
        floor:       Values shorter than ``head + tail + floor_margin`` are
                     fully masked (returns ``placeholder``). Default 12 —
                     matches the existing config/status/dump convention.
        placeholder: Value returned for too-short inputs. Default ``"***"``.
        empty:       Value returned when ``value`` is falsy (None, ""). The
                     caller can override this to e.g. ``color("(not set)",
                     Colors.DIM)`` for user-facing display.

    Examples:
        >>> mask_secret("sk-proj-abcdef1234567890")
        'sk-p...7890'
        >>> mask_secret("short")                         # fully masked
        '***'
        >>> mask_secret("")                              # empty default
        ''
        >>> mask_secret("", empty="(not set)")           # empty override
        '(not set)'
        >>> mask_secret("long-token", head=6, tail=4, floor=18)
        '***'
    """
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_token(token: str) -> str:
    """Mask a log token — conservative 18-char floor, preserves 6 prefix / 4 suffix."""
    # Empty input: historically this returned "***" rather than "". Preserve.
    if not token:
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)


def _redact_query_string(query: str) -> str:
    """Redact sensitive parameter values in a URL query string.

    Handles `k=v&k=v` format. Sensitive keys (case-insensitive) have values
    replaced with `***`. Non-sensitive keys pass through unchanged.
    Empty or malformed pairs are preserved as-is.
    """
    if not query:
        return query
    parts = []
    for pair in query.split("&"):
        if "=" not in pair:
            parts.append(pair)
            continue
        key, _, value = pair.partition("=")
        if key.lower() in _SENSITIVE_QUERY_PARAMS:
            parts.append(f"{key}=***")
        else:
            parts.append(pair)
    return "&".join(parts)


def _redact_url_query_params(text: str) -> str:
    """Scan text for URLs with query strings and redact sensitive params.

    Catches opaque tokens that don't match vendor prefix regexes, e.g.
    `https://example.com/cb?code=ABC123&state=xyz` → `...?code=***&state=xyz`.
    """
    def _sub(m: re.Match) -> str:
        scheme = m.group(1)
        authority = m.group(2)
        path = m.group(3)
        query = _redact_query_string(m.group(4))
        fragment = m.group(5) or ""
        return f"{scheme}://{authority}{path}?{query}{fragment}"
    return _URL_WITH_QUERY_RE.sub(_sub, text)


def _redact_url_userinfo(text: str) -> str:
    """Strip `user:password@` from HTTP/WS/FTP URLs.

    DB protocols (postgres, mysql, mongodb, redis, amqp) are handled
    separately by `_DB_CONNSTR_RE`.
    """
    return _URL_USERINFO_RE.sub(
        lambda m: f"{m.group(1)}://{m.group(2)}:***@",
        text,
    )


def _redact_form_body(text: str) -> str:
    """Redact sensitive values in a form-urlencoded body.

    Only applies when the entire input looks like a pure form body
    (k=v&k=v with no newlines, no other text). Single-line non-form
    text passes through unchanged. This is a conservative pass — the
    `_redact_url_query_params` function handles embedded query strings.
    """
    if not text or "\n" in text or "&" not in text:
        return text
    # The body-body form check is strict: only trigger on clean k=v&k=v.
    if not _FORM_BODY_RE.match(text.strip()):
        return text
    return _redact_query_string(text.strip())


def redact_sensitive_text(text: str, *, force: bool = False, code_file: bool = False) -> str:
    """Apply all redaction patterns to a block of text.

    Safe to call on any string -- non-matching text passes through unchanged.
    Disabled by default — enable via security.redact_secrets: true in config.yaml.
    Set force=True for safety boundaries that must never return raw secrets
    regardless of the user's global logging redaction preference.

    Set code_file=True to skip the ENV-assignment and JSON-field regex
    patterns when the text is known to be source code (e.g. MAX_TOKENS=***
    constants, "apiKey": "test" fixtures). Prefix patterns, auth headers,
    private keys, DB connstrings, JWTs, and URL secrets are still redacted.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not (force or _REDACT_ENABLED):
        return text

    # Known prefixes (sk-, ghp_, etc.)
    text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    # ENV assignments: OPENAI_API_KEY=***  (skip for code files — false positives)
    if not code_file:
        def _redact_env(m):
            name, quote, value = m.group(1), m.group(2), m.group(3)
            return f"{name}={quote}{_mask_token(value)}{quote}"
        text = _ENV_ASSIGN_RE.sub(_redact_env, text)

        # JSON fields: "apiKey": "***"  (skip for code files — false positives)
        def _redact_json(m):
            key, value = m.group(1), m.group(2)
            return f'{key}: "{_mask_token(value)}"'
        text = _JSON_FIELD_RE.sub(_redact_json, text)

    # Authorization headers
    text = _AUTH_HEADER_RE.sub(
        lambda m: m.group(1) + _mask_token(m.group(2)),
        text,
    )

    # Bare `Bearer <token>`. Runs after the header pattern and skips tokens it
    # already masked, so the two are idempotent rather than double-masking a
    # value down to "***".
    def _redact_bearer(m):
        prefix, token = m.group(1), m.group(2)
        if "..." in token or token == "***":
            return m.group(0)
        return prefix + _mask_token(token)
    text = _BEARER_RE.sub(_redact_bearer, text)

    # Telegram bot tokens
    def _redact_telegram(m):
        prefix = m.group(1) or ""
        digits = m.group(2)
        return f"{prefix}{digits}:***"
    text = _TELEGRAM_RE.sub(_redact_telegram, text)

    # Private key blocks
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    # Database connection string passwords
    text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

    # JWT tokens (eyJ... — base64-encoded JSON headers)
    text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)

    # URL userinfo (http(s)://user:pass@host) — redact for non-DB schemes.
    # DB schemes are handled above by _DB_CONNSTR_RE.
    text = _redact_url_userinfo(text)

    # URL query params containing opaque tokens (?access_token=…&code=…)
    text = _redact_url_query_params(text)

    # Form-urlencoded bodies (only triggers on clean k=v&k=v inputs).
    text = _redact_form_body(text)

    # Discord user/role mentions (<@snowflake_id>)
    text = _DISCORD_MENTION_RE.sub(lambda m: f"<@{'!' if '!' in m.group(0) else ''}***>", text)

    # E.164 phone numbers (Signal, WhatsApp)
    def _redact_phone(m):
        phone = m.group(1)
        if len(phone) <= 8:
            return phone[:2] + "****" + phone[-2:]
        return phone[:4] + "****" + phone[-4:]
    text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    return text


# Placeholder written in place of a value whose *key* marks it sensitive.
# Distinct from _mask_token's head/tail masking: a key-matched value is
# replaced wholesale, because we cannot assume its shape is recognisable.
REDACTED_PLACEHOLDER = "[REDACTED]"

# Depth cap for redact_object. Deep enough for any real request body; a
# structure past this is either pathological or adversarial, and is replaced
# rather than walked.
_MAX_REDACT_DEPTH = 64

# Key names that are ambiguous: they mark a credential often enough to keep in
# the set, but are also ordinary parameter names in real tool schemas. Bare
# `key` is the case that bit us -- `browser_press` takes `key` holding "Enter",
# "Tab", "ArrowDown", and blanket redaction destroyed both the recorded tool
# call AND the tool schema's property definition.
#
# Every OTHER name in _SENSITIVE_BODY_KEYS stays unambiguous and is redacted
# wholesale with no shape test, so `{"api_key": "short"}` is unaffected.
_AMBIGUOUS_BODY_KEYS = frozenset({"key"})

# A value that is short and identifier-shaped is not credential-shaped:
# "Enter", "Tab", "Escape", "ArrowDown", "F5", "ctrl-c". Real opaque
# credentials are long and high-entropy and fail this test.
#
# RESIDUAL, accepted deliberately: a SHORT credential under a bare `key`
# (e.g. {"key": "hunter2"}) is now kept. Pinned by a test so the cost of this
# guard stays visible. Unambiguous key names have no such exemption.
_IDENTIFIER_SHAPED_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,15}$")


def _is_sensitive_key(key) -> bool:
    """Exact-match a mapping key against _SENSITIVE_BODY_KEYS.

    Case-insensitive, and ``-`` is normalised to ``_`` so ``api-key`` matches
    ``api_key``. Deliberately EXACT, not substring: ``token_count`` and
    ``session_id`` must not match.
    """
    if not isinstance(key, str):
        return False
    return key.strip().lower().replace("-", "_") in _SENSITIVE_BODY_KEYS


def _is_ambiguous_key(key) -> bool:
    """True for sensitive key names that are also ordinary parameter names."""
    if not isinstance(key, str):
        return False
    return key.strip().lower().replace("-", "_") in _AMBIGUOUS_BODY_KEYS


def _redact_keyed_value(value, *, ambiguous: bool, recurse):
    """Decide what to write for a value whose KEY marked it sensitive.

    Unambiguous key names (``api_key``, ``token``, ``password``, ...) are
    replaced wholesale with no inspection -- we cannot assume the value's shape
    is recognisable, and that is the whole point of key-based matching.

    Ambiguous names (bare ``key``) get a shape test first, because they are
    also real tool parameters:

    * short identifier-shaped string  -> kept  ("Enter", "Tab", "ArrowDown")
    * any other string                -> redacted wholesale
    * dict / list                     -> RECURSED, not replaced

    That last branch is the half a string-only guard would miss. A tool
    *schema* has ``{"key": {"type": "string", "description": ...}}`` -- a dict,
    not a string -- and replacing it wholesale erased the entire property
    definition from every session log and transcript.
    """
    if not ambiguous:
        return None if value is None else REDACTED_PLACEHOLDER

    if value is None:
        return None
    if isinstance(value, str):
        if _IDENTIFIER_SHAPED_RE.match(value):
            return value
        return REDACTED_PLACEHOLDER
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return recurse(value)
    # Non-string scalars (ints, bools) are not credentials.
    return value


def redact_object(
    obj,
    *,
    force: bool = True,
    code_file: bool = False,
    max_depth: int = _MAX_REDACT_DEPTH,
):
    """Recursively redact secrets from a nested dict/list/scalar structure.

    ``redact_sensitive_text`` only handles ``str``. Request bodies, session
    logs, and transcript entries are nested containers, so redacting them
    requires walking the structure and redacting each string leaf. This is
    that walker.

    Two mechanisms, both applied:

    1. **Key-based** — a mapping key matching :data:`_SENSITIVE_BODY_KEYS`
       has its value replaced with :data:`REDACTED_PLACEHOLDER` wholesale,
       without inspecting it. This catches opaque credentials that match no
       vendor prefix and no entropy heuristic.
    2. **Value-based** — every remaining string leaf goes through
       ``redact_sensitive_text``, reusing the existing curated matchers.

    ``force`` defaults to **True**, unlike ``redact_sensitive_text``. Callers
    of this function are persistence boundaries — data is about to be written
    to disk, where it stays. That must not depend on the operator's global
    logging preference, nor on whether the ``security.redact_secrets`` →
    ``HERMES_REDACT_SECRETS`` bridge in ``hermes_cli/main.py`` ran before
    ``agent.redact`` was imported. Same rationale as
    ``hermes_cli/debug.py``'s upload-bound ``force=True``.

    Values of unknown type are converted with ``str()`` and then redacted.
    This is deliberate: these structures are serialised with
    ``json.dumps(..., default=str)``, so an object whose ``__repr__`` carries
    a secret would otherwise be stringified *after* redaction and land in the
    file unredacted.

    Containers are copied — the input is never mutated in place, so the
    caller's live in-memory conversation state is unaffected.
    """
    return _redact_obj(
        obj,
        force=force,
        code_file=code_file,
        depth=0,
        max_depth=max_depth,
        seen=set(),
    )


def _redact_obj(obj, *, force, code_file, depth, max_depth, seen):
    if depth > max_depth:
        return "[REDACTED: max depth exceeded]"

    if obj is None or isinstance(obj, (bool, int, float)):
        return obj

    if isinstance(obj, str):
        return redact_sensitive_text(obj, force=force, code_file=code_file)

    if isinstance(obj, (bytes, bytearray)):
        # Not safely pattern-matchable and not JSON-native. Preserve length
        # only — it is the debugging signal that matters here.
        return f"[REDACTED BYTES len={len(obj)}]"

    # Path-based cycle detection: a node is "seen" only while it is on the
    # current recursion path, so a shared (DAG) child is still walked once
    # per parent, while a true cycle terminates.
    marker = id(obj)

    if isinstance(obj, dict):
        if marker in seen:
            return "[REDACTED: circular reference]"
        seen.add(marker)
        try:
            def _recurse(v):
                return _redact_obj(
                    v,
                    force=force,
                    code_file=code_file,
                    depth=depth + 1,
                    max_depth=max_depth,
                    seen=seen,
                )

            out = {}
            for key, value in obj.items():
                if _is_sensitive_key(key):
                    out[key] = _redact_keyed_value(
                        value,
                        ambiguous=_is_ambiguous_key(key),
                        recurse=_recurse,
                    )
                else:
                    out[key] = _recurse(value)
            return out
        finally:
            seen.discard(marker)

    if isinstance(obj, (list, tuple, set, frozenset)):
        if marker in seen:
            return "[REDACTED: circular reference]"
        seen.add(marker)
        try:
            items = [
                _redact_obj(
                    item,
                    force=force,
                    code_file=code_file,
                    depth=depth + 1,
                    max_depth=max_depth,
                    seen=seen,
                )
                for item in obj
            ]
            # Sets are not JSON-serialisable; every caller here serialises to
            # JSON, so normalise them to lists rather than reconstructing.
            if isinstance(obj, tuple):
                return tuple(items)
            return items
        finally:
            seen.discard(marker)

    # Unknown type — mirror json.dumps(default=str) and redact the result.
    try:
        return redact_sensitive_text(str(obj), force=force, code_file=code_file)
    except Exception:  # pragma: no cover — a __str__ that raises
        return f"[REDACTED UNSERIALISABLE {type(obj).__name__}]"


class RedactingFormatter(logging.Formatter):
    """Log formatter that redacts secrets from all log messages."""

    def __init__(self, fmt=None, datefmt=None, style='%', **kwargs):
        super().__init__(fmt, datefmt, style, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_sensitive_text(original)

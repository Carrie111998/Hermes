"""Regex-based secret redaction for logs and tool output.

Applies pattern matching to mask API keys, tokens, and credentials
before they reach log files, verbose output, or gateway logs.

Short tokens (< 18 chars) are fully masked. Longer tokens preserve
the first 6 and last 4 characters for debuggability.
"""

import json
import logging
import os
import re
import shlex

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
# `export HERMES_REDACT_SECRETS=false`) cannot disable redaction
# mid-session.  ON by default — secure default per issue #17691. Users who
# need raw credential values in tool output (e.g. working on the redactor
# itself) can opt out via `security.redact_secrets: false` in config.yaml
# (bridged to this env var in hermes_cli/main.py, gateway/run.py, and
# cli.py) or `HERMES_REDACT_SECRETS=false` in ~/.hermes/.env. An opt-out
# warning is logged at gateway and CLI startup so operators see the
# downgrade — see `_log_redaction_status()` in gateway/run.py and cli.py.
_REDACT_ENABLED = os.getenv("HERMES_REDACT_SECRETS", "true").lower() in {"1", "true", "yes", "on"}

# Known API key prefixes -- match the prefix + contiguous token chars
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xapp-\d+-[A-Za-z0-9-]{10,}",      # Slack app-Level token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack bot/app/user tokens
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
    r"xai-[A-Za-z0-9]{30,}",            # xAI (Grok) API key
    r"ntn_[A-Za-z0-9]{10,}",            # Notion internal integration token
    r"fw-[A-Za-z0-9]{30,}",             # Fireworks AI API key
    r"fw_[A-Za-z0-9]{30,}",             # Fireworks AI API key
    r"fpk_[A-Za-z0-9]{30,}",            # Fireworks AI project key
]

# ENV assignment patterns: KEY=value where KEY contains a secret-like name.
# Uppercase keys tolerate spaces around "=" (e.g. ``FOO_SECRET = bar``) because
# an all-caps key is almost never prose/code.
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2",
)

# Lowercase / dotted / hyphenated config keys from config files
# (application.properties, .env, YAML-ish dumps): ``spring.datasource.password=secret``,
# ``app.api.key=xyz``, ``password=secret``. The uppercase _ENV_ASSIGN_RE above
# never matched these, so config-file passwords leaked verbatim (issue #16413).
#
# These run only in a config-file context, NOT in prose, code, or URLs — three
# carve-outs preserved from the original design (#4367 + the documented
# web-URL passthrough below):
#   1. The value is bounded by ``[^\s&]`` (stops at whitespace AND ``&``) so
#      form-urlencoded bodies are handled pair-by-pair (by _redact_form_body),
#      not greedily swallowed.
#   2. _CFG_DOTTED_RE only matches when the key is NAMESPACED (contains a dot),
#      which is unambiguously a config key — never a prose word.
#   3. _CFG_ANCHORED_RE matches a bare secret-word key only at line start
#      (optionally after ``export``), so conversational ``I have password=foo``
#      mid-sentence is left alone.
# The colon-form URL guard (skip when ``://`` present) lives at the call site.
_SECRET_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential|auth)"
_CFG_VALUE = r"(['\"]?)([^\s&]+?)\2(?=[\s&]|$)"

# Programmatic env lookups (``os.getenv(...)``, ``os.environ[...]``,
# ``os.environ.get(...)``, ``process.env.X``, ``$ENV{X}``) reference variable
# *names*, not secret values. When one appears as the VALUE of a KEY=... match
# it's a code snippet, not a leaked secret — skip redaction (issue #2852).
_ENV_LOOKUP_VALUE_RE = re.compile(
    r"^(?:os\.(?:getenv|environ)|process\.env|\$ENV\{)"
)
# Namespaced (dotted) key: the secret word may sit anywhere in a dotted path.
_CFG_DOTTED_RE = re.compile(
    rf"((?:[A-Za-z0-9_\-]+\.)+[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*"
    rf"|[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*\.[A-Za-z0-9_.\-]+)"
    rf"={_CFG_VALUE}",
    re.IGNORECASE,
)
# Line-anchored bare key: ``password=…`` / ``export api_key=…`` at start of line.
_CFG_ANCHORED_RE = re.compile(
    rf"(^[ \t]*(?:export[ \t]+)?[A-Za-z0-9_\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_\-]*)={_CFG_VALUE}",
    re.IGNORECASE | re.MULTILINE,
)

# Unquoted YAML / colon config (e.g. ``password: secret``,
# ``spring.datasource.password: hunter2``). The secret keyword must be part of
# the KEY (anchored to the start of the line/indent), and the value is a single
# whitespace-free token — so prose like ``note: secret meeting`` (keyword in the
# value) and ``error: token expired`` are left alone. Bare ``auth`` is excluded
# from the key set so ``Authorization:`` / ``author:`` don't match (the former
# is masked by _AUTH_HEADER_RE); ``auth_token``/``auth-token`` still match via
# the ``token`` keyword. Quoted values defer to _JSON_FIELD_RE via the lookahead.
_YAML_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential)"
_YAML_ASSIGN_RE = re.compile(
    rf"(^[ \t]*[A-Za-z0-9_.\-]*{_YAML_CFG_NAMES}[A-Za-z0-9_.\-]*)(:[ \t]*)(?!['\"])([^\s&]+)",
    re.IGNORECASE | re.MULTILINE,
)

# JSON field patterns: "apiKey": "value", "token": "value", etc.
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# Authorization headers — any scheme (Bearer, Basic, Token, Digest, …) plus the
# bare-credential form, and Proxy-Authorization. The credential token is masked
# while the header name and scheme word are preserved for debuggability. The
# previous rule only matched ``Bearer``, so ``Basic <base64 user:pass>`` and
# ``token <pat>`` leaked verbatim into logs/transcripts.
#
# The credential class excludes quote characters (``"`` / ``'``): a token sitting
# flush against a closing quote (``"Authorization: Bearer sk-..."``) must not pull
# that quote into the match, or masking turns value corruption into *syntax*
# corruption — the closing quote vanishes and the command/string no longer parses
# (unterminated quote → shell EOF / Python SyntaxError). Real credentials never
# contain ``"`` or ``'``, so excluding them is safe. See #43083.
_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)([A-Za-z][\w.+-]*\s+)?([^\s\"']+)",
    re.IGNORECASE,
)

# API-key style auth headers carrying a single opaque value (no scheme word).
# Anthropic and many providers authenticate with ``x-api-key``; values without
# a known vendor prefix (custom/local backends) would otherwise leak when a
# request or curl command is logged or echoed into tool output / transcripts.
_SECRET_HEADER_NAMES = (
    r"(?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)"
)
_SECRET_HEADER_RE = re.compile(
    rf"({_SECRET_HEADER_NAMES}\s*:\s*)(\S+)",
    re.IGNORECASE,
)

# Cookie headers need structure-aware handling rather than the single-value
# header rule above.  The optional quote/backreference pair supports serialized
# header maps (``{"Cookie": "a=b"}``) while the unquoted form supports normal
# wire headers and shell strings (``-H 'Cookie: a=b'``).  The value span itself
# is located by _redact_cookie_headers so surrounding shell/JSON quotes are not
# consumed or damaged.
_cookie_header_re = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"(?P<name_quote>[\"']?)(?P<name>Set-Cookie|Cookie)(?P=name_quote)"
    r"(?P<separator>[ \t]*:[ \t]*)",
    re.IGNORECASE,
)

# Embedded JSON is attacker/model-controlled output, so recursive decoding is
# allowed only after a bounded lexical pass has proved the candidate shallow
# and complete. Candidates beyond any bound are never handed to ``json``.
_COOKIE_JSON_MAX_DEPTH = 64
_COOKIE_JSON_MAX_CANDIDATE_CHARS = 256 * 1024
_COOKIE_JSON_MAX_CANDIDATES = 256
_COOKIE_JSON_MAX_EMBEDDED_DEPTH = 8
_COOKIE_JSON_STRING_PROBE_CHARS = 64
_COOKIE_JSON_REDACTED_SENTINEL = "[REDACTED COOKIE JSON]"


class _JSONObjectPairs(list):
    """Distinguish raw JSON object pairs from ordinary JSON arrays."""


class _CookieJSONAmbiguous(Exception):
    """A Cookie-bearing JSON candidate cannot be reserialized safely."""


class _EmbeddedJSONEvidenceProbe:
    """Bounded lexical evidence for a JSON container encoded inside a string."""

    _OUTSIDE_CHARS = frozenset(
        " \t\r\n{}[],:-+0123456789.eEtruefalsn"
    )
    _ESCAPES = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def __init__(self):
        self.state = "unknown"
        self.stack: list[str] = []
        self.depth = 0
        self.untracked_depth = False
        self.in_string = False
        self.escape_pending = False
        self.unicode_digits: str | None = None
        self.string_probe: list[str] = []
        self.string_overflow = False
        self.cookie_evidence = False

    def _append_string_char(self, ch: str) -> None:
        if len(self.string_probe) < _COOKIE_JSON_STRING_PROBE_CHARS:
            self.string_probe.append(ch)
        else:
            self.string_overflow = True

    def _close_string(self) -> None:
        if not self.string_overflow and (
            "".join(self.string_probe).lower() in {"cookie", "set-cookie"}
        ):
            self.cookie_evidence = True
        self.in_string = False
        self.escape_pending = False
        self.unicode_digits = None
        self.string_probe = []
        self.string_overflow = False

    def feed(self, ch: str) -> None:
        if self.state == "invalid":
            return
        if self.in_string:
            if self.unicode_digits is not None:
                if ch not in "0123456789abcdefABCDEF":
                    self.state = "invalid"
                    return
                self.unicode_digits += ch
                if len(self.unicode_digits) == 4:
                    self._append_string_char(chr(int(self.unicode_digits, 16)))
                    self.unicode_digits = None
                return
            if self.escape_pending:
                self.escape_pending = False
                if ch == "u":
                    self.unicode_digits = ""
                    return
                decoded = self._ESCAPES.get(ch)
                if decoded is None:
                    self.state = "invalid"
                    return
                self._append_string_char(decoded)
                return
            if ch == "\\":
                self.escape_pending = True
                return
            if ch == '"':
                self._close_string()
                return
            if ord(ch) < 0x20:
                self.state = "invalid"
                return
            self._append_string_char(ch)
            return

        if self.state == "complete":
            if ch not in " \t\r\n":
                self.state = "invalid"
            return
        if self.state == "unknown":
            if ch in " \t\r\n":
                return
            if ch not in "[{":
                self.state = "invalid"
                return
            self.state = "active"
            self.stack = [ch]
            self.depth = 1
            return

        if ch == '"':
            self.in_string = True
            self.string_probe = []
            self.string_overflow = False
        elif ch in "[{":
            self.depth += 1
            if self.untracked_depth:
                return
            if self.depth > _COOKIE_JSON_MAX_DEPTH:
                self.untracked_depth = True
            else:
                self.stack.append(ch)
        elif ch in "]}":
            if self.untracked_depth:
                self.depth -= 1
                if self.depth <= _COOKIE_JSON_MAX_DEPTH:
                    self.untracked_depth = False
            elif not self.stack or (ch == "]" and self.stack[-1] != "[") or (
                ch == "}" and self.stack[-1] != "{"
            ):
                self.state = "invalid"
                return
            else:
                self.stack.pop()
                self.depth -= 1
            if self.depth == 0:
                self.state = "complete"
        elif ch not in self._OUTSIDE_CHARS:
            # Reject ordinary prose before treating a quoted alias as embedded
            # JSON evidence. The real decoder still decides validity for every
            # candidate within the normal size/depth bounds.
            self.state = "invalid"

    def has_cookie_evidence(self) -> bool:
        return (
            self.state == "complete"
            and not self.in_string
            and self.cookie_evidence
        )

# Discord bot/user tokens are otherwise bare: unlike most credentials they do
# not carry a vendor prefix.  Their three URL-safe-base64-ish segments are very
# distinctive.  Keep the first segment deliberately narrow (24-26 chars), the
# range produced by Discord snowflake IDs used by real bots, and require the
# fixed six-character middle segment plus a substantial final segment.  These
# constraints avoid treating ordinary dotted prose, semantic versions, and
# dotted hashes as credentials.  Legacy user tokens beginning ``mfa.`` use a
# separate distinctive prefix and a long URL-safe body.
_discord_credential_re = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"[A-Za-z0-9_-]{24,26}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,110}"
    r"|mfa\.[A-Za-z0-9_-]{40,}"
    r")(?![A-Za-z0-9_-])"
)
_discord_file_sentinel = "«redacted:discord-token»"

# Telegram bot tokens: bot<digits>:<token> or <digits>:<token>,
# where token part is restricted to [-A-Za-z0-9_] and length >= 30
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})",
)

# Private-key blocks are redacted only when a line-anchored, dashed BEGIN-ish
# private-key header is followed by substantial PEM/base64 material.  The
# private-key word may be severely partial (``P`` through ``PRIVATE``), as may
# the trailing ``KEY`` word, because a truncated stream frame is still a secret
# boundary.  Requiring material avoids masking marker-only documentation.  The
# match remains line-bounded and stops after the last material/END line so
# ordinary prose following an unterminated block survives.
_PRIVATE_KEY_MATERIAL_RE = re.compile(
    r"^[ \t]*-{3,}[ \t]*BEGIN"
    r"(?:[ \t]+[A-Z0-9-]+)*[ \t]+P(?:R(?:I(?:V(?:A(?:T(?:E)?)?)?)?)?)?"
    r"(?:[ \t]+K(?:E(?:Y)?)?)?[ \t]*-*[ \t]*\r?\n"
    r"(?:(?:[ \t]*(?:PROC-TYPE|DEK-INFO)[ \t]*:[^\r\n]*|[ \t]*)\r?\n)*"
    r"[ \t]*[A-Z0-9+/]{24,}={0,2}[ \t]*(?=\r?$)"
    r"(?:\r?\n[ \t]*[A-Z0-9+/]{4,}={0,2}[ \t]*(?=\r?$))*"
    r"(?:\r?\n[ \t]*-{3,}[ \t]*END"
    r"(?:[ \t]+[A-Z0-9-]+)*[ \t]+PRIVATE(?:[ \t]+KEY)?[ \t]*-*[ \t]*(?=\r?$))?",
    re.IGNORECASE | re.MULTILINE,
)

# Database connection strings: protocol://user:PASSWORD@host
# Catches postgres, mysql, mongodb, redis, amqp URLs and redacts the password.
# The userinfo and password groups forbid whitespace ([^:\s]+ / [^@\s]+) so the
# match can never span a line break. A real DSN password never contains
# whitespace; without this bound the greedy [^@]+ would scan past the end of a
# code line to the next stray "@" (e.g. a Python decorator), swallowing
# intervening lines and corrupting tool OUTPUT for any source containing a
# postgresql:// f-string template. See issue #33801.
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)

# Bare-token credential in a web/transport URL: ``scheme://TOKEN@host``.
# This is the ``git remote set-url origin https://PASSWORD@github.com/...``
# shape from issue #6396 — a single opaque credential in the userinfo position
# with NO ``user:pass`` colon. It is unambiguously a secret: legitimate
# round-trip URLs (OAuth callbacks, magic links, pre-signed shares — see the
# "Web-URL redaction is intentionally OFF" note in redact_sensitive_text) carry
# their tokens in the QUERY STRING, never in bare userinfo. The colon form
# ``user:pass@`` is deliberately left to pass through (commit "pass web URLs
# through unchanged", #34029) and is NOT matched here — the token class forbids
# ``:``. DB schemes are handled by _DB_CONNSTR_RE above and excluded here.
#
# Guards against false positives:
#   - 8+ char floor skips short usernames (git, admin, root, deploy, ubuntu).
#   - The token class ``[^\s:@/]`` cannot cross ``/``, so an ``@`` sitting in a
#     path or query (e.g. ``?q=user@example.com``) is never treated as userinfo.
_URL_BARE_TOKEN_RE = re.compile(
    r"((?:https?|wss?|git|ssh|ftp|ftps|sftp)://)"  # scheme
    r"([^\s:@/]{8,})"                               # bare token (no colon/slash/@), 8+ chars
    r"(@[^\s]+)",                                   # @host...
    re.IGNORECASE,
)

# JWT tokens: header.payload[.signature] — always start with "eyJ" (base64 for "{")
# Matches 1-part (header only), 2-part (header.payload), and full 3-part JWTs.
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}"           # Header (always starts with eyJ)
    r"(?:\.[A-Za-z0-9_=-]{4,}){0,2}"   # Optional payload and/or signature
)

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

# HTTP access logs often use a relative request target rather than a full URL:
# `"POST /webhook?password=... HTTP/1.1"`. The full-URL redactor above only
# sees strings containing `://`, so handle request-target query strings too.
_HTTP_REQUEST_TARGET_QUERY_RE = re.compile(
    r"\b((?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+[^ \t\r\n\"']*?)"
    r"\?([^ \t\r\n\"']+)",
    re.IGNORECASE,
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


def redact_cdp_url(value: object) -> str:
    """Mask secrets in a CDP/browser endpoint URL before it is logged.

    The global ``redact_sensitive_text`` deliberately passes web-URL query
    params and ``user:pass@`` userinfo through unmasked (OAuth callbacks,
    magic-link / pre-signed URLs the agent is meant to follow -- see the
    web-URL note above). CDP discovery endpoints are NOT such a workflow:
    their query-string tokens and userinfo passwords are pure credentials
    that must never reach the logs. So for CDP URLs we opt INTO the two URL
    redactors that the global pass leaves off.

    This is the single source of truth for redacting a CDP URL that is passed
    *directly* to a log or error message. Callers that instead need to redact an
    exception whose text embeds the URL (e.g. a ``websockets`` connect error)
    should route that through their own error-text helper, which delegates here
    -- see ``tools.browser_supervisor._redact_cdp_error_text``.
    """
    text = redact_sensitive_text("" if value is None else str(value))
    if not text:
        return text
    text = _redact_url_query_params(text)
    text = _redact_url_userinfo(text)
    return text


def _redact_http_request_target_query_params(text: str) -> str:
    """Redact sensitive query params in HTTP access-log request targets."""
    def _sub(m: re.Match) -> str:
        prefix = m.group(1)
        query = _redact_query_string(m.group(2))
        return f"{prefix}?{query}"
    return _HTTP_REQUEST_TARGET_QUERY_RE.sub(_sub, text)


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


def _find_unescaped_quote(text: str, start: int, end: int, quote: str) -> int | None:
    """Return the next unescaped ``quote`` index within ``[start, end)``."""
    i = start
    while i < end:
        if text[i] == "\\":
            # In JSON/shell-rendered text an escaped quote is part of the
            # header value, not the surrounding string's terminator.
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return None


def _split_cookie_value(value: str) -> list[str]:
    """Split a Cookie/Set-Cookie value on unquoted semicolons, preserving them."""
    parts: list[str] = []
    start = 0
    quote: str | None = None
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            escaped = value[i + 1]
            # JSON-rendered cookie values use escaped quotes. Treat those quote
            # characters as delimiters for semicolon scanning while preserving
            # both bytes in the returned segments.
            if escaped in {"\"", "'"}:
                if quote is None:
                    quote = escaped
                elif quote == escaped:
                    quote = None
            i += 2
            continue
        if ch in {"\"", "'"}:
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
        elif ch == ";" and quote is None:
            parts.extend((value[start:i], ";"))
            start = i + 1
        i += 1
    parts.append(value[start:])
    return parts


def _mask_cookie_assignment(segment: str) -> str:
    """Fully mask one ``name=value`` cookie pair without changing its syntax."""
    if "=" not in segment:
        # A request Cookie field without ``=`` is malformed, but may still be
        # an opaque reusable credential. Fail closed while preserving only
        # surrounding whitespace. Set-Cookie attributes never reach this path:
        # that caller masks only the leading cookie pair in each field-value.
        leading = segment[: len(segment) - len(segment.lstrip())]
        trailing = segment[len(segment.rstrip()):]
        return f"{leading}***{trailing}" if segment.strip() else segment
    name, equals, raw_value = segment.partition("=")

    leading_len = len(raw_value) - len(raw_value.lstrip())
    trailing_len = len(raw_value) - len(raw_value.rstrip())
    if leading_len == len(raw_value):
        trailing_len = 0
    leading = raw_value[:leading_len]
    # Avoid overlapping slices when the original value is whitespace-only.
    core_end = len(raw_value) - trailing_len if trailing_len else len(raw_value)
    core = raw_value[leading_len:core_end]
    trailing = raw_value[core_end:]

    # Preserve quotes around individual cookie values, including the escaped
    # quotes found inside serialized JSON strings.
    if len(core) >= 2 and core[0] == core[-1] and core[0] in {"\"", "'"}:
        masked = f"{core[0]}***{core[-1]}"
    elif (
        len(core) >= 4
        and core[:2] in {"\\\"", "\\'"}
        and core[-2:] == core[:2]
    ):
        masked = f"{core[:2]}***{core[-2:]}"
    else:
        masked = "***"
    return f"{name}{equals}{leading}{masked}{trailing}"


def _split_combined_set_cookie(value: str) -> list[str]:
    """Split comma-combined Set-Cookie values without splitting Expires dates."""
    parts: list[str] = []
    start = 0
    quote: str | None = None
    i = 0
    cookie_name_re = re.compile(r"\s*[!#$%&'*+.^_`|~0-9A-Za-z-]+\s*=")
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            i += 2
            continue
        if ch in {"\"", "'"}:
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
        elif ch == "," and quote is None and cookie_name_re.match(value, i + 1):
            parts.extend((value[start:i], ","))
            start = i + 1
        i += 1
    parts.append(value[start:])
    return parts


def _mask_cookie_header_value(value: str, *, set_cookie: bool) -> str:
    """Mask request Cookie pairs or each Set-Cookie leading cookie pair."""
    if set_cookie:
        combined = _split_combined_set_cookie(value)
        for combined_idx in range(0, len(combined), 2):
            fields = _split_cookie_value(combined[combined_idx])
            if fields:
                fields[0] = _mask_cookie_assignment(fields[0])
            combined[combined_idx] = "".join(fields)
        return "".join(combined)

    parts = _split_cookie_value(value)
    # Request Cookie headers carry multiple cookies. Every name=value segment
    # is sensitive; delimiters, whitespace, names, and quotes remain unchanged.
    for i in range(0, len(parts), 2):
        parts[i] = _mask_cookie_assignment(parts[i])
    return "".join(parts)


def _bounded_json_container_status(text: str) -> tuple[bool, bool]:
    """Return ``(valid_container, exceeded_bound)`` for decoded JSON text."""
    if not text:
        return False, False
    if len(text) > _COOKIE_JSON_MAX_CANDIDATE_CHARS:
        return False, True
    start = 0
    end = len(text)
    while start < end and text[start] in " \t\r\n":
        start += 1
    while end > start and text[end - 1] in " \t\r\n":
        end -= 1
    if start == end or text[start] not in "[{":
        return False, False

    stack: list[str] = []
    in_string = False
    i = start
    while i < end:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
            if len(stack) > _COOKIE_JSON_MAX_DEPTH:
                return False, True
        elif ch in "]}":
            if not stack or (ch == "]" and stack[-1] != "[") or (
                ch == "}" and stack[-1] != "{"
            ):
                return False, False
            stack.pop()
            if not stack and i != end - 1:
                return False, False
        i += 1
    return (not in_string and not stack), False


def _redact_cookie_json_document(
    text: str,
    *,
    cookie_bearing: bool = False,
) -> str | None:
    """Redact one bounded JSON candidate, failing closed on ambiguity/errors."""
    try:
        value = json.loads(text, object_pairs_hook=_JSONObjectPairs)
    except Exception:
        # The caller's lexical pass identifies Cookie-bearing strings without
        # invoking the recursive decoder. A decoder failure must not leak a
        # pair/list representation or escape a final streaming boundary.
        return _COOKIE_JSON_REDACTED_SENTINEL if cookie_bearing else None

    change_count = 0
    embedded_count = 0
    embedded_chars = 0

    def mask_header_item(item, *, set_cookie: bool):
        nonlocal change_count
        change_count += 1
        if isinstance(item, str):
            return _mask_cookie_header_value(item, set_cookie=set_cookie)
        if isinstance(item, list) and not isinstance(item, _JSONObjectPairs):
            return [
                _mask_cookie_header_value(entry, set_cookie=set_cookie)
                if isinstance(entry, str)
                else "***"
                for entry in item
            ]
        return "***"

    def walk(node, *, embedded_depth: int = 0):
        nonlocal change_count, embedded_chars, embedded_count
        if isinstance(node, _JSONObjectPairs):
            pairs = list(node)
            exact_key_counts: dict[str, int] = {}
            for key, _item in pairs:
                exact_key_counts[key] = exact_key_counts.get(key, 0) + 1

            name_pairs = [
                (key, item)
                for key, item in pairs
                if key.lower() in {"name", "header", "header_name"}
            ]
            value_keys = {
                key
                for key, _item in pairs
                if key.lower() in {"value", "values"}
            }
            header_names = {
                item.lower()
                for _key, item in name_pairs
                if isinstance(item, str)
            }
            structured_cookie = header_names & {"cookie", "set-cookie"}
            direct_cookie_keys = {
                key for key, _item in pairs if key.lower() in {"cookie", "set-cookie"}
            }

            # Raw duplicate pairs cannot be represented by a normal dict. Fail
            # this top-level candidate closed whenever collapsing one could hide
            # an earlier Cookie alias or value. Case-distinct aliases remain
            # representable and continue to be masked independently.
            ambiguous_keys = {
                key
                for key, count in exact_key_counts.items()
                if count > 1
                and key.lower()
                in {"name", "header", "header_name", "value", "values", "cookie", "set-cookie"}
            }
            conflicting_names = bool(structured_cookie) and len(header_names) > 1
            if direct_cookie_keys & ambiguous_keys or (
                structured_cookie and (ambiguous_keys or conflicting_names)
            ):
                raise _CookieJSONAmbiguous

            if value_keys and structured_cookie:
                # Request-Cookie semantics are stricter than Set-Cookie, so a
                # representable Cookie/Set-Cookie combination masks every pair.
                set_cookie = "cookie" not in structured_cookie
                out = {}
                for key, item in pairs:
                    key_lower = key.lower()
                    if key in value_keys:
                        item = mask_header_item(item, set_cookie=set_cookie)
                    elif key_lower in {"cookie", "set-cookie"}:
                        item = mask_header_item(
                            item,
                            set_cookie=key_lower == "set-cookie",
                        )
                    else:
                        item = walk(item, embedded_depth=embedded_depth)
                    out[key] = item
                return out

            out = {}
            for key, item in pairs:
                key_lower = key.lower()
                if key_lower in {"cookie", "set-cookie"}:
                    item = mask_header_item(
                        item,
                        set_cookie=key_lower == "set-cookie",
                    )
                else:
                    item = walk(item, embedded_depth=embedded_depth)
                out[key] = item
            return out
        if isinstance(node, list):
            if len(node) >= 2 and isinstance(node[0], str):
                header_name = node[0].lower()
                if header_name in {"cookie", "set-cookie"}:
                    out = list(node)
                    out[1] = mask_header_item(
                        node[1],
                        set_cookie=header_name == "set-cookie",
                    )
                    for index in range(2, len(out)):
                        out[index] = walk(
                            out[index],
                            embedded_depth=embedded_depth,
                        )
                    return out
            return [walk(item, embedded_depth=embedded_depth) for item in node]
        if isinstance(node, str):
            # Recurse only into an entire decoded, bounded JSON object/array.
            # The aggregate counters prevent nested encoded strings from
            # multiplying decoder work beyond a fixed linear budget.
            stripped = node.strip()
            if stripped[:1] in {"[", "{"}:
                if embedded_depth >= _COOKIE_JSON_MAX_EMBEDDED_DEPTH:
                    raise _CookieJSONAmbiguous
                bounded, exceeded_bound = _bounded_json_container_status(node)
                if exceeded_bound:
                    raise _CookieJSONAmbiguous
                if not bounded:
                    redacted = _redact_plain_cookie_headers(node)
                    if redacted != node:
                        change_count += 1
                    return redacted
                embedded_count += 1
                embedded_chars += len(node)
                if (
                    embedded_count > _COOKIE_JSON_MAX_CANDIDATES
                    or embedded_chars > _COOKIE_JSON_MAX_CANDIDATE_CHARS
                ):
                    raise _CookieJSONAmbiguous
                try:
                    embedded = json.loads(
                        node,
                        object_pairs_hook=_JSONObjectPairs,
                    )
                except Exception:
                    redacted = _redact_plain_cookie_headers(node)
                    if redacted != node:
                        change_count += 1
                    return redacted
                before = change_count
                embedded = walk(
                    embedded,
                    embedded_depth=embedded_depth + 1,
                )
                if change_count != before:
                    return json.dumps(embedded, ensure_ascii=False)

            # A JSON field may itself contain a literal wire header. Keep JSON
            # valid by redacting the decoded value before re-encoding it.
            redacted = _redact_plain_cookie_headers(node)
            if redacted != node:
                change_count += 1
            return redacted
        return node

    try:
        redacted = walk(value)
    except _CookieJSONAmbiguous:
        return _COOKIE_JSON_REDACTED_SENTINEL
    if not change_count:
        return None
    return json.dumps(redacted, ensure_ascii=False)


def _plain_cookie_header_records(
    text: str,
) -> list[tuple[re.Match, int, int]]:
    """Locate plain Cookie values in one monotonic pass over matches/lines."""
    records: list[tuple[re.Match, int, int]] = []
    covered_until = 0
    line_start = 0
    physical_end = text.find("\n")
    logical_end: int | None = None
    text_len = len(text)

    for match in _cookie_header_re.finditer(text):
        if match.start() < covered_until:
            continue

        while physical_end >= 0 and match.start() > physical_end:
            line_start = physical_end + 1
            physical_end = text.find("\n", line_start)
            logical_end = None

        if logical_end is None:
            folded_end = physical_end
            logical_end = text_len if folded_end < 0 else folded_end
            # Compute a folded logical line once, not once per header match.
            while (
                folded_end >= 0
                and folded_end + 1 < text_len
                and text[folded_end + 1] in {" ", "\t"}
            ):
                folded_end = text.find("\n", folded_end + 1)
                logical_end = text_len if folded_end < 0 else folded_end
            if logical_end > match.end() and text[logical_end - 1] == "\r":
                logical_end -= 1

        value_start = match.end()
        value_end = logical_end

        # JSON/dict header map: {"Cookie": "a=b"}. The name quotes are part
        # of the regex; leave the value quotes outside the replacement span.
        if value_start < value_end and text[value_start] in {"\"", "'"}:
            quote = text[value_start]
            closing = _find_unescaped_quote(text, value_start + 1, value_end, quote)
            if closing is not None:
                value_start += 1
                value_end = closing
        # Shell or JSON string containing a literal header:
        # curl -H 'Cookie: a=b' / "Cookie: a=b".
        elif match.start() > line_start and text[match.start() - 1] in {"\"", "'"}:
            quote = text[match.start() - 1]
            closing = _find_unescaped_quote(text, value_start, value_end, quote)
            if closing is not None:
                value_end = closing

        records.append((match, value_start, value_end))
        covered_until = value_end

    return records


def _redact_plain_cookie_headers(
    text: str,
    records: list[tuple[re.Match, int, int]] | None = None,
) -> str:
    """Redact Cookie/Set-Cookie header syntax outside structured JSON."""
    if records is None:
        records = _plain_cookie_header_records(text)
    if not records:
        return text

    chunks: list[str] = []
    cursor = 0
    for match, value_start, value_end in records:
        chunks.append(text[cursor:value_start])
        chunks.append(
            _mask_cookie_header_value(
                text[value_start:value_end],
                set_cookie=match.group("name").lower() == "set-cookie",
            )
        )
        cursor = value_end
    chunks.append(text[cursor:])
    return "".join(chunks)


def _plain_cookie_header_value_spans(
    records: list[tuple[re.Match, int, int]],
) -> list[tuple[int, int]]:
    """Select plain Cookie spans so JSON scanning cannot split a header."""
    return [
        (value_start, value_end)
        for match, value_start, value_end in records
        if not match.group("name_quote")
    ]


def _looks_like_json_candidate(text: str, start: int) -> bool:
    """Recognize a JSON opener inside otherwise unmatched quoted prose."""
    i = start + 1
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text):
        return False
    if text[start] == "{":
        return text[i] in {'"', "}"}
    return text[i] in '[{"-0123456789tfn]'


def _scan_json_string_container_candidate(
    text: str,
    start: int,
) -> tuple[int, bool, bool, bool] | None:
    """Return ``(end, decoded_container, over_limit, valid)`` for a string span."""
    over_limit = False
    valid_json_string = True
    decoded_container: bool | None = None
    i = start + 1
    text_len = len(text)

    while i < text_len:
        if i - start >= _COOKIE_JSON_MAX_CANDIDATE_CHARS:
            over_limit = True

        ch = text[i]
        decoded = ""
        if ch == "\\" and i + 1 < text_len:
            escaped = text[i + 1]
            if (
                escaped == "u"
                and i + 5 < text_len
                and all(c in "0123456789abcdefABCDEF" for c in text[i + 2 : i + 6])
            ):
                decoded = chr(int(text[i + 2 : i + 6], 16))
                i += 6
            else:
                decoded = _EmbeddedJSONEvidenceProbe._ESCAPES.get(escaped, "")
                if not decoded:
                    valid_json_string = False
                i += 2
        elif ch == '"':
            end = i + 1
            return (
                end,
                bool(decoded_container),
                over_limit or end - start > _COOKIE_JSON_MAX_CANDIDATE_CHARS,
                valid_json_string,
            )
        else:
            if ord(ch) < 0x20:
                valid_json_string = False
            else:
                decoded = ch
            i += 1

        if valid_json_string and decoded_container is None:
            for decoded_ch in decoded:
                if decoded_ch in " \t\r\n":
                    continue
                decoded_container = decoded_ch in "[{"
                break

    return None


def _string_tail_has_plain_cookie_header(tail: str) -> bool:
    """Recognize an explicit Cookie header ending at the latest decoded colon."""
    if not tail.endswith(":"):
        return False
    before_colon = tail[:-1].rstrip(" \t")
    for alias in ("set-cookie", "cookie"):
        if not before_colon.endswith(alias):
            continue
        prefix_end = len(before_colon) - len(alias)
        return prefix_end == 0 or before_colon[prefix_end - 1] not in (
            "abcdefghijklmnopqrstuvwxyz0123456789-"
        )
    return False


def _redact_cookie_headers(text: str) -> str:
    """Redact plain and embedded JSON Cookie representations in bounded O(n)."""
    # Replacement spans are disjoint top-level candidates. Bytes outside those
    # spans are handled by the existing plain-header redactor after the lexical
    # pass, preserving prose and malformed non-Cookie controls byte-for-byte.
    replacements: list[tuple[int, int, str]] = []
    plain_header_records = _plain_cookie_header_records(text)
    plain_header_spans = _plain_cookie_header_value_spans(plain_header_records)
    plain_span_index = 0
    candidate_start: int | None = None
    stack: list[str] = []
    depth = 0
    untracked_depth = False
    candidate_count = 0
    over_limit = False
    malformed_candidate = False
    in_string = False
    cookie_bearing = False
    string_probe: list[str] = []
    string_probe_overflow = False
    string_header_tail = ""
    embedded_probe = _EmbeddedJSONEvidenceProbe()
    outside_string = False
    text_len = len(text)
    i = 0

    while i < text_len:
        ch = text[i]

        if candidate_start is None:
            while (
                plain_span_index < len(plain_header_spans)
                and i >= plain_header_spans[plain_span_index][1]
            ):
                plain_span_index += 1
            if (
                plain_span_index < len(plain_header_spans)
                and plain_header_spans[plain_span_index][0]
                <= i
                < plain_header_spans[plain_span_index][1]
            ):
                # Leave the entire plain field-value intact for one redaction
                # pass; splitting it around a JSON-looking cookie value could
                # expose later request-cookie pairs after the replacement.
                outside_string = False
                i += 1
                continue

            # Ignore braces inside ordinary balanced quoted prose/header values.
            # A strong JSON opener can resynchronize after an unmatched prose
            # quote; treating JSON-looking text inside prose as a candidate is
            # conservative at this security boundary.
            if outside_string:
                if ch in "[{" and _looks_like_json_candidate(text, i):
                    outside_string = False
                elif ch == "\\" and i + 1 < text_len:
                    i += 2
                    continue
                elif ch == '"':
                    outside_string = False
                    i += 1
                    continue
                else:
                    i += 1
                    continue
            if ch == '"':
                string_candidate = _scan_json_string_container_candidate(text, i)
                if string_candidate is None:
                    outside_string = True
                    i += 1
                    continue
                end, decoded_container, string_over_limit, valid_json_string = (
                    string_candidate
                )
                if valid_json_string and decoded_container:
                    candidate_count += 1
                    replacement: str | None
                    if (
                        string_over_limit
                        or candidate_count > _COOKIE_JSON_MAX_CANDIDATES
                    ):
                        replacement = _COOKIE_JSON_REDACTED_SENTINEL
                    else:
                        replacement = _redact_cookie_json_document(
                            text[i:end],
                            cookie_bearing=False,
                        )
                    if replacement is not None:
                        replacements.append((i, end, replacement))
                elif not decoded_container:
                    outside_string = True
                    i += 1
                    continue
                outside_string = False
                i = end
                continue
            if ch not in "[{":
                i += 1
                continue

            candidate_start = i
            candidate_count += 1
            stack = [ch]
            depth = 1
            untracked_depth = False
            over_limit = candidate_count > _COOKIE_JSON_MAX_CANDIDATES
            malformed_candidate = False
            in_string = False
            cookie_bearing = False
            string_probe = []
            string_probe_overflow = False
            string_header_tail = ""
            embedded_probe = _EmbeddedJSONEvidenceProbe()
            i += 1
            continue

        if i - candidate_start >= _COOKIE_JSON_MAX_CANDIDATE_CHARS:
            over_limit = True

        if in_string:
            decoded = ""
            if ch == "\\" and i + 1 < text_len:
                escaped = text[i + 1]
                if (
                    escaped == "u"
                    and i + 5 < text_len
                    and all(
                        c in "0123456789abcdefABCDEF"
                        for c in text[i + 2 : i + 6]
                    )
                ):
                    decoded = chr(int(text[i + 2 : i + 6], 16))
                    i += 6
                else:
                    decoded = {
                        '"': '"',
                        "\\": "\\",
                        "/": "/",
                        "b": "\b",
                        "f": "\f",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                    }.get(escaped, "")
                    i += 2
            elif ch == '"':
                in_string = False
                if not string_probe_overflow and (
                    "".join(string_probe).lower() in {"cookie", "set-cookie"}
                ):
                    cookie_bearing = True
                if embedded_probe.has_cookie_evidence():
                    cookie_bearing = True
                string_probe = []
                string_probe_overflow = False
                string_header_tail = ""
                i += 1
                continue
            else:
                decoded = ch
                i += 1

            # Retain only bounded lexical state for this decoded JSON string.
            # Exact aliases are recognized at the closing quote; explicit wire
            # headers are recognized at a decoded colon wherever they occur.
            for decoded_ch in decoded:
                embedded_probe.feed(decoded_ch)
                if len(string_probe) < _COOKIE_JSON_STRING_PROBE_CHARS:
                    string_probe.append(decoded_ch)
                else:
                    string_probe_overflow = True
                string_header_tail = (string_header_tail + decoded_ch.lower())[-32:]
                if decoded_ch == ":" and _string_tail_has_plain_cookie_header(
                    string_header_tail
                ):
                    cookie_bearing = True
            continue

        if ch == '"':
            in_string = True
            string_probe = []
            string_probe_overflow = False
            string_header_tail = ""
            embedded_probe = _EmbeddedJSONEvidenceProbe()
            i += 1
            continue

        # A strong nested opener is a safe resynchronization point after malformed
        # prose. Preserve the malformed prefix and scan the new container as its
        # own candidate. A mismatched closer alone is not a reset point because
        # later strings in that same candidate may carry exact Cookie evidence.
        if (
            malformed_candidate
            and not untracked_depth
            and depth == 1
            and ch in "[{"
            and _looks_like_json_candidate(text, i)
        ):
            if cookie_bearing:
                replacements.append(
                    (candidate_start, i, _COOKIE_JSON_REDACTED_SENTINEL)
                )
            candidate_start = None
            stack = []
            depth = 0
            over_limit = False
            malformed_candidate = False
            cookie_bearing = False
            outside_string = False
            continue

        complete = False
        if ch in "[{":
            depth += 1
            if untracked_depth:
                pass
            elif depth > _COOKIE_JSON_MAX_DEPTH:
                # Stop retaining recursive delimiter state at the exact depth
                # bound. Numeric depth is sufficient to locate this candidate's
                # end; it will never be passed to the recursive JSON decoder.
                untracked_depth = True
                over_limit = True
            else:
                stack.append(ch)
        elif ch in "]}":
            if untracked_depth:
                depth -= 1
                complete = depth == 0
            elif not stack or (ch == "]" and stack[-1] != "[") or (
                ch == "}" and stack[-1] != "{"
            ):
                malformed_candidate = True
            else:
                stack.pop()
                depth -= 1
                complete = depth == 0

        i += 1
        if not complete:
            continue

        end = i
        replacement: str | None = None
        if malformed_candidate or over_limit:
            if cookie_bearing:
                replacement = _COOKIE_JSON_REDACTED_SENTINEL
        else:
            # This is the only top-level recursive decode site: each completed,
            # bounded candidate reaches it at most once.
            replacement = _redact_cookie_json_document(
                text[candidate_start:end],
                cookie_bearing=cookie_bearing,
            )
        if replacement is not None:
            replacements.append((candidate_start, end, replacement))

        candidate_start = None
        stack = []
        depth = 0
        untracked_depth = False
        over_limit = False
        malformed_candidate = False
        in_string = False
        cookie_bearing = False
        string_probe = []
        string_probe_overflow = False
        string_header_tail = ""
        outside_string = False

    # An incomplete candidate is never decoded. If its own JSON strings carry
    # exact Cookie aliases/header syntax, replace the unresolved tail rather than
    # exposing a credential. A prose substring containing "cookie" is not enough.
    if candidate_start is not None and cookie_bearing:
        replacements.append((candidate_start, text_len, _COOKIE_JSON_REDACTED_SENTINEL))

    if not replacements:
        return _redact_plain_cookie_headers(text, plain_header_records)

    chunks: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        chunks.append(_redact_plain_cookie_headers(text[cursor:start]))
        chunks.append(replacement)
        cursor = end
    chunks.append(_redact_plain_cookie_headers(text[cursor:]))
    return "".join(chunks)


def _mask_token_nonreusable(token: str) -> str:
    """Redact a prefix-matched credential to a NON-REUSABLE sentinel.

    Unlike :func:`_mask_token` (which keeps head/tail chars — fine for logs
    that are never fed back into a config), this emits a marker that:

    * cannot be mistaken for a usable-but-truncated key, so an agent that
      reads it from a config file and writes it back does NOT corrupt the
      stored credential into a dead 13-char string (issue #35519); and
    * still does not leak the secret material (no head/tail chars).

    The vendor prefix label is preserved for debuggability so the agent can
    still tell *which* credential is present (e.g. a GitHub PAT vs an OpenAI
    key) without seeing any of its bytes.
    """
    if not token:
        return "«redacted-secret»"
    # Preserve only the recognizable vendor prefix label (e.g. "ghp_", "sk-"),
    # never any of the random secret body.
    label = ""
    for sub in _PREFIX_SUBSTRINGS:
        if token.startswith(sub):
            label = sub
            break
    return f"«redacted:{label}…»" if label else "«redacted-secret»"


def redact_sensitive_text(
    text: str,
    *,
    force: bool = False,
    code_file: bool = False,
    file_read: bool = False,
) -> str:
    """Apply all redaction patterns to a block of text.

    Safe to call on any string -- non-matching text passes through unchanged.
    Enabled by default. Disable via security.redact_secrets: false in config.yaml.
    Set force=True for safety boundaries that must never return raw secrets
    regardless of the user's global logging redaction preference.

    Set code_file=True to skip the ENV-assignment and JSON-field regex
    patterns when the text is known to be source code (e.g. MAX_TOKENS=***
    constants, "apiKey": "test" fixtures). Prefix patterns, auth headers,
    Cookie headers, Discord tokens, private keys, DB connstrings, JWTs, and URL
    secrets are still redacted.

    Set file_read=True for file *content* returned to the agent (read_file /
    search_files / cat). Secrets are STILL redacted — they are never exposed —
    but prefix-matched credentials are replaced with a non-reusable sentinel
    (``«redacted:ghp_…»``) instead of a head/tail-preserving mask
    (``ghp_S1...Pn2T``). The old mask looked like a real-but-truncated key, so
    an agent reading it from config.yaml and writing it back silently corrupted
    the stored credential into a dead 13-char value → 401 (issue #35519). The
    sentinel is syntactically invalid as a token, so it can't be mistaken for a
    usable key or written back as one. Implies code_file=True (config/data
    files shouldn't trigger the source-code ENV/JSON false-positive paths).

    Performance: each regex pattern is gated behind a cheap substring
    pre-check (e.g. ``"=" in text`` for ENV assignments, ``"://" in text``
    for URLs, ``"eyJ" in text`` for JWTs). On a typical hermes log line
    (no secrets) this drops the 13-pattern scan from ~5.6us to ~1.8us per
    record (-68%). The pre-checks are conservative — false positives
    still run the full regex, which then doesn't match. False negatives
    are impossible because every regex requires the gated substring to
    match.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not (force or _REDACT_ENABLED):
        return text

    # file_read content shouldn't hit the source-code ENV/JSON false-positive
    # paths either (it's config/data, not log lines).
    if file_read:
        code_file = True

    # Known prefixes (sk-, ghp_, etc.) — gate on substring presence
    if _has_known_prefix_substring(text):
        _prefix_sub = _mask_token_nonreusable if file_read else _mask_token
        text = _PREFIX_RE.sub(lambda m: _prefix_sub(m.group(1)), text)

    # Bare Discord credentials have no vendor prefix, but their constrained
    # three-segment shape (or distinctive mfa. prefix) is recognizable. This
    # pass intentionally applies to source output and file reads too. File
    # reads retain NONE of the token's random bytes: use a syntactically invalid
    # non-reusable sentinel rather than the normal head/tail display mask.
    if "." in text:
        discord_sub = (
            (lambda _m: _discord_file_sentinel)
            if file_read
            else (lambda m: _mask_token(m.group(0)))
        )
        text = _discord_credential_re.sub(discord_sub, text)

    # ENV assignments: OPENAI_API_KEY=***  (skip for code files — false positives)
    if not code_file:
        if "=" in text:
            def _redact_env(m):
                name, quote, value = m.group(1), m.group(2), m.group(3)
                # Programmatic env lookups reference variable *names*, not
                # secret values — masking them corrupts code snippets in
                # prose/log contexts (issue #2852): ``KEY=os.getenv('X')``.
                if _ENV_LOOKUP_VALUE_RE.match(value):
                    return m.group(0)
                return f"{name}={quote}{_mask_token(value)}{quote}"
            text = _ENV_ASSIGN_RE.sub(_redact_env, text)
            # Lowercase/dotted config keys (issue #16413). Skip URLs entirely —
            # web-URL query params are intentionally passed through (see note
            # near the bottom of this function); _DB_CONNSTR_RE still guards
            # connection-string passwords.
            if "://" not in text:
                text = _CFG_DOTTED_RE.sub(_redact_env, text)
                text = _CFG_ANCHORED_RE.sub(_redact_env, text)

        # JSON fields: "apiKey": "***"  (skip for code files — false positives)
        if ":" in text and '"' in text:
            def _redact_json(m):
                key, value = m.group(1), m.group(2)
                # Same programmatic-env-lookup exception as _redact_env above
                # (issue #2852): "apiKey": "os.getenv('X')" is a code snippet,
                # not a leaked secret value.
                if _ENV_LOOKUP_VALUE_RE.match(value):
                    return m.group(0)
                return f'{key}: "{_mask_token(value)}"'
            text = _JSON_FIELD_RE.sub(_redact_json, text)

        # Unquoted YAML / colon config: password: ***  (after JSON so quoted
        # values are handled there; the lookahead in _YAML_ASSIGN_RE skips
        # quotes). Skip URLs — web-URL query params pass through by design.
        if ":" in text and "://" not in text:
            def _redact_yaml(m):
                key, sep, value = m.group(1), m.group(2), m.group(3)
                # Same programmatic-env-lookup exception as _redact_env above
                # (issue #2852): api_key: os.getenv('X') is a code snippet,
                # not a leaked secret value.
                if _ENV_LOOKUP_VALUE_RE.match(value):
                    return m.group(0)
                return f"{key}{sep}{_mask_token(value)}"
            text = _YAML_ASSIGN_RE.sub(_redact_yaml, text)

    # Authorization headers — _AUTH_HEADER_RE matches any scheme after
    # "[Proxy-]Authorization:" case-insensitively, so "uthorization" is the
    # cheapest substring gate that covers every casing without a casefold().
    if "uthorization" in text or "UTHORIZATION" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: m.group(1) + (m.group(2) or "") + _mask_token(m.group(3)),
            text,
        )

    # Cookie is a multi-value header and Set-Cookie has non-secret attributes,
    # so neither can use the opaque single-header-value rule. Header names are
    # case-insensitive; all lines are processed. JSON aliases may encode any
    # alias byte as ``\uXXXX``; gate those container-shaped texts into the
    # bounded scanner without running it for arbitrary non-JSON prose.
    cookie_gate_text = text.lower()
    if "cookie" in cookie_gate_text or (
        "\\u" in cookie_gate_text and ("{" in text or "[" in text)
    ):
        text = _redact_cookie_headers(text)

    # API-key style headers (x-api-key, api-key, …). Header values are
    # colon-separated, so gate on ":" — the regex itself is the precise filter.
    if ":" in text:
        text = _SECRET_HEADER_RE.sub(
            lambda m: m.group(1) + _mask_token(m.group(2)),
            text,
        )

    # Telegram bot tokens — pattern requires ":<token>" with digits prefix
    if ":" in text:
        def _redact_telegram(m):
            prefix = m.group(1) or ""
            digits = m.group(2)
            return f"{prefix}{digits}:***"
        text = _TELEGRAM_RE.sub(_redact_telegram, text)

    # Private-key blocks require a PEM-like BEGIN line plus substantial key
    # material, including when the header/body is truncated at frame end.
    if "BEGIN" in text.upper() and "---" in text:
        text = _PRIVATE_KEY_MATERIAL_RE.sub("[REDACTED PRIVATE KEY]", text)

    # Database connection string passwords. With code_file=True, a password
    # group that is a pure ``{...}`` brace expression is an f-string template
    # reference (e.g. f"postgresql://{user}:{pass}@{host}"), not a literal
    # credential — preserve it. Literal passwords are still redacted. The regex
    # forbids whitespace in the password group, so a single-line template's
    # group(2) is exactly the brace expression. See issue #33801.
    if "://" in text:
        if code_file:
            def _redact_db(m):
                pw = m.group(2)
                if pw.startswith("{") and pw.endswith("}"):
                    return m.group(0)
                return f"{m.group(1)}***{m.group(3)}"
            text = _DB_CONNSTR_RE.sub(_redact_db, text)
        else:
            text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

        # Bare-token userinfo in web/transport URLs: ``scheme://TOKEN@host``.
        # The git-remote-with-embedded-password shape from #6396. Only the
        # colon-less bare-token form is redacted — ``user:pass@`` and
        # query-string tokens are left to pass through (see the web-URL note
        # below). See _URL_BARE_TOKEN_RE for the false-positive guards.
        text = _URL_BARE_TOKEN_RE.sub(
            lambda m: f"{m.group(1)}{_mask_token(m.group(2))}{m.group(3)}",
            text,
        )

    # JWT tokens (eyJ... — base64-encoded JSON headers)
    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)

    # NOTE: Web-URL redaction (query params + userinfo + HTTP access-log
    # request targets) is intentionally OFF. Many legitimate workflows pass
    # opaque tokens through query strings — magic-link checkouts, OAuth
    # callbacks the agent is meant to follow, pre-signed share URLs — and
    # blanket-redacting param values by name breaks those skills mid-flow.
    # Known credential shapes (sk-, ghp_, JWTs, etc.) inside URLs are still
    # caught by _PREFIX_RE and _JWT_RE above. DB connection-string passwords
    # are still caught by _DB_CONNSTR_RE. The ONE userinfo case still redacted
    # is the colon-less bare-token form ``scheme://TOKEN@host`` (#6396, handled
    # by _URL_BARE_TOKEN_RE in the ``://`` block above): a bare credential in
    # userinfo is never a round-trip workflow token (those live in the query
    # string), so masking it can't break a skill. The ``user:pass@`` form is
    # left to pass through per #34029.

    # Form-urlencoded bodies (only triggers on clean k=v&k=v inputs).
    if "&" in text and "=" in text:
        text = _redact_form_body(text)

    # E.164 phone numbers (Signal, WhatsApp)
    if "+" in text:
        def _redact_phone(m):
            phone = m.group(1)
            if len(phone) <= 8:
                return phone[:2] + "****" + phone[-2:]
            return phone[:4] + "****" + phone[-4:]
        text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    return text


# Commands whose stdout is an environment-variable dump (KEY=value lines),
# NOT source code. For these, terminal-output redaction must run the
# ENV-assignment pass (code_file=False) so opaque tokens with no recognized
# vendor prefix (e.g. ``MY_SERVICE_TOKEN=abc123randomstring``) are still
# masked. For all other commands, code_file=True is used to avoid mangling
# legitimate source/config dumps (``MAX_TOKENS=100``, ``"apiKey": "x"``
# fixtures, ``postgresql://{user}`` f-string templates). See issue #43025.
_ENV_DUMP_COMMANDS = frozenset({"env", "printenv", "set", "export", "declare"})


def is_env_dump_command(command: str | None) -> bool:
    """Return True if ``command`` dumps environment variables to stdout.

    Detects ``env`` / ``printenv`` / ``set`` / ``export`` / ``declare`` as the
    first token of any segment in a pipeline or sequence (``;`` / ``&&`` /
    ``||`` / ``|``). Conservative: a parse failure or anything unrecognized
    returns False (callers then fall back to the safer code_file=True path,
    which still masks prefix-shaped keys).
    """
    if not command or not isinstance(command, str):
        return False
    # Split on shell separators, then inspect the first token of each segment.
    segments = re.split(r"[|;&]+", command)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if tokens and tokens[0] in _ENV_DUMP_COMMANDS:
            return True
    return False


def redact_terminal_output(
    output: str, command: str | None = None, *, force: bool = False
) -> str:
    """Redact secrets from terminal/process stdout.

    Single redaction policy for ALL terminal-output surfaces — foreground
    ``terminal`` results AND background ``process(action=poll/log/wait)``
    output — so they can't diverge. Picks ``code_file`` based on whether
    ``command`` is an environment dump:

    - env-dump command (``env``/``printenv``/``set``/``export``/``declare``)
      → ``code_file=False`` so the ENV-assignment pass masks opaque tokens.
    - anything else (or unknown command) → ``code_file=True`` to avoid
      false positives on source/config dumps.

    ``force=True`` bypasses the global ``security.redact_secrets`` preference
    for safety boundaries that must never emit raw credentials.
    """
    if not output:
        return output
    code_file = not is_env_dump_command(command or "")
    return redact_sensitive_text(output, force=force, code_file=code_file)


# Substrings used to gate ``_PREFIX_RE`` execution. If none of these appear in
# the input string, the prefix regex cannot match anything, so we skip it.
# False positives are fine (they just run the regex, which then matches
# nothing) — the bound is "no false negatives" and that holds because every
# pattern in ``_PREFIX_PATTERNS`` has at least one of these as a literal
# substring of its leading characters.
#
# Derived automatically from ``_PREFIX_PATTERNS`` at module load time so a
# future PR that adds a new prefix to the regex list can't silently break
# the screen.

def _extract_literal_prefix(pattern: str) -> str:
    """Return the leading literal characters of a regex pattern.

    Stops at the first regex metacharacter (``[``, ``(``, ``\\``, ``.``,
    ``?``, ``*``, ``+``, ``|``, ``{``, ``^``, ``$``).  Returns the literal
    that any match of the pattern MUST contain as a substring, so the
    pre-screen never produces false negatives.
    """
    meta = "[(\\.?*+|{^$"
    for i, ch in enumerate(pattern):
        if ch in meta:
            return pattern[:i]
    return pattern


_PREFIX_SUBSTRINGS = tuple(
    _extract_literal_prefix(p) for p in _PREFIX_PATTERNS
)


def _has_known_prefix_substring(text: str) -> bool:
    """Return True if ``text`` contains any known credential prefix substring.

    Used as a cheap pre-check before invoking the expensive ``_PREFIX_RE``.
    """
    return any(p in text for p in _PREFIX_SUBSTRINGS)


_HTTP_METHOD_SUBSTRINGS = (
    "GET ",
    "POST ",
    "PUT ",
    "PATCH ",
    "DELETE ",
    "HEAD ",
    "OPTIONS ",
    "TRACE ",
    "CONNECT ",
)


def _has_http_method_substring(text: str) -> bool:
    """Cheap pre-check before scanning for access-log request targets."""
    upper = text.upper()
    return any(method in upper for method in _HTTP_METHOD_SUBSTRINGS)


class RedactingFormatter(logging.Formatter):
    """Log formatter that redacts secrets from all log messages."""

    def __init__(self, fmt=None, datefmt=None, style='%', **kwargs):
        super().__init__(fmt, datefmt, style, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_sensitive_text(original)

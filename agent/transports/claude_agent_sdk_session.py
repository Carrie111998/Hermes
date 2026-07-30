"""Session adapter for the claude-agent-sdk runtime.

Owns one Claude Agent SDK client per Hermes session — the structural twin of
``codex_app_server_session.py``, with the Codex JSON-RPC subprocess replaced
by Anthropic's official ``claude-agent-sdk`` (which manages the Claude Code
CLI subprocess, its agent loop, and — critically — **subscription OAuth**:
Claude-managed login storage, never an environment credential or metered
backend). See GitHub issue #25267.

Lifecycle:
    session = ClaudeAgentSdkSession(cwd="/home/x/proj", model="claude-opus-4-8")
    session.ensure_started()                       # loop thread + SDK connect
    result = session.run_turn(user_input="hello")  # blocks until ResultMessage
    session.close()                                # disconnect + stop loop

Threading model: the SDK is async-first, but AIAgent.run_conversation() is
synchronous (the same constraint that made CodexAppServerClient thread-based).
The adapter owns a dedicated background thread running one asyncio event loop
for the whole session lifetime; every SDK coroutine is marshaled onto it with
``asyncio.run_coroutine_threadsafe`` and awaited with a timeout, so the SDK
client keeps stable loop affinity and ``run_turn`` stays blocking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

# TurnResult is the shared contract with the runtime glue — reused verbatim
# from the codex session module (same fields, same semantics) so
# ``run_claude_agent_sdk_turn`` mirrors ``run_codex_app_server_turn`` 1:1.
from agent.transports.codex_app_server_session import TurnResult
from agent.transports.claude_sdk_event_projector import ClaudeSdkEventProjector

logger = logging.getLogger(__name__)


@dataclass
class ClaudeTurnResult(TurnResult):
    """Turn result with the model identifier reported by the SDK stream."""

    response_model: Optional[str] = None
    subscription_attested: bool = False
    # True only when a resumed SDK client was rejected during connect before
    # client.query() could run. The runtime may retry only this explicit phase;
    # every query/response/timeout/generic failure stays non-replayable.
    retry_safe_before_query: bool = False


# HERMES_TERMINAL_SECURITY_MODE → SDK permission_mode. A deploy-side env
# knob read at session construction (default "auto"), mirroring the codex
# session's identical switch — no config key backs it in either runtime.
# "auto" parity note: codex's default profile is workspace-write; the
# closest SDK mode is acceptEdits (file edits auto-approved inside cwd).
# Non-default modes route tool calls through can_use_tool / fail-closed —
# reachable only when an operator exports the variable.
_HERMES_TO_SDK_PERMISSION_MODE = {
    "auto": "acceptEdits",
    "approval-required": "default",
    "unrestricted": "bypassPermissions",
    "yolo": "bypassPermissions",
}


def _approvals_mode_off() -> bool:
    """Return the live Hermes approval opt-out without widening its meaning."""
    from tools.approval import _get_approval_mode

    return _get_approval_mode() == "off"


# ``ClaudeAgentOptions.tools`` is the pinned SDK's base native-tool surface.
# In native-read-only mode this is the entire tool surface: no MCP server or
# allowed_tools entries are materialized. Pair the exact native set with
# explicit mutator denies as defense in depth.
_NATIVE_READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
_NATIVE_MUTATOR_DENIES = ("Write", "Edit", "Bash", "NotebookEdit")

_SDK_DISCONNECT_TIMEOUT_SECONDS = 30.0
_UNSUPPORTED_IMAGE_ERROR = (
    "claude-agent-sdk image inputs are unsupported until rich-image "
    "transport is implemented"
)

# Substrings in SDK/CLI errors that signal broken subscription credentials.
# Conservative on purpose — mirrors codex's _OAUTH_REFRESH_FAILURE_HINTS
# contract: every needle is a phrase, never a bare token. Bare "401" matched
# tool ids and byte offsets; bare "credentials" matched an MCP server
# complaining about its OWN files — and a hit retires the session, so a
# false positive is a wrong shutdown.
_AUTH_FAILURE_HINTS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "401 unauthorized",
    "unauthorized",
    "oauth token",
    "token has expired",
    "expired token",
    "invalid bearer token",
)


def classify_auth_failure(*parts: str) -> Optional[str]:
    """Return a user-friendly re-auth hint if the strings look like a Claude
    subscription auth failure; otherwise None. The hint keeps the underlying
    error text: a hit retires the session, so the evidence must survive the
    redirect (codex surfaces the original error the same way)."""
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return None
    for needle in _AUTH_FAILURE_HINTS:
        if needle in haystack:
            original = next((p.strip() for p in parts if p and p.strip()), "")
            if len(original) > 400:
                original = original[:400] + "…"
            return (
                "Claude authentication failed — the subscription OAuth token "
                "looks expired or invalid. Run `claude auth login` on this "
                "machine to refresh Claude-managed login storage, then retry. "
                f"(underlying error: {original})"
            )
    return None


def check_claude_sdk_available() -> tuple[bool, str]:
    """Preflight: the optional SDK extra must be importable, and it bundles /
    locates the Claude Code CLI itself. Mirrors check_codex_binary()."""
    try:
        from tools import lazy_deps

        lazy_deps.ensure("provider.claude_agent_sdk", prompt=False)
    except Exception as exc:
        return False, str(exc)
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return (
            False,
            "claude-agent-sdk is not installed. "
            "Install with: pip install 'hermes-agent[claude-agent-sdk]'",
        )
    return True, "ok"


def _hermes_repo_root() -> str:
    """Repo root for the hermes-tools MCP subprocess (PYTHONPATH)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


# The SDK serializes the stdio MCP config — env INCLUDED — into the claude
# CLI's --mcp-config argument, i.e. onto the subprocess argv, which any local
# user can read via ps. Nothing secret may ever ride this dict: the env is a
# minimal ALLOWLIST, never a copy of the credentialed environment. Keyed
# Hermes tools inside the server degrade via their own check_fns — the
# subscription lane's fail-closed posture.
_MCP_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "PYTHONUTF8",
    "HERMES_HOME",
    "HERMES_KANBAN_TASK",
    "HERMES_MCP_STATE_DB",  # the shims' documented state-DB override — a path, not a secret
    "HERMES_QUIET",
    "HERMES_REDACT_SECRETS",
)


# ClaudeAgentOptions.env is overlaid on the SDK subprocess's inherited
# environment, not used as a replacement. Therefore every ambient key must be
# present here: allowed runtime/config values keep their value, and everything
# else gets an explicit empty-string override. In particular, do not preserve
# proxies, SSH agents, cloud credentials, Hermes secrets, provider keys, or bot
# tokens. The SDK adds its own CLAUDE_CODE_ENTRYPOINT/version markers later.
_CLI_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "PATH",
        "SHELL",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "TERM",
        "COLORTERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "NO_COLOR",
        "FORCE_COLOR",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # Windows process/bootstrap paths.
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)


def _build_sanitized_cli_env() -> dict[str, str]:
    """Return the complete SDK env overlay with ambient secrets blanked."""
    return {
        key: value
        if key in _CLI_ENV_ALLOWLIST or key.startswith("LC_")
        else ""
        for key, value in os.environ.items()
    }


def _resolve_claude_cli_path() -> str:
    """Resolve the exact CLI selected by the pinned SDK transport."""
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    transport = SubprocessCLITransport(
        prompt=_empty_sdk_prompt_stream(),
        options=ClaudeAgentOptions(),
    )
    return transport._find_cli()


def _attest_claude_subscription(*, env: dict[str, str], cwd: str) -> str:
    """Require a logged-in first-party Claude.ai identity without exposing it.

    The status child receives the same sanitized environment snapshot later
    passed to ``ClaudeAgentOptions.env``. Status output may contain account
    details, so neither stdout nor stderr is logged or copied into errors.
    """
    from hermes_cli._subprocess_compat import windows_hide_flags

    cli_path = _resolve_claude_cli_path()
    try:
        completed = subprocess.run(
            [cli_path, "--setting-sources=", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            creationflags=windows_hide_flags(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Claude.ai subscription authentication attestation failed. "
            "Run `claude auth login` and retry."
        ) from exc

    if completed.returncode != 0:
        raise RuntimeError(
            "Claude.ai subscription authentication attestation failed. "
            "Run `claude auth login` and retry."
        )
    try:
        status = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Claude.ai subscription authentication attestation returned "
            "malformed status. Run `claude auth login` and retry."
        ) from exc
    if not isinstance(status, dict):
        raise RuntimeError(
            "Claude.ai subscription authentication attestation returned "
            "malformed status. Run `claude auth login` and retry."
        )
    if status.get("loggedIn") is not True:
        raise RuntimeError(
            "Claude.ai subscription authentication is required. "
            "Run `claude auth login` and retry."
        )
    if (
        status.get("authMethod") != "claude.ai"
        or status.get("apiProvider") != "firstParty"
    ):
        raise RuntimeError(
            "claude-agent-sdk requires a first-party Claude.ai login; "
            "Console, API-key, cloud, and custom identities are unsupported."
        )
    return cli_path


async def _empty_sdk_prompt_stream():
    """Match the empty streaming prompt used by ``ClaudeSDKClient.connect(None)``."""
    return
    yield {}  # pragma: no cover - makes this an async generator


def _build_sanitized_sdk_transport(options: Any) -> Any:
    """Build the pinned SDK subprocess transport with a sanitized version probe.

    ``claude-agent-sdk==0.2.120`` applies ``options.env`` to the long-lived CLI
    child but omits ``env=`` from its preceding ``claude -v`` process. The SDK's
    supported custom-Transport seam lets us repair only that preflight while
    inheriting its command construction, streaming, stderr, and cleanup logic.
    """
    import re
    from contextlib import suppress
    from subprocess import PIPE

    import anyio
    from claude_agent_sdk._errors import CLINotFoundError
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        MINIMUM_CLAUDE_CODE_VERSION,
        SubprocessCLITransport,
    )

    class _SanitizedSubprocessCLITransport(SubprocessCLITransport):
        async def _check_claude_version(self) -> None:
            """Preserve the SDK check while giving its child the sanitized env."""
            if self._cli_path is None:
                raise CLINotFoundError("CLI path not resolved. Call connect() first.")
            version_process = None
            try:
                with anyio.fail_after(2):
                    version_process = await anyio.open_process(
                        [self._cli_path, "-v"],
                        stdout=PIPE,
                        stderr=PIPE,
                        env=dict(self._options.env or {}),
                    )

                    if version_process.stdout:
                        stdout_bytes = await version_process.stdout.receive()
                        version_output = stdout_bytes.decode().strip()

                        match = re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", version_output)
                        if match:
                            version = match.group(1)
                            version_parts = [int(x) for x in version.split(".")]
                            min_parts = [
                                int(x)
                                for x in MINIMUM_CLAUDE_CODE_VERSION.split(".")
                            ]

                            if version_parts < min_parts:
                                logger.warning(
                                    "Claude Code version %s at %s is unsupported in the "
                                    "Agent SDK. Minimum required version is %s. Some "
                                    "features may not work correctly.",
                                    version,
                                    self._cli_path,
                                    MINIMUM_CLAUDE_CODE_VERSION,
                                )
            except Exception:
                pass
            finally:
                if version_process:
                    with suppress(Exception):
                        version_process.terminate()
                    with suppress(Exception):
                        await version_process.wait()

    return _SanitizedSubprocessCLITransport(
        prompt=_empty_sdk_prompt_stream(),
        options=options,
    )


# Any one of these non-empty variables can select or authenticate a metered
# Claude backend. This runtime is subscription-only, so reject the route before
# the SDK creates its inherited-environment CLI subprocess. The cloud-provider
# credential names below are the documented Bedrock/Vertex credential paths in
# addition to the explicit Claude Code selectors and endpoint overrides.
_BILLING_ROUTE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_MANTLE",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "CLOUD_ML_REGION",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
)


def _provider_config() -> dict:
    """The `agent.claude_agent_sdk` config block ({} when absent/unreadable)."""
    try:
        from hermes_cli.config import load_config_readonly

        block = ((load_config_readonly() or {}).get("agent", {}) or {}).get(
            "claude_agent_sdk", {}
        )
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _provider_flag(
    config_key: str,
    default: bool = False,
    *,
    config: Optional[dict] = None,
) -> bool:
    """Behavioural flag read from `agent.claude_agent_sdk.<key>` in config.yaml.

    config.yaml is the ONLY interface. AGENTS.md keeps non-secret behavioural
    settings out of `HERMES_*` environment variables, so there is deliberately
    no env override here — a deployment sets the key in config.yaml.
    Canonical defaults live in `hermes_cli/config.py::DEFAULT_CONFIG`.
    """
    value = (config if config is not None else _provider_config()).get(
        config_key, default
    )
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _normalize_add_dirs(value: Any) -> list[str]:
    """Validate and snapshot ``agent.claude_agent_sdk.add_dirs``.

    Existence is deliberately not required: the SDK accepts paths that may be
    mounted or created later. Validation is lexical and deterministic at SDK
    session creation, before any child or client is constructed.
    """
    config_key = "agent.claude_agent_sdk.add_dirs"
    if not isinstance(value, list):
        raise ValueError(f"{config_key} must be a list")

    normalized: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            raise ValueError(
                f"{config_key} entries must be non-empty absolute paths "
                f"(invalid entry at index {index})"
            )
        if (
            not entry
            or entry != entry.strip()
            or not os.path.isabs(entry)
            or "\x00" in entry
        ):
            raise ValueError(
                f"{config_key} entries must be non-empty absolute paths "
                f"(invalid entry at index {index})"
            )
        path = os.path.normpath(entry)
        if path not in seen:
            seen.add(path)
            normalized.append(path)
    return normalized


def _build_hermes_tools_mcp_config(
    hermes_session_id: Optional[str] = None,
) -> dict[str, Any]:
    """The stdio MCP server exposing Hermes tools into the SDK agent loop —
    the exact server the codex runtime uses (backend-agnostic), launched with
    this venv's interpreter. McpStdioServerConfig has no cwd field, so the
    repo root rides PYTHONPATH."""
    env = {
        key: os.environ[key]
        for key in _MCP_ENV_ALLOWLIST
        if os.environ.get(key)
    }
    env["PYTHONPATH"] = _hermes_repo_root() + os.pathsep + os.environ.get("PYTHONPATH", "")
    if hermes_session_id:
        # Lets the stateless session_search shim exclude the calling
        # session's own lineage from recall results (#26567). The shim reads
        # the canonical HERMES_SESSION_ID — set explicitly with THIS
        # session's id rather than allowlisting the ambient variable, so a
        # multi-session host can never leak a sibling session's id into the
        # subprocess.
        env["HERMES_SESSION_ID"] = str(hermes_session_id)
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "agent.transports.hermes_tools_mcp_server"],
        "env": env,
    }


class ClaudeAgentSdkSession:
    """One SDK client per Hermes session, lifetime owned by AIAgent.

    Not thread-safe from the caller's side — one caller drives it at a time,
    matching AIAgent.run_conversation(). Internally owns a loop thread."""

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        add_dirs: Optional[list[str]] = None,
        native_read_only: bool = False,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        system_prompt_append: Optional[str] = None,
        approval_callback: Optional[Callable[..., str]] = None,
        on_tool_started: Optional[Callable[[str, str, dict], None]] = None,
        max_budget_usd: Optional[float] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        auth_status_checker: Optional[Callable[..., Optional[str]]] = None,
        include_hermes_tools: bool = True,
        hermes_session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        on_stream_delta: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._add_dirs = _normalize_add_dirs([] if add_dirs is None else add_dirs)
        self._native_read_only = bool(native_read_only)
        self._model = model
        if self._native_read_only:
            # Native read-only is a fail-closed boundary. Ambient terminal
            # policy (including acceptEdits/bypassPermissions) cannot widen it.
            self._permission_mode = "default"
        else:
            self._permission_mode = (
                permission_mode
                or _HERMES_TO_SDK_PERMISSION_MODE.get(
                    os.environ.get("HERMES_TERMINAL_SECURITY_MODE", "auto"),
                    "acceptEdits",
                )
            )
        self._system_prompt_append = system_prompt_append
        self._approval_callback = approval_callback
        self._on_tool_started = on_tool_started
        self._max_budget_usd = max_budget_usd
        self._client_factory = client_factory  # test seam
        self._auth_status_checker = auth_status_checker  # test seam
        self._include_hermes_tools = include_hermes_tools
        # Hermes-side session id, exported to the hermes-tools MCP subprocess
        # so the stateless session_search shim can exclude its own lineage.
        self._hermes_session_id = hermes_session_id
        # SDK-side session id to resume (#25267 continuity). Resuming restores
        # the model context and keeps the same session id; a
        # stale id fails the session start (the caller retires + retries
        # fresh).
        self._resume_session_id = resume_session_id
        # Display-only partial-text consumer. Deltas never
        # enter the projected transcript; the gateway's stream consumer
        # handles rate limiting and the already_sent final-send dedup.
        self._on_stream_delta = on_stream_delta

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._cli_env: Optional[dict[str, str]] = None
        self._cli_path: Optional[str] = None
        self._subscription_attested = False
        self._session_id: Optional[str] = None
        self._interrupt_event = threading.Event()
        self._closed = False

    # ---------- lifecycle ----------

    def ensure_started(self) -> str:
        """Start the loop thread, build the SDK client, connect. Idempotent —
        returns the session marker (SDK session ids arrive on first result)."""
        if self._client is not None:
            return self._session_id or "pending"
        # Hard rule, enforced fail-closed: this provider exists to bill the
        # Claude subscription. Refuse every environment-selected credential or
        # alternate backend before creating the inherited-environment CLI.
        for route_var in _BILLING_ROUTE_ENV_VARS:
            if os.environ.get(route_var):
                raise RuntimeError(
                    f"claude-agent-sdk runtime refuses to start: {route_var} "
                    "is set, which can select credentials or a metered backend. "
                    "Unset it; this runtime uses only Claude-managed subscription "
                    "login storage."
                )
        if self._client_factory is None:
            ok, msg = check_claude_sdk_available()
            if not ok:
                raise RuntimeError(msg)

        # Snapshot once so the auth-status child and the long-lived SDK child
        # receive the same sanitized parent environment. Attestation happens
        # before the loop, SDK client construction, or connect.
        self._cli_env = _build_sanitized_cli_env()
        checker = self._auth_status_checker or _attest_claude_subscription
        resolved_cli_path = checker(env=self._cli_env, cwd=self._cwd)
        if resolved_cli_path:
            self._cli_path = resolved_cli_path
        self._subscription_attested = True

        self._start_loop_thread()
        client = self._build_client()
        # Assign BEFORE connect: a connect timeout/cancel leaves a
        # half-connected client whose CLI subprocess close() must still reap
        # — a None _client would skip disconnect and orphan it.
        self._client = client
        self._run_coro(client.connect(), timeout=60.0)
        logger.info(
            "claude-agent-sdk session started: model=%s mode=%s cwd=%s",
            self._model or "cli-default",
            self._permission_mode,
            self._cwd,
        )
        return self._session_id or "pending"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None and self._loop is not None:
            try:
                self._run_coro(
                    self._client.disconnect(),
                    timeout=_SDK_DISCONNECT_TIMEOUT_SECONDS,
                )
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._client = None
        self._stop_loop_thread()

    def __enter__(self) -> "ClaudeAgentSdkSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- interrupt ----------

    def consume_interrupt(self) -> None:
        """Clear a pending interrupt signal — the caller honored it through
        another path (e.g. the runtime's cold-agent short-circuit)."""
        self._interrupt_event.clear()

    def request_interrupt(self) -> None:
        """Idempotent: signal the active turn loop to interrupt and unwind."""
        self._interrupt_event.set()
        if self._client is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.interrupt(), self._loop
                )
            except Exception:  # pragma: no cover
                logger.debug("SDK interrupt scheduling failed", exc_info=True)

    # ---------- per-turn ----------

    def run_turn(
        self,
        user_input: Any,
        *,
        turn_timeout: float = 600.0,
    ) -> ClaudeTurnResult:
        """Send a user message and block until the SDK's ResultMessage,
        projecting the typed stream into Hermes' messages shape."""
        result = ClaudeTurnResult()
        try:
            text = _coerce_turn_input_text(user_input)
        except _UnsupportedImageInput:
            result.error = _UNSUPPORTED_IMAGE_ERROR
            return result
        try:
            self.ensure_started()
        except Exception as exc:
            hint = classify_auth_failure(str(exc))
            result.error = hint or f"claude-agent-sdk startup failed: {exc}"
            result.should_retire = True
            # A fresh retry is safe only for a resumed client rejected by the
            # SDK during connect. Requiring both a constructed client and a
            # typed SDK connection/process error excludes attestation/build
            # failures, generic exceptions, and timeouts.
            if self._resume_session_id and self._client is not None:
                try:
                    from claude_agent_sdk._errors import (
                        CLIConnectionError,
                        ProcessError,
                    )

                    result.retry_safe_before_query = isinstance(
                        exc, (CLIConnectionError, ProcessError)
                    )
                except ImportError:  # pragma: no cover - optional SDK absent
                    pass
            return result
        result.subscription_attested = self._subscription_attested

        # An interrupt that arrived between turns or during connect (up to
        # 60s) targets THIS turn — honor it instead of erasing it. (The old
        # unconditional clear() silently swallowed that window.)
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()
            result.interrupted = True
            return result
        try:
            turn_data = self._run_coro(
                self._consume_turn(text), timeout=turn_timeout
            )
        except asyncio.TimeoutError:
            self.request_interrupt()
            result.interrupted = True
            result.error = f"turn timed out after {turn_timeout:.0f}s"
            result.should_retire = True
            return result
        except Exception as exc:
            hint = classify_auth_failure(str(exc))
            result.error = hint or f"claude-agent-sdk turn failed: {exc}"
            result.should_retire = True
            return result

        result.final_text = turn_data["final_text"]
        result.projected_messages = turn_data["messages"]
        result.tool_iterations = turn_data["tool_iterations"]
        result.token_usage_last = turn_data["usage"]
        result.token_usage_total = turn_data["usage"]
        result.thread_id = self._session_id
        result.turn_id = turn_data.get("result_uuid")
        result.response_model = turn_data.get("model")
        result.interrupted = self._interrupt_event.is_set()
        if result.interrupted:
            # Consume the honored interrupt so it cannot bleed into the
            # next turn on this session object.
            self._interrupt_event.clear()
        if turn_data["error"]:
            hint = classify_auth_failure(turn_data["error"])
            result.error = hint or turn_data["error"]
            result.should_retire = True
        return result

    # ---------- internals ----------

    async def _consume_turn(self, text: str) -> dict[str, Any]:
        """The async side of one turn: query + drain receive_response()."""
        projector = ClaudeSdkEventProjector()
        out: dict[str, Any] = {
            "final_text": "",
            "messages": [],
            "tool_iterations": 0,
            "usage": None,
            "error": None,
            "result_uuid": None,
            "model": None,
        }
        await self._client.query(text)
        async for message in self._client.receive_response():
            # Capture the SDK session id from ANY message that carries it —
            # the init SystemMessage announces it first, so even a turn
            # interrupted before its ResultMessage keeps a resumable id.
            early_sid = getattr(message, "session_id", None)
            if early_sid:
                self._session_id = early_sid
            if self._interrupt_event.is_set():
                break
            if type(message).__name__ == "StreamEvent":
                self._forward_stream_delta(message)
                continue
            if type(message).__name__ == "AssistantMessage":
                response_model = getattr(message, "model", None)
                if isinstance(response_model, str) and response_model:
                    out["model"] = response_model
            self._notify_tool_started(message)
            projection = projector.project(message)
            if projection.messages:
                out["messages"].extend(projection.messages)
            if projection.is_tool_iteration:
                out["tool_iterations"] += 1
            if projection.final_text is not None:
                out["final_text"] = projection.final_text
            if projection.is_result:
                usage = getattr(message, "usage", None)
                if isinstance(usage, dict):
                    out["usage"] = dict(usage)
                sid = getattr(message, "session_id", None)
                if sid:
                    self._session_id = sid
                out["result_uuid"] = getattr(message, "uuid", None)
                subtype = getattr(message, "subtype", "") or ""
                if getattr(message, "is_error", False):
                    errors = getattr(message, "errors", None) or []
                    out["error"] = (
                        f"SDK result error (subtype={subtype}): "
                        + ("; ".join(str(e) for e in errors) or subtype)
                    )
                elif subtype not in ("", "success"):
                    # e.g. error_max_turns / error_max_budget_usd — surface
                    # honestly; the partial transcript is still projected.
                    out["error"] = f"SDK turn ended: {subtype}"
        return out

    def _forward_stream_delta(self, message: Any) -> None:
        """Relay a top-level text delta to the display callback (never the
        transcript). Subagent streams (parent_tool_use_id set) stay quiet."""
        if self._on_stream_delta is None:
            return
        if getattr(message, "parent_tool_use_id", None):
            return
        event = getattr(message, "event", None) or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        text = delta.get("text")
        if not text:
            return
        try:
            self._on_stream_delta(text)
        except Exception:  # pragma: no cover - display callback
            logger.debug("stream delta callback raised", exc_info=True)

    def _notify_tool_started(self, message: Any) -> None:
        """Bridge ToolUseBlocks to Hermes tool-progress (gateway breadcrumbs),
        mirroring codex_runtime._codex_note_to_tool_progress (#38835)."""
        if self._on_tool_started is None:
            return
        if type(message).__name__ != "AssistantMessage":
            return
        for block in getattr(message, "content", None) or []:
            if type(block).__name__ != "ToolUseBlock":
                continue
            name = getattr(block, "name", "") or "unknown"
            args = getattr(block, "input", None) or {}
            if not isinstance(args, dict):
                args = {"input": args}
            preview = _tool_preview(name, args)
            try:
                self._on_tool_started(name, preview, args)
            except Exception:  # pragma: no cover - display callback
                logger.debug("tool-progress callback raised", exc_info=True)

    def build_option_fields(self) -> dict[str, Any]:
        """The ClaudeAgentOptions field dict — plain data so tests can assert
        on it without importing the SDK."""
        mcp_servers: dict[str, Any] = {}
        allowed_tools: list[str] = []
        if self._include_hermes_tools and not self._native_read_only:
            from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS

            mcp_servers["hermes-tools"] = _build_hermes_tools_mcp_config(
                hermes_session_id=self._hermes_session_id
            )
            # Hermes registry tools enforce their own safety contracts. Approve
            # this curated namespace so a headless gateway turn does not receive
            # an SDK permission prompt that it has no interactive channel to answer.
            allowed_tools = [
                *(f"mcp__hermes-tools__{name}" for name in EXPOSED_TOOLS),
                "mcp__hermes-tools__memory",
                "mcp__hermes-tools__session_search",
            ]

        system_prompt: Any = {"type": "preset", "preset": "claude_code"}
        if self._system_prompt_append:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_prompt_append,
            }

        can_use_tool = None
        if self._permission_mode == "default" and (
            self._native_read_only
            or self._approval_callback is not None
            or _approvals_mode_off()
        ):
            can_use_tool = self._make_can_use_tool()

        fields = {
            "model": self._model,
            "cwd": self._cwd,
            "env": (
                dict(self._cli_env)
                if self._cli_env is not None
                else _build_sanitized_cli_env()
            ),
            "permission_mode": self._permission_mode,
            "system_prompt": system_prompt,
            "mcp_servers": mcp_servers,
            # Never load user/project/local settings or ambient MCP config.
            # Hermes supplies cwd/add_dirs and the curated MCP map explicitly.
            "setting_sources": [],
            "strict_mcp_config": True,
            "allowed_tools": allowed_tools,
            "max_budget_usd": self._max_budget_usd,
            "can_use_tool": can_use_tool,
        }
        if self._native_read_only:
            # This boundary does not depend on can_use_tool: mutators are absent
            # from the native surface and explicitly denied. An empty source list
            # blocks user/project/local settings from widening permissions/roots.
            fields.update(
                {
                    "tools": list(_NATIVE_READ_ONLY_TOOLS),
                    "disallowed_tools": list(_NATIVE_MUTATOR_DENIES),
                }
            )
        if self._cli_path:
            fields["cli_path"] = self._cli_path
        if self._resume_session_id:
            fields["resume"] = self._resume_session_id
        if self._add_dirs:
            fields["add_dirs"] = list(self._add_dirs)
        # Default OFF (upstream-conservative): partial messages only when the
        # operator opts in via agent.claude_agent_sdk.streaming in config.yaml.
        if _provider_flag("streaming"):
            fields["include_partial_messages"] = True
        return fields

    def _build_client(self) -> Any:
        fields = self.build_option_fields()
        if self._client_factory is not None:
            return self._client_factory(options=fields)
        from dataclasses import replace

        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(**fields)
        # The SDK normally adds this field to the transport's option copy when
        # can_use_tool is active. A pre-constructed custom transport does not
        # receive that copy, so preserve the approval control-protocol flag here.
        transport_options = (
            replace(options, permission_prompt_tool_name="stdio")
            if options.can_use_tool
            else options
        )
        transport = _build_sanitized_sdk_transport(transport_options)
        return ClaudeSDKClient(options=options, transport=transport)

    def _make_can_use_tool(self) -> Any:
        """Bridge SDK permission requests onto Hermes' fail-closed policy.

        Native read-only denies every expansion request. With Hermes approvals
        off, ordinary requests are allowed without a prompt. Native Bash always
        crosses the existing pre-bypass guard floor before other modes delegate
        to Hermes' approval callback. Callback and guard failures deny."""
        approval_callback = self._approval_callback

        async def _can_use_tool(tool_name: str, tool_input: dict, context: Any):
            from claude_agent_sdk import (
                PermissionResultAllow,
                PermissionResultDeny,
            )

            if self._native_read_only:
                # cwd/add_dirs reads are authorized by the SDK without asking.
                # Every callback invocation is therefore an expansion request.
                return PermissionResultDeny(
                    message="native read-only permission expansion denied"
                )

            if tool_name == "Bash":
                command = tool_input.get("command")
                if not isinstance(command, str):
                    return PermissionResultDeny(
                        message="native Bash request has no string command"
                    )
                try:
                    from tools.approval import check_unconditional_command_guards

                    guard = await asyncio.to_thread(
                        check_unconditional_command_guards, command
                    )
                except Exception:
                    logger.exception("Hermes guard failed on native Bash request")
                    return PermissionResultDeny(
                        message="Hermes Bash guard failed closed"
                    )
                if not isinstance(guard, dict) or type(guard.get("approved")) is not bool:
                    logger.error("Hermes guard returned invalid native Bash result: %r", guard)
                    return PermissionResultDeny(
                        message="Hermes Bash guard returned an invalid result and failed closed"
                    )
                if not guard["approved"]:
                    message = guard.get("message")
                    return PermissionResultDeny(
                        message=(
                            message
                            if isinstance(message, str) and message
                            else "native Bash request blocked"
                        )
                    )

            if _approvals_mode_off():
                return PermissionResultAllow()

            if approval_callback is None:
                return PermissionResultDeny(
                    message="no Hermes approval callback is available"
                )
            try:
                choice = await asyncio.to_thread(
                    approval_callback,
                    f"{tool_name}({_tool_preview(tool_name, tool_input)})",
                    f"Claude requests tool {tool_name}",
                    allow_permanent=False,
                )
            except Exception:
                logger.exception("approval_callback raised on SDK permission")
                return PermissionResultDeny(message="approval callback failed")
            if choice in ("once", "session", "always"):
                return PermissionResultAllow()
            return PermissionResultDeny(message="denied by user")

        return _can_use_tool

    # ---------- loop-thread plumbing ----------

    def _start_loop_thread(self) -> None:
        if self._loop_thread is not None:
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run, name="claude-sdk-loop", daemon=True
        )
        thread.start()
        ready.wait(timeout=10)
        self._loop = loop
        self._loop_thread = thread

    def _stop_loop_thread(self) -> None:
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # pragma: no cover
                pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None

    def _run_coro(self, coro: Any, *, timeout: float) -> Any:
        import concurrent.futures

        assert self._loop is not None, "loop thread not started"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except (TimeoutError, concurrent.futures.TimeoutError):
            future.cancel()
            raise asyncio.TimeoutError(f"coroutine exceeded {timeout}s")


def _tool_preview(name: str, args: dict) -> str:
    """Short human preview of a tool call for progress breadcrumbs."""
    for key in ("command", "file_path", "path", "url", "query", "prompt"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return name


class _UnsupportedImageInput(ValueError):
    pass


def _contains_image_input(value: Any) -> bool:
    if isinstance(value, str):
        return value.lstrip().lower().startswith("data:image/")
    if isinstance(value, (list, tuple)):
        return any(_contains_image_input(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("type") in {"image", "image_url", "input_image"}:
        return True
    if "image_url" in value:
        return True
    source = value.get("source")
    if isinstance(source, dict) and source.get("type") in {"base64", "url"}:
        return True
    return any(_contains_image_input(item) for item in value.values())


def _coerce_turn_input_text(user_input: Any) -> str:
    """Collapse Hermes/OpenAI rich content into plain text input (same
    contract as the codex session's _coerce_turn_input_text)."""
    if _contains_image_input(user_input):
        raise _UnsupportedImageInput(_UNSUPPORTED_IMAGE_ERROR)
    if isinstance(user_input, str):
        return user_input
    if isinstance(user_input, list):
        parts: list[str] = []
        for item in user_input:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                if item is not None:
                    parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text"}:
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        text = "\n\n".join(p for p in parts if p).strip()
        return text
    return "" if user_input is None else str(user_input)

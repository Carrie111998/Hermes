"""Session adapter for codex app-server runtime.

Owns one live Codex thread for one Hermes-session workspace. Drives
`turn/start`, consumes
streaming notifications via CodexEventProjector, handles server-initiated
approval requests (apply_patch, exec command), translates cancellation,
and returns a clean turn result that AIAgent.run_conversation() can splice
into its `messages` list.

Lifecycle:
    session = CodexAppServerSession(cwd="/home/x/proj")
    session.ensure_started()                    # handshake + thread/start or thread/resume
    result = session.run_turn(user_input="hello")         # blocks until turn/completed
    # result.final_text          → assistant text returned to caller
    # result.projected_messages  → list of {role, content, ...} for messages list
    # result.tool_iterations     → how many tool-shaped items completed (skill nudge counter)
    # result.interrupted         → True if Ctrl+C / interrupt_requested fired mid-turn
    session.close()                                       # tears down subprocess

Threading model: the adapter is single-threaded from the caller's perspective.
The underlying CodexAppServerClient owns its own reader threads but exposes
blocking-with-timeout queues that this adapter polls in a loop, so the run_turn
call is synchronous and behaves like AIAgent's existing chat_completions loop.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.codex_responses_adapter import _format_responses_error
from agent.redact import redact_sensitive_text
from agent.transports.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
)
from agent.transports.codex_event_projector import CodexEventProjector

logger = logging.getLogger(__name__)


# How many tailing stderr lines from the codex subprocess to attach to a
# user-facing error when we don't have a more specific classification (OAuth,
# wedge watchdog, etc.). Small enough to keep error messages legible, large
# enough to surface a config/provider/auth diagnostic.
_STDERR_TAIL_LINES = 12
_INITIAL_USER_ECHO_MARKER = "_codex_initial_user_echo"


def _remaining_timeout(deadline: Optional[float], cap: float) -> float:
    """Return a per-operation timeout that cannot exceed an outer deadline."""
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("codex app-server deadline expired")
    return min(cap, remaining)


# Permission profile mapping mirrors the docstring in PR proposal:
# Hermes' tools.terminal.security_mode → Codex's permissions profile id.
# Defaults if config is missing → workspace-write (matches Codex's own default).
_HERMES_TO_CODEX_PERMISSION_PROFILE = {
    "auto": "workspace-write",
    "approval-required": "read-only-with-approval",
    "unrestricted": "full-access",
    # Backstop alias used by some skills/tests.
    "yolo": "full-access",
}


@dataclass
class TurnResult:
    """Result of one user→assistant→tool turn through the codex app-server."""

    final_text: str = ""
    projected_messages: list[dict] = field(default_factory=list)
    tool_iterations: int = 0
    interrupted: bool = False
    error: Optional[str] = None  # Set if turn ended in a non-recoverable error
    turn_id: Optional[str] = None
    thread_id: Optional[str] = None
    token_usage_last: Optional[dict[str, Any]] = None
    token_usage_total: Optional[dict[str, Any]] = None
    model_context_window: Optional[int] = None
    compacted: bool = False
    # True only when the absolute outer turn deadline expires. Kept separate
    # from user interrupts and watchdog/process failures so callers can
    # withhold intermediate assistant text from terminal success handling.
    timed_out: bool = False
    # Hint to the caller that the underlying codex subprocess is likely
    # wedged (turn-level timeout fired, post-tool watchdog tripped, or
    # token-refresh failure killed the child). The caller should retire
    # the session so the next turn respawns codex from scratch instead
    # of riding a CPU-spinning or auth-broken process. Mirrors openclaw
    # beta.8's "retire timed-out app-server clients" fix.
    should_retire: bool = False


# Markers we accept as terminal even when codex never emits turn/completed.
# Some codex versions stream `<turn_aborted>` as raw text in agentMessage
# items when an interrupt or upstream error tears the turn down before the
# normal completion path fires. Mirrors openclaw beta.8 fix.
_TURN_ABORTED_MARKERS = ("<turn_aborted>", "<turn_aborted/>")


def _notification_scope_ids(
    note: dict,
) -> tuple[Optional[str], Optional[str]]:
    """Extract the thread/turn identity carried by a notification."""
    if not isinstance(note, dict):
        return None, None
    params = note.get("params") or {}
    if not isinstance(params, dict):
        return None, None

    nested_turn = params.get("turn") or {}
    nested_item = params.get("item") or {}

    observed_thread_id = params.get("threadId") or params.get("thread_id")
    if observed_thread_id is None and isinstance(nested_turn, dict):
        observed_thread_id = (
            nested_turn.get("threadId")
            or nested_turn.get("thread_id")
        )
    if observed_thread_id is None and isinstance(nested_item, dict):
        observed_thread_id = (
            nested_item.get("threadId")
            or nested_item.get("thread_id")
        )

    observed_turn_id = params.get("turnId") or params.get("turn_id")
    if observed_turn_id is None and isinstance(nested_turn, dict):
        observed_turn_id = nested_turn.get("id") or nested_turn.get("turnId")
    if observed_turn_id is None and isinstance(nested_item, dict):
        observed_turn_id = (
            nested_item.get("turnId")
            or nested_item.get("turn_id")
        )

    return observed_thread_id, observed_turn_id


def _notification_belongs_to_turn(
    note: dict,
    *,
    thread_id: Optional[str],
    turn_id: Optional[str],
) -> bool:
    """Return whether a multiplexed notification belongs to this turn.

    Codex app-server can carry parent and hosted subagent threads over one
    JSON-RPC connection.  An explicitly foreign child or
    stale-turn event must not mutate the active parent's transcript or mark
    its turn complete.  Unscoped notifications remain accepted for protocol
    compatibility.
    """
    if not isinstance(note, dict):
        return False

    observed_thread_id, observed_turn_id = _notification_scope_ids(note)

    if (
        thread_id is not None
        and observed_thread_id is not None
        and str(observed_thread_id) != str(thread_id)
    ):
        return False

    if (
        turn_id is not None
        and observed_turn_id is not None
        and str(observed_turn_id) != str(turn_id)
    ):
        return False

    return True


def _matching_completed_turn_payload(
    note: dict,
    *,
    thread_id: Optional[str],
    turn_id: Optional[str],
) -> Optional[dict]:
    """Return a strictly scoped ``turn/completed`` payload, or ``None``.

    Unscoped notifications remain compatible for progress/tool events, but a
    completion boundary must prove both the owning thread and the exact turn.
    Otherwise a child/subagent or malformed event could terminate the parent
    turn and promote interim assistant text to success.
    """
    if not isinstance(note, dict) or note.get("method") != "turn/completed":
        return None
    params = note.get("params")
    if not isinstance(params, dict):
        return None
    completed_turn = params.get("turn")
    if not isinstance(completed_turn, dict):
        return None
    observed_thread_id = params.get("threadId")
    observed_turn_id = completed_turn.get("id")
    if (
        thread_id is None
        or turn_id is None
        or observed_thread_id is None
        or observed_turn_id is None
    ):
        return None
    if str(observed_thread_id) != str(thread_id):
        return None
    if str(observed_turn_id) != str(turn_id):
        return None
    return completed_turn


def _coerce_turn_input_text(user_input: Any) -> str:
    """Collapse Hermes/OpenAI rich content into app-server text input.

    The current `turn/start` path sends text items only. TUI image attachment
    can hand us OpenAI-style content parts, so keep the text/path hints and
    replace opaque image payloads with a small marker instead of putting a
    Python list into the `text` field.
    """
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
            elif item_type in {"image", "image_url", "input_image"}:
                parts.append("[image attached]")
        text = "\n\n".join(p for p in parts if p).strip()
        return text or "What do you see in this image?"
    return "" if user_input is None else str(user_input)


# Substrings in codex stderr / JSON-RPC error messages that signal the
# subprocess died because its OAuth credentials are no longer valid.
# Kept conservative: we only redirect users to `codex login` when we're
# reasonably sure that's the actual failure, otherwise we surface the
# original error verbatim. Mirrors openclaw beta.8's auth-refresh
# classification.
_OAUTH_REFRESH_FAILURE_HINTS = (
    "invalid_grant",
    "invalid grant",
    "refresh token",
    "refresh_token",
    "token refresh",
    "token_refresh",
    "token has expired",
    "expired_token",
    "expired token",
    "not authenticated",
    "unauthenticated",
    "unauthorized",
    "401 unauthorized",
    "re-authenticate",
    "reauthenticate",
    "please log in",
    "please login",
    "auth profile",
    "no auth profile",
    "oauth",
)


def _classify_resume_recovery(exc: CodexAppServerError) -> Optional[str]:
    """Classify a resume failure that is safe to replace with a new thread.

    JSON-RPC error codes are too broad for this decision: ``-32600`` can mean
    either a missing rollout (safe to replace) or that another app-server
    process currently owns the thread (not safe to replace).  Match only
    explicit thread/rollout persistence failures and leave auth, required-MCP,
    ownership, and generic configuration failures to the caller.

    The returned category is deliberately identifier-free so it is safe to
    include in logs.  The exception text is used only for classification and
    is never logged from the fallback path.
    """
    parts = [str(getattr(exc, "message", "") or "")]
    data = getattr(exc, "data", None)
    if data is not None:
        parts.append(str(data))
    text = " ".join(parts).lower()
    compact = "".join(ch for ch in text if ch.isalnum())

    # A method lookup failure contains both "thread" and "not found" when
    # the method name is thread/resume; it is a protocol compatibility error,
    # not evidence that the persisted thread is missing.
    if "method not found" in text or "methodnotfound" in compact:
        return None

    if "threadnotfound" in compact or "unknownthread" in compact:
        return "missing"
    if "missing source rollout" in text or "invalid paginated history lineage" in text:
        return "incompatible"

    missing_phrases = (
        "thread not found",
        "thread was not found",
        "missing thread",
        "could not find thread",
        "cannot find thread",
        "thread does not exist",
        "rollout not found",
        "rollout was not found",
        "missing rollout",
        "could not find rollout",
        "cannot find rollout",
        "rollout does not exist",
        "no rollout found",
    )
    if any(phrase in text for phrase in missing_phrases):
        return "missing"

    incompatible_phrases = (
        "incompatible rollout",
        "incompatible thread history",
        "unsupported history",
        "unsupported thread",
        "corrupt rollout",
        "corrupted rollout",
        "failed to deserialize rollout",
        "failed to deserialize thread",
        "failed to deserialize history",
        "failed to parse rollout",
        "cannot load rollout",
        "could not load rollout",
    )
    if any(phrase in text for phrase in incompatible_phrases):
        return "incompatible"

    stale_phrases = (
        "thread archived",
        "archived thread",
        "thread is stale",
        "stale thread",
        "thread expired",
        "expired thread",
        "rollout archived",
        "archived rollout",
        "rollout is stale",
        "stale rollout",
        "rollout expired",
        "expired rollout",
    )
    if any(phrase in text for phrase in stale_phrases):
        return "stale"
    return None


def _classify_oauth_failure(*parts: str) -> Optional[str]:
    """Return a user-friendly re-auth hint if any of the provided strings
    look like a codex OAuth/token-refresh failure; otherwise None.

    Used for both `turn/start` JSON-RPC errors and post-mortem stderr
    inspection when the subprocess exits unexpectedly. Conservative on
    purpose — we only redirect users to `codex login` when the signal
    is strong, so unrelated runtime failures still surface verbatim.
    """
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return None
    for needle in _OAUTH_REFRESH_FAILURE_HINTS:
        if needle in haystack:
            return (
                "Codex authentication failed — your ChatGPT/Codex login "
                "looks expired or invalid. Run `codex login` to refresh, "
                "then retry. (Fall back to default runtime with "
                "`/codex-runtime auto` if the issue persists.)"
            )
    return None


@dataclass
class _ServerRequestRouting:
    """Default policies for codex-side approval requests when no interactive
    callback is wired in. These are only used by tests + cron / non-interactive
    contexts; the live CLI path passes an approval_callback that defers to
    tools.approval.prompt_dangerous_approval()."""

    auto_approve_exec: bool = False
    auto_approve_apply_patch: bool = False


class CodexAppServerSession:
    """One live Codex workspace thread, lifetime owned by AIAgent.

    Not thread-safe — one caller drives it at a time, matching how AIAgent's
    run_conversation() loop is structured today. The codex client itself can
    handle interleaved reads/writes via its own threads, but the adapter's
    state (projector, thread_id, turn counter) is owned by the caller thread.
    """

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        prior_thread_id: Optional[str] = None,
        codex_bin: str = "codex",
        codex_home: Optional[str] = None,
        permission_profile: Optional[str] = None,
        approval_callback: Optional[Callable[..., str]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        request_routing: Optional[_ServerRequestRouting] = None,
        client_factory: Optional[Callable[..., CodexAppServerClient]] = None,
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._prior_thread_id = (
            str(prior_thread_id).strip() if prior_thread_id else None
        )
        self._codex_bin = codex_bin
        self._codex_home = codex_home
        self._permission_profile = (
            permission_profile or _HERMES_TO_CODEX_PERMISSION_PROFILE.get(
                os.environ.get("HERMES_TERMINAL_SECURITY_MODE", "auto"),
                "workspace-write",
            )
        )
        self._approval_callback = approval_callback
        self._on_event = on_event  # Display hook (kawaii spinner ticks etc.)
        self._routing = request_routing or _ServerRequestRouting()
        self._client_factory = client_factory or CodexAppServerClient

        self._client: Optional[CodexAppServerClient] = None
        self._thread_id: Optional[str] = None
        self._interrupt_event = threading.Event()
        self._active_turn_id: Optional[str] = None
        self._active_turn_deadline: Optional[float] = None
        self._turn_generation_counter = 0
        self._active_turn_generation: Optional[int] = None
        self._next_steer_token = 0
        self._steer_reservations: dict[int, dict[str, Any]] = {}
        self._transport_poisoned = False
        self._active_turn_lock = threading.Lock()
        self._active_turn_condition = threading.Condition(self._active_turn_lock)
        # Pending file-change items, keyed by item id. Populated on
        # item/started for fileChange items; consumed by the approval
        # bridge when codex sends item/fileChange/requestApproval. The
        # approval params don't carry the changeset, so we cache here
        # to surface a real summary in the approval prompt (quirk #4).
        self._pending_file_changes: dict[str, str] = {}
        self._closed = False

    # ---------- lifecycle ----------

    def ensure_started(self, *, startup_timeout: Optional[float] = None) -> str:
        """Spawn, initialize, and start or resume a Codex thread.

        ``startup_timeout`` bounds the entire handshake, including both
        initialize and any resume-to-fresh fallback. Repeated calls are
        idempotent and return the active thread immediately.
        """
        if self._thread_id is not None:
            client = self._client
            is_alive = getattr(client, "is_alive", None) if client is not None else None
            client_alive = bool(is_alive()) if callable(is_alive) else not bool(
                getattr(client, "_closed", True)
            )
            if (
                client is None
                or bool(getattr(client, "poisoned", False))
                or not client_alive
            ):
                raise RuntimeError("codex app-server client is closed or poisoned")
            return self._thread_id
        startup_deadline = (
            None
            if startup_timeout is None
            else time.monotonic() + max(0.0, startup_timeout)
        )
        if self._client is None:
            self._client = self._client_factory(
                codex_bin=self._codex_bin,
                codex_home=self._codex_home,
                cwd=self._cwd,
            )
        self._client.initialize(
            client_name="hermes",
            client_title="Hermes Agent",
            client_version=_get_hermes_version(),
            timeout=_remaining_timeout(startup_deadline, 10.0),
        )
        # Permission selection is intentionally NOT sent on thread/start or
        # thread/resume.
        # Two reasons (live-tested against codex 0.130.0):
        #   1. `thread/start.permissions` is gated behind the experimentalApi
        #      capability on this codex version — we'd have to opt in during
        #      initialize and accept the unstable surface.
        #   2. Even with experimentalApi declared and the correct shape
        #      (`{"type": "profile", "id": "..."}`, not `{"profileId": ...}`),
        #      codex requires a matching `[permissions]` table in
        #      ~/.codex/config.toml or it fails the request with
        #      'default_permissions requires a [permissions] table'.
        # Letting codex pick its default (`:read-only` unless the user has
        # configured otherwise in their codex config.toml) is the standard
        # codex CLI workflow and avoids fighting codex's own validation.
        # Users who want a write-capable profile configure it in their
        # ~/.codex/config.toml the same way they would for any codex usage.
        # ``cwd`` is the only Hermes-owned thread runtime override today.
        # Codex restores the stored model/reasoning/policy on resume; explicit
        # overrides are intentionally kept identical to thread/start so the
        # resumed thread cannot silently run in its historical repository.
        runtime_params: dict[str, Any] = {"cwd": self._cwd}
        method = "thread/start"
        params = runtime_params
        resumed = False
        if self._prior_thread_id is not None:
            method = "thread/resume"
            params = {
                "threadId": self._prior_thread_id,
                **runtime_params,
            }
            try:
                result = self._client.request(
                    method,
                    params,
                    timeout=_remaining_timeout(startup_deadline, 15.0),
                )
                resumed = True
            except CodexAppServerError as exc:
                recovery = _classify_resume_recovery(exc)
                if recovery is None:
                    raise
                logger.warning(
                    "codex app-server prior thread is %s; starting a fresh "
                    "thread",
                    recovery,
                )
                self._prior_thread_id = None
                method = "thread/start"
                params = runtime_params
                result = self._client.request(
                    method,
                    params,
                    timeout=_remaining_timeout(startup_deadline, 15.0),
                )
        else:
            result = self._client.request(
                method,
                params,
                timeout=_remaining_timeout(startup_deadline, 15.0),
            )
        # Cross-fill thread.id/sessionId — different codex versions have
        # serialized this under either key. Mirrors openclaw beta.8's
        # tolerance fix so future codex drops/renames don't KeyError us
        # at handshake time.
        thread_obj = result.get("thread") or {}
        thread_id = (
            thread_obj.get("id")
            or thread_obj.get("sessionId")
            or result.get("sessionId")
            or result.get("threadId")
        )
        if not thread_id:
            raise CodexAppServerError(
                code=-32603,
                message=(
                    f"codex {method} returned no thread id "
                    f"(payload keys: {sorted(result.keys())})"
                ),
            )
        self._thread_id = thread_id
        logger.info(
            "codex app-server thread %s: profile=%s cwd=%s",
            "resumed" if resumed else "started",
            self._permission_profile,
            self._cwd,
        )
        return self._thread_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._active_turn_lock:
            self._active_turn_id = None
            self._active_turn_deadline = None
            self._active_turn_generation = None
            self._steer_reservations.clear()
            self._active_turn_condition.notify_all()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._client = None
        self._thread_id = None

    def __enter__(self) -> "CodexAppServerSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- interrupt ----------

    def request_interrupt(self) -> None:
        """Idempotent: signal the active turn loop to issue turn/interrupt
        and unwind. Called by AIAgent's _interrupt_requested path."""
        self._interrupt_event.set()

    def request_steer(self, text: str) -> bool:
        """Append user guidance to the active Codex turn via ``turn/steer``."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        with self._active_turn_condition:
            turn_id = self._active_turn_id
            thread_id = self._thread_id
            client = self._client
            deadline = self._active_turn_deadline
            generation = self._active_turn_generation
            now = time.monotonic()
            if (
                not turn_id
                or not thread_id
                or client is None
                or deadline is None
                or generation is None
                or now >= deadline
            ):
                return False
            request_timeout = min(10.0, deadline - now)
            self._next_steer_token += 1
            token = self._next_steer_token
            self._steer_reservations[token] = {
                "generation": generation,
                "text": cleaned,
                "status": "pending",
                "expires_at": now + request_timeout,
            }

        try:
            response = client.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": cleaned}],
                    "expectedTurnId": turn_id,
                },
                timeout=request_timeout,
            )
        except (CodexAppServerError, TimeoutError) as exc:
            with self._active_turn_condition:
                reservation = self._steer_reservations.get(token)
                if (
                    reservation is not None
                    and reservation.get("generation") == generation
                ):
                    if (
                        isinstance(exc, TimeoutError)
                        or bool(getattr(client, "poisoned", False))
                        or (
                            isinstance(exc, CodexAppServerError)
                            and exc.code == -32098
                        )
                    ):
                        # The RPC may have reached Codex even though its reply
                        # did not. Preserve a matching user item rather than
                        # discarding a potentially accepted steer.
                        reservation["status"] = "indeterminate"
                        reservation["expires_at"] = time.monotonic()
                    else:
                        self._steer_reservations.pop(token, None)
                if bool(getattr(client, "poisoned", False)):
                    self._transport_poisoned = True
                self._active_turn_condition.notify_all()
            logger.debug(
                "turn/steer rejected for active Codex turn: error_type=%s",
                type(exc).__name__,
            )
            return False
        accepted_turn_id = response.get("turnId") if isinstance(response, dict) else None
        accepted = accepted_turn_id in {None, turn_id}
        with self._active_turn_condition:
            reservation = self._steer_reservations.get(token)
            still_active = (
                self._active_turn_generation == generation
                and self._active_turn_id == turn_id
            )
            if (
                reservation is not None
                and reservation.get("generation") == generation
            ):
                if accepted:
                    reservation["status"] = "confirmed"
                else:
                    self._steer_reservations.pop(token, None)
            self._active_turn_condition.notify_all()
        return accepted and still_active and reservation is not None

    # ---------- diagnostics ----------

    def _format_error_with_stderr(
        self,
        prefix: str,
        exc: Any = "",
        *,
        tail_lines: int = _STDERR_TAIL_LINES,
    ) -> str:
        """Build a user-facing error string for codex failures.

        Appends the last few lines of codex's stderr buffer when available,
        passed through agent.redact with force=True so secrets in provider
        error responses (auth headers, query-string tokens, sk-* keys) never
        leak into chat output or trajectories. The codex CLI's own error
        text ('Internal error', 'turn/start failed: ...') is otherwise
        opaque and forces users to re-run with verbose flags to diagnose
        config / provider / auth-bridge problems.

        Use this for the generic / catch-all branches. Specific
        classifications (OAuth via _classify_oauth_failure, post-tool wedge
        watchdog) already produce a clean hint and should be used instead.
        """
        exc_str = str(exc) if exc != "" and exc is not None else ""
        base = f"{prefix}: {exc_str}" if exc_str else prefix

        def _without_thread_ids(text: str) -> str:
            for internal_id in (self._thread_id, self._prior_thread_id):
                # Real Codex identifiers are opaque, non-trivial strings.
                # Ignore tiny test/protocol sentinels (for example ``"t"``),
                # which would otherwise corrupt every matching character in
                # the surrounding diagnostic.
                if internal_id and len(str(internal_id)) >= 8:
                    text = text.replace(str(internal_id), "[internal thread]")
            return text

        if self._client is None:
            return _without_thread_ids(base)
        try:
            tail = self._client.stderr_tail(tail_lines)
        except Exception:  # pragma: no cover - diagnostic best-effort
            return _without_thread_ids(base)
        if not tail:
            return _without_thread_ids(base)
        joined = "\n".join(line.rstrip() for line in tail if line)
        if not joined.strip():
            return _without_thread_ids(base)
        redacted = redact_sensitive_text(joined, force=True)
        return _without_thread_ids(
            f"{base}\ncodex stderr (last {len(tail)} lines):\n{redacted}"
        )

    def _format_startup_error(self, exc: Any) -> str:
        """Format startup failures without exposing a persisted thread id."""
        if self._prior_thread_id and isinstance(exc, CodexAppServerError):
            return (
                "codex app-server startup failed: prior thread resume was "
                f"rejected (JSON-RPC code {exc.code})"
            )
        return self._format_error_with_stderr(
            "codex app-server startup failed", exc
        )

    # ---------- per-turn ----------

    def run_turn(
        self,
        user_input: Any,
        *,
        turn_timeout: float = 600.0,
        notification_poll_timeout: float = 0.25,
        post_tool_quiet_timeout: float = 90.0,
        turn_deadline: Optional[float] = None,
    ) -> TurnResult:
        """Send a user message and block until turn/completed, while
        forwarding server-initiated approval requests and projecting items
        into Hermes' messages shape.

        post_tool_quiet_timeout: if codex emits a tool completion and then
        goes quiet for this many seconds without emitting another item or
        `turn/completed`, fast-fail and mark the session for retirement.
        Mirrors openclaw beta.8's post-tool completion watchdog (#81697)
        so a wedged codex doesn't burn the full turn deadline.
        """
        deadline = (
            turn_deadline
            if turn_deadline is not None
            else time.monotonic() + turn_timeout
        )
        # Pre-create the result so startup failures (codex subprocess can't
        # spawn, initialize handshake rejects, thread/start blows up) surface
        # the same way per-turn failures do — with a TurnResult.error string
        # the caller can render — instead of bubbling raw codex exceptions
        # up to AIAgent.run_conversation.
        result = TurnResult()
        try:
            self.ensure_started(
                startup_timeout=_remaining_timeout(deadline, turn_timeout)
            )
        except TimeoutError:
            result.interrupted = True
            result.timed_out = True
            result.error = f"turn timed out after {turn_timeout}s"
            result.should_retire = True
            self._interrupt_event.clear()
            return result
        except CodexAppServerError as exc:
            result.error = self._format_startup_error(exc)
            # Subprocess almost certainly unhealthy — retire so the next
            # turn re-spawns cleanly.
            result.should_retire = True
            self._interrupt_event.clear()
            return result
        except RuntimeError as exc:
            stderr_blob = "\n".join(
                self._client.stderr_tail(40) if self._client is not None else []
            )
            result.error = _classify_oauth_failure(str(exc), stderr_blob) or (
                self._format_error_with_stderr(
                    "codex app-server startup failed", exc
                )
            )
            result.should_retire = True
            self._interrupt_event.clear()
            return result
        assert self._client is not None and self._thread_id is not None
        result.thread_id = self._thread_id

        # Do not clear here: a hard stop can arrive while ensure_started() is
        # spawning/initializing the subprocess. Honor it before launching a
        # Codex turn instead of erasing the signal.
        if self._interrupt_event.is_set():
            result.interrupted = True
            self._interrupt_event.clear()
            return result
        projector = CodexEventProjector()

        user_input_text = _coerce_turn_input_text(user_input)

        # Send turn/start with the user input. Text-only for now (codex
        # supports rich content but Hermes' text path is the common case).
        try:
            ts = self._client.request(
                "turn/start",
                {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": user_input_text}],
                },
                timeout=_remaining_timeout(deadline, 10.0),
            )
        except CodexAppServerError as exc:
            # Classify auth/refresh failures so the user gets a clear
            # `codex login` pointer instead of a raw RPC error string.
            stderr_blob = "\n".join(self._client.stderr_tail(40))
            hint = _classify_oauth_failure(exc.message, stderr_blob)
            if hint is not None:
                result.error = hint
                # Subprocess is fine on a JSON-RPC level here, but the
                # token store is broken — retire so the next turn does a
                # clean handshake (and the user has a chance to re-auth
                # via `codex login` between turns).
                result.should_retire = True
            else:
                result.error = self._format_error_with_stderr(
                    "turn/start failed", exc
                )
            self._interrupt_event.clear()
            return result
        except TimeoutError as exc:
            # Startup, turn/start, and the notification loop share one
            # absolute deadline. Do not reset the budget at this boundary.
            stderr_blob = "\n".join(self._client.stderr_tail(40))
            hint = _classify_oauth_failure(stderr_blob)
            result.interrupted = True
            result.timed_out = True
            result.error = hint or self._format_error_with_stderr(
                "turn/start timed out before the turn deadline", exc
            )
            result.should_retire = True
            self._interrupt_event.clear()
            return result

        result.turn_id = (ts.get("turn") or {}).get("id")
        with self._active_turn_condition:
            self._turn_generation_counter += 1
            turn_generation = self._turn_generation_counter
            self._active_turn_id = result.turn_id
            self._active_turn_deadline = deadline
            self._active_turn_generation = turn_generation
            self._active_turn_condition.notify_all()
        turn_complete = False
        deadline_expired = False
        # Post-tool watchdog state. last_tool_completion_at is set whenever
        # a tool-shaped item completes; if no further notification arrives
        # within post_tool_quiet_timeout and the turn hasn't completed, we
        # fast-fail and retire the session.
        last_tool_completion_at: Optional[float] = None
        matching_user_projections: list[dict] = []

        def _observe_user_projection(
            notification: dict, projected_messages: list[dict]
        ) -> None:
            """Collect same-text user items for end-of-turn reconciliation.

            Codex may omit the initial turn/start echo, and an accepted steer
            can repeat the initial text before model output. Marking the first
            matching item immediately would then discard a durable steer. At
            turn end we suppress one item only when observed same-text items
            outnumber accepted same-text steers.
            """
            if notification.get("method") != "item/completed":
                return
            item = ((notification.get("params") or {}).get("item") or {})
            if item.get("type") != "userMessage":
                return
            matching_user_projections.extend(
                message
                for message in projected_messages
                if message.get("role") == "user"
                and message.get("content") == user_input_text
            )

        def _record_completed_turn(notification: dict) -> bool:
            """Validate and record one terminal completion notification."""
            nonlocal turn_complete
            assert self._client is not None
            completed_turn = _matching_completed_turn_payload(
                notification,
                thread_id=self._thread_id,
                turn_id=result.turn_id,
            )
            if completed_turn is None:
                return False

            turn_complete = True
            turn_status = str(completed_turn.get("status") or "").strip()
            if turn_status == "completed":
                return True

            result.interrupted = result.interrupted or turn_status == "interrupted"
            err_obj = completed_turn.get("error")
            err_msg = (
                _format_responses_error(err_obj, turn_status or "unknown")
                if err_obj
                else f"turn ended status={turn_status or 'unknown'}"
            )
            stderr_blob = "\n".join(self._client.stderr_tail(40))
            hint = _classify_oauth_failure(err_msg, stderr_blob)
            if hint is not None:
                result.error = hint
                result.should_retire = True
            else:
                result.error = self._format_error_with_stderr(
                    f"turn ended status={turn_status or 'unknown'}",
                    err_msg if err_obj else None,
                )
            return True

        while time.monotonic() < deadline and not turn_complete:
            if self._interrupt_event.is_set():
                self._issue_interrupt(result.turn_id, deadline=deadline)
                result.interrupted = True
                break

            # Detect a dead subprocess between iterations. If codex exited
            # (e.g. crashed, segfaulted, or its auth refresh thread killed
            # the process), we won't get any more notifications — bail out
            # rather than waiting for the full turn deadline.
            if not self._client.is_alive():
                stderr_blob = "\n".join(self._client.stderr_tail(60))
                hint = _classify_oauth_failure(stderr_blob)
                if hint is not None:
                    result.error = hint
                else:
                    result.error = self._format_error_with_stderr(
                        "codex app-server subprocess exited unexpectedly",
                        tail_lines=20,
                    )
                result.should_retire = True
                break

            # Post-tool watchdog: if a tool completion was the most recent
            # signal and codex has been silent past the quiet timeout, give
            # up on this turn instead of waiting for the outer deadline.
            if (
                last_tool_completion_at is not None
                and (time.monotonic() - last_tool_completion_at)
                    > post_tool_quiet_timeout
            ):
                self._issue_interrupt(result.turn_id, deadline=deadline)
                result.interrupted = True
                result.error = (
                    f"codex went silent for "
                    f"{post_tool_quiet_timeout:.0f}s after a tool result; "
                    f"retiring app-server session."
                )
                result.should_retire = True
                break

            # Drain any server-initiated requests (approvals) before
            # reading notifications, so the codex side isn't blocked.
            sreq = self._client.take_server_request(timeout=0)
            if sreq is not None:
                # Drain any pending notifications first so per-turn state
                # (e.g. _pending_file_changes for fileChange approvals) is
                # up to date when we make the approval decision. Bounded
                # to avoid starving the server-request response.
                for _ in range(8):
                    pending = self._client.take_notification(timeout=0)
                    if pending is None:
                        break
                    if pending.get("method") == "turn/completed":
                        if not _record_completed_turn(pending):
                            logger.debug(
                                "ignoring unscoped or foreign turn/completed "
                                "while draining server request"
                            )
                        continue
                    if not _notification_belongs_to_turn(
                        pending,
                        thread_id=self._thread_id,
                        turn_id=result.turn_id,
                    ):
                        logger.debug(
                            "ignoring foreign codex notification while draining "
                            "server request: method=%s",
                            pending.get("method"),
                        )
                        continue
                    # Mirror the main notification-handling block below so
                    # display events surface and stay in step with projector
                    # state. Without this, item/started / item/completed
                    # events drained as part of the approval-roundtrip
                    # preamble are projected into messages but never reach
                    # the tool-progress display, silently hiding tool
                    # bubbles around approvals.
                    if self._on_event is not None:
                        try:
                            self._on_event(pending)
                        except Exception:  # pragma: no cover - display callback
                            logger.debug(
                                "on_event callback raised", exc_info=True
                            )
                    _apply_token_usage_notification(result, pending)
                    _apply_compaction_notification(result, pending)
                    self._track_pending_file_change(pending)
                    proj = projector.project(pending)
                    _observe_user_projection(pending, proj.messages)
                    if proj.messages:
                        result.projected_messages.extend(proj.messages)
                    if proj.is_tool_iteration:
                        result.tool_iterations += 1
                        last_tool_completion_at = time.monotonic()
                    if proj.final_text is not None:
                        result.final_text = proj.final_text
                        if _has_turn_aborted_marker(proj.final_text):
                            turn_complete = True
                            result.interrupted = True
                            result.error = (
                                result.error
                                or "codex reported turn_aborted"
                            )
                try:
                    self._handle_server_request(sreq, deadline=deadline)
                except (TimeoutError, RuntimeError, CodexAppServerError) as exc:
                    result.interrupted = True
                    result.timed_out = time.monotonic() >= deadline
                    result.error = self._format_error_with_stderr(
                        "codex approval response failed", exc
                    )
                    result.should_retire = True
                    break
                # Activity counts as live signal — reset the post-tool
                # quiet timer so an approval round-trip doesn't trip it.
                last_tool_completion_at = None
                continue

            remaining = max(0.0, deadline - time.monotonic())
            note = self._client.take_notification(
                timeout=min(notification_poll_timeout, remaining)
            )
            if note is None:
                continue

            method = note.get("method", "")
            if method == "turn/completed":
                if not _record_completed_turn(note):
                    logger.debug(
                        "ignoring unscoped or foreign turn/completed notification"
                    )
                    continue
            elif not _notification_belongs_to_turn(
                note,
                thread_id=self._thread_id,
                turn_id=result.turn_id,
            ):
                logger.debug(
                    "ignoring foreign codex notification: method=%s", method
                )
                continue

            if self._on_event is not None:
                try:
                    self._on_event(note)
                except Exception:  # pragma: no cover - display callback
                    logger.debug("on_event callback raised", exc_info=True)

            _apply_token_usage_notification(result, note)
            _apply_compaction_notification(result, note)

            # Track in-progress fileChange items so the approval bridge
            # can surface a real change summary when codex requests
            # approval (the approval params themselves don't carry the
            # changeset). Quirk #4 fix.
            self._track_pending_file_change(note)

            # Project into messages
            projection = projector.project(note)
            _observe_user_projection(note, projection.messages)
            if projection.messages:
                result.projected_messages.extend(projection.messages)
            if projection.is_tool_iteration:
                result.tool_iterations += 1
                # Arm/refresh the post-tool quiet watchdog whenever a
                # tool-shaped item completes.
                last_tool_completion_at = time.monotonic()
            else:
                # Any non-tool projected activity (assistant message,
                # status update, etc.) means codex is still producing
                # output — clear the quiet timer so we don't fast-fail.
                if projection.messages or projection.final_text is not None:
                    last_tool_completion_at = None
            if projection.final_text is not None:
                # Codex can emit multiple agentMessage items in one turn
                # (e.g. partial then final). Take the last one as canonical.
                result.final_text = projection.final_text
                # Some codex builds tear a turn down by emitting a
                # `<turn_aborted>` marker in the agent message text and
                # never sending turn/completed. Treat the marker itself
                # as terminal so we don't burn the full deadline.
                if _has_turn_aborted_marker(projection.final_text):
                    turn_complete = True
                    result.interrupted = True
                    result.error = (
                        result.error or "codex reported turn_aborted"
                    )

        else:
            # The loop condition can become false because either the matching
            # turn completed or the absolute deadline elapsed. Early process,
            # watchdog, and user-interrupt exits use ``break`` and skip this
            # clause, so timeout reporting never relies on error-text parsing.
            deadline_expired = not turn_complete

        if deadline_expired:
            # Hit the deadline. Issue interrupt to stop wasted compute, and
            # tell the caller to retire the session — a turn that never
            # finished is a strong sign codex is wedged in a way the next
            # turn shouldn't inherit.
            self._issue_interrupt(result.turn_id, deadline=deadline)
            result.interrupted = True
            result.timed_out = True
            result.error = self._format_error_with_stderr(
                f"turn timed out after {turn_timeout}s"
            )
            result.should_retire = True
        elif not turn_complete and not result.interrupted:
            # An early non-deadline failure (for example, a dead subprocess)
            # still invalidates this turn/session but keeps its more specific
            # redacted diagnostic instead of being mislabeled as a timeout.
            self._issue_interrupt(result.turn_id, deadline=deadline)
            result.interrupted = True
            result.should_retire = True

        with self._active_turn_condition:
            # A completion notification can race a turn/steer response. Wait
            # only until each steer RPC's own bounded expiry, then classify any
            # still-pending request as indeterminate rather than letting a late
            # response mutate a later turn.
            while True:
                pending = [
                    reservation
                    for reservation in self._steer_reservations.values()
                    if reservation.get("generation") == turn_generation
                    and reservation.get("status") == "pending"
                ]
                if not pending:
                    break
                now = time.monotonic()
                next_expiry = min(
                    float(reservation.get("expires_at", now))
                    for reservation in pending
                )
                if next_expiry <= now:
                    for reservation in pending:
                        if float(reservation.get("expires_at", now)) <= now:
                            reservation["status"] = "indeterminate"
                    continue
                self._active_turn_condition.wait(timeout=next_expiry - now)

            accepted_same_text_steers = sum(
                1
                for reservation in self._steer_reservations.values()
                if reservation.get("generation") == turn_generation
                and reservation.get("text") == user_input_text
                and reservation.get("status") in {"confirmed", "indeterminate"}
            )
            if len(matching_user_projections) > accepted_same_text_steers:
                matching_user_projections[0][_INITIAL_USER_ECHO_MARKER] = True
            self._steer_reservations = {
                token: reservation
                for token, reservation in self._steer_reservations.items()
                if reservation.get("generation") != turn_generation
            }
            if self._active_turn_generation == turn_generation:
                self._active_turn_id = None
                self._active_turn_deadline = None
                self._active_turn_generation = None
            self._active_turn_condition.notify_all()
            if self._transport_poisoned or bool(
                getattr(self._client, "poisoned", False)
            ):
                result.should_retire = True
        self._interrupt_event.clear()
        return result

    def compact_thread(
        self,
        *,
        turn_timeout: float = 600.0,
        notification_poll_timeout: float = 0.25,
    ) -> TurnResult:
        """Trigger Codex-native history compaction for the current thread.

        `thread/compact/start` returns immediately; the actual compaction
        progress streams through the same turn/item notifications as a normal
        turn. We wait for the matching `turn/completed` so callers can treat a
        successful return as a completed compaction boundary.
        """
        result = TurnResult()
        try:
            self.ensure_started()
        except (CodexAppServerError, TimeoutError) as exc:
            result.error = self._format_error_with_stderr(
                "codex app-server startup failed", exc
            )
            result.should_retire = True
            return result

        assert self._client is not None and self._thread_id is not None
        result.thread_id = self._thread_id
        self._interrupt_event.clear()
        projector = CodexEventProjector()

        try:
            self._client.request(
                "thread/compact/start",
                {"threadId": self._thread_id},
                timeout=10,
            )
        except CodexAppServerError as exc:
            stderr_blob = "\n".join(self._client.stderr_tail(40))
            hint = _classify_oauth_failure(exc.message, stderr_blob)
            if hint is not None:
                result.error = hint
                result.should_retire = True
            else:
                result.error = self._format_error_with_stderr(
                    "thread/compact/start failed", exc
                )
            return result
        except TimeoutError as exc:
            stderr_blob = "\n".join(self._client.stderr_tail(40))
            hint = _classify_oauth_failure(stderr_blob)
            result.error = hint or self._format_error_with_stderr(
                "thread/compact/start timed out", exc
            )
            result.should_retire = True
            return result

        deadline = time.monotonic() + turn_timeout
        turn_complete = False

        while time.monotonic() < deadline and not turn_complete:
            if self._interrupt_event.is_set():
                self._issue_interrupt(result.turn_id, deadline=deadline)
                result.interrupted = True
                break

            if not self._client.is_alive():
                stderr_blob = "\n".join(self._client.stderr_tail(60))
                hint = _classify_oauth_failure(stderr_blob)
                if hint is not None:
                    result.error = hint
                else:
                    result.error = self._format_error_with_stderr(
                        "codex app-server subprocess exited unexpectedly",
                        tail_lines=20,
                    )
                result.should_retire = True
                break

            sreq = self._client.take_server_request(timeout=0)
            if sreq is not None:
                try:
                    self._handle_server_request(sreq, deadline=deadline)
                except (TimeoutError, RuntimeError, CodexAppServerError) as exc:
                    result.interrupted = True
                    result.timed_out = time.monotonic() >= deadline
                    result.error = self._format_error_with_stderr(
                        "codex compact approval response failed", exc
                    )
                    result.should_retire = True
                    break
                continue

            remaining = max(0.0, deadline - time.monotonic())
            note = self._client.take_notification(
                timeout=min(notification_poll_timeout, remaining)
            )
            if note is None:
                continue

            method = note.get("method", "")
            observed_thread_id, observed_turn_id = _notification_scope_ids(note)
            if result.turn_id is None:
                if method == "turn/started":
                    if (
                        observed_thread_id is not None
                        and str(observed_thread_id) != str(self._thread_id)
                    ):
                        logger.debug(
                            "ignoring foreign compact turn/started"
                        )
                        continue
                    if observed_turn_id is None:
                        logger.debug(
                            "ignoring compact turn/started without a turn id"
                        )
                        continue
                    result.turn_id = str(observed_turn_id)
                elif observed_turn_id is not None or method in {
                    "item/completed",
                    "turn/completed",
                }:
                    # thread/compact/start does not return a turn id. Until the
                    # new turn/started arrives, any terminal/projectable event
                    # is stale or cannot be safely attributed to this compaction.
                    logger.debug(
                        "ignoring codex notification before compact turn start: "
                        "method=%s",
                        method,
                    )
                    continue

            if not _notification_belongs_to_turn(
                note,
                thread_id=self._thread_id,
                turn_id=result.turn_id,
            ):
                logger.debug(
                    "ignoring foreign codex notification: method=%s", method
                )
                continue

            if self._on_event is not None:
                try:
                    self._on_event(note)
                except Exception:  # pragma: no cover - display callback
                    logger.debug("on_event callback raised", exc_info=True)

            _apply_token_usage_notification(result, note)
            _apply_compaction_notification(result, note)
            self._track_pending_file_change(note)

            projection = projector.project(note)
            if projection.messages:
                result.projected_messages.extend(projection.messages)
            if projection.is_tool_iteration:
                result.tool_iterations += 1
            if projection.final_text is not None:
                result.final_text = projection.final_text
                if _has_turn_aborted_marker(projection.final_text):
                    turn_complete = True
                    result.interrupted = True
                    result.error = (
                        result.error or "codex reported turn_aborted"
                    )

            if method == "turn/started":
                turn_obj = (note.get("params") or {}).get("turn") or {}
                result.turn_id = turn_obj.get("id") or result.turn_id
            elif method == "turn/completed":
                turn_complete = True
                turn_obj = (note.get("params") or {}).get("turn") or {}
                result.turn_id = turn_obj.get("id") or result.turn_id
                turn_status = turn_obj.get("status")
                if turn_status == "interrupted":
                    result.interrupted = True
                    result.error = result.error or "compact turn interrupted"
                elif turn_status and turn_status != "completed":
                    err_obj = turn_obj.get("error")
                    err_msg = _format_responses_error(err_obj, str(turn_status))
                    stderr_blob = "\n".join(self._client.stderr_tail(40))
                    hint = _classify_oauth_failure(err_msg, stderr_blob)
                    if hint is not None:
                        result.error = hint
                        result.should_retire = True
                    else:
                        result.error = self._format_error_with_stderr(
                            f"compact turn ended status={turn_status}",
                            err_msg,
                        )

        if not turn_complete and not result.interrupted:
            self._issue_interrupt(result.turn_id, deadline=deadline)
            result.interrupted = True
            if not result.error:
                result.error = self._format_error_with_stderr(
                    f"compact turn timed out after {turn_timeout}s"
                )
            result.should_retire = True

        return result

    # ---------- internals ----------

    def _issue_interrupt(
        self,
        turn_id: Optional[str],
        *,
        deadline: Optional[float] = None,
    ) -> None:
        if self._client is None or self._thread_id is None or turn_id is None:
            return
        timeout = (
            5.0
            if deadline is None
            else min(5.0, max(0.0, deadline - time.monotonic()))
        )
        if timeout <= 0:
            return
        try:
            self._client.request(
                "turn/interrupt",
                {"threadId": self._thread_id, "turnId": turn_id},
                timeout=timeout,
            )
        except CodexAppServerError as exc:
            # "no active turn to interrupt" is fine — already done.
            logger.debug(
                "turn/interrupt non-fatal: error_type=%s code=%s",
                type(exc).__name__,
                exc.code,
            )
        except TimeoutError:
            logger.warning("turn/interrupt timed out")

    def _call_approval_callback(
        self,
        command: str,
        description: str,
        *,
        timeout: Optional[float],
    ) -> Any:
        """Run the potentially interactive callback inside the turn budget."""
        callback = self._approval_callback
        if callback is None:
            return None
        if timeout is None:
            return callback(command, description, allow_permanent=False)
        if timeout <= 0:
            raise TimeoutError("approval deadline expired")

        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                value = callback(command, description, allow_permanent=False)
                outcome.put((True, value))
            except Exception as exc:  # pragma: no cover - reraised in caller
                outcome.put((False, exc))

        threading.Thread(
            target=invoke,
            name="codex-approval-callback",
            daemon=True,
        ).start()
        try:
            ok, value = outcome.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("approval deadline expired") from exc
        if not ok:
            if isinstance(value, BaseException):
                raise value
            raise RuntimeError("approval callback failed")
        return value

    def _handle_server_request(
        self,
        req: dict,
        *,
        deadline: Optional[float] = None,
    ) -> None:
        """Translate a codex server request (approval) into Hermes' approval
        flow, then send the response.

        Method names verified live against codex 0.130.0 (Apr 2026):
          item/commandExecution/requestApproval — exec approvals
          item/fileChange/requestApproval       — apply_patch approvals
          item/permissions/requestApproval      — permissions changes
                                                  (we decline; user controls
                                                  permission profile in
                                                  ~/.codex/config.toml).
        """
        if self._client is None:
            return
        method = req.get("method", "")
        rid = req.get("id")
        params = req.get("params") or {}
        approval_timeout = (
            None if deadline is None else max(0.0, deadline - time.monotonic())
        )

        def _write_timeout() -> float:
            return (
                5.0
                if deadline is None
                else _remaining_timeout(deadline, 5.0)
            )

        if method == "item/commandExecution/requestApproval":
            decision = self._decide_exec_approval(
                params, timeout=approval_timeout
            )
            self._client.respond(
                rid, {"decision": decision}, timeout=_write_timeout()
            )
        elif method == "item/fileChange/requestApproval":
            decision = self._decide_apply_patch_approval(
                params, timeout=approval_timeout
            )
            self._client.respond(
                rid, {"decision": decision}, timeout=_write_timeout()
            )
        elif method == "item/permissions/requestApproval":
            # Codex sometimes asks to escalate permissions mid-turn. We
            # always decline — the user already chose their permission
            # profile in ~/.codex/config.toml and surprise escalations
            # shouldn't be silently accepted.
            self._client.respond(
                rid, {"decision": "decline"}, timeout=_write_timeout()
            )
        elif method == "mcpServer/elicitation/request":
            # Codex's MCP layer asks the user for structured input on
            # behalf of an MCP server (e.g. tool-call confirmation,
            # OAuth, form data). For our own hermes-tools callback we
            # auto-accept — the user already approved Hermes' tools
            # by enabling the runtime, and we never expose anything
            # codex's built-in shell can't already do. For other MCP
            # servers we decline so the user explicitly opts in via
            # codex's own auth flow.
            server_name = params.get("serverName") or ""
            if server_name == "hermes-tools":
                self._client.respond(
                    rid,
                    {"action": "accept", "content": None, "_meta": None},
                    timeout=_write_timeout(),
                )
            else:
                self._client.respond(
                    rid,
                    {"action": "decline", "content": None, "_meta": None},
                    timeout=_write_timeout(),
                )
        else:
            # Unknown server request — codex can extend this surface. Reject
            # cleanly so codex doesn't hang waiting for us.
            logger.warning("Unknown codex server request: %s", method)
            self._client.respond_error(
                rid,
                code=-32601,
                message=f"Unsupported method: {method}",
                timeout=_write_timeout(),
            )

    def _decide_exec_approval(
        self,
        params: dict,
        *,
        timeout: Optional[float] = None,
    ) -> str:
        """Decide a Codex exec approval request.

        This is protocol-level routing only — it carries NO Hermes
        approval-mode/timeout logic. The Hermes-side resolution happens
        upstream: ``agent/codex_runtime.py`` derives
        ``auto_approve_exec`` from the canonical
        ``tools.approval.is_approval_bypass_active()`` (which reads
        ``approvals.mode`` via ``tools.approval._get_approval_mode``),
        and ``self._approval_callback`` itself runs the shared approval
        gate (mode + ``approvals.timeout``) in ``tools/approval.py``.
        Keep it that way — do not re-read approval config here.
        """
        if self._routing.auto_approve_exec:
            return "accept"
        command = params.get("command") or ""
        # Codex's CommandExecutionRequestApprovalParams has cwd as Optional —
        # fall back to the session's cwd when codex doesn't include it so the
        # approval prompt is never empty (quirk #10 fix).
        cwd = params.get("cwd") or self._cwd or "<unknown>"
        reason = params.get("reason")
        description = f"Codex requests exec in {cwd}"
        if reason:
            description += f" — {reason}"
        if self._approval_callback is not None:
            try:
                choice = self._call_approval_callback(
                    command,
                    description,
                    timeout=timeout,
                )
                return _approval_choice_to_codex_decision(choice)
            except TimeoutError:
                logger.warning("approval_callback timed out on exec request")
                return "decline"
            except Exception:
                logger.exception("approval_callback raised on exec request")
                return "decline"
        return "decline"  # fail-closed when no callback wired

    def _decide_apply_patch_approval(
        self,
        params: dict,
        *,
        timeout: Optional[float] = None,
    ) -> str:
        """Decide a Codex apply_patch approval request.

        Protocol-level routing only; Hermes approval-mode/timeout
        resolution is delegated to ``tools/approval.py`` upstream — see
        the docstring on ``_decide_exec_approval``.
        """
        if self._routing.auto_approve_apply_patch:
            return "accept"
        if self._approval_callback is not None:
            # FileChangeRequestApprovalParams gives us reason + grantRoot.
            # The actual changeset lives on the corresponding fileChange
            # item which the projector has already cached for us — look it
            # up by item_id so the user sees what's actually changing.
            reason = params.get("reason")
            grant_root = params.get("grantRoot")
            item_id = params.get("itemId") or ""
            change_summary = self._lookup_pending_file_change(item_id)
            description_parts = []
            if reason:
                description_parts.append(reason)
            if change_summary:
                description_parts.append(change_summary)
            if grant_root:
                description_parts.append(f"grants write to {grant_root}")
            description = (
                "; ".join(description_parts)
                if description_parts
                else "Codex requests to apply a patch"
            )
            command_label = (
                f"apply_patch: {change_summary}" if change_summary
                else f"apply_patch: {reason}" if reason
                else "apply_patch"
            )
            try:
                choice = self._call_approval_callback(
                    command_label,
                    description,
                    timeout=timeout,
                )
                return _approval_choice_to_codex_decision(choice)
            except TimeoutError:
                logger.warning("approval_callback timed out on apply_patch")
                return "decline"
            except Exception:
                logger.exception("approval_callback raised on apply_patch")
                return "decline"
        return "decline"

    def _track_pending_file_change(self, note: dict) -> None:
        """Maintain self._pending_file_changes from item/started + item/completed
        notifications. Lets the apply_patch approval prompt show what's
        actually changing — codex's approval params don't carry the data."""
        method = note.get("method", "")
        params = note.get("params") or {}
        item = params.get("item") or {}
        if item.get("type") != "fileChange":
            return
        item_id = item.get("id") or ""
        if not item_id:
            return
        if method == "item/started":
            changes = item.get("changes") or []
            if not changes:
                self._pending_file_changes[item_id] = "1 change pending"
                return
            kinds: dict[str, int] = {}
            paths: list[str] = []
            for ch in changes:
                if not isinstance(ch, dict):
                    continue
                kind = (ch.get("kind") or {}).get("type") or "update"
                kinds[kind] = kinds.get(kind, 0) + 1
                p = ch.get("path") or ""
                if p:
                    paths.append(p)
            counts = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
            preview = ", ".join(paths[:3])
            if len(paths) > 3:
                preview += f", +{len(paths) - 3} more"
            self._pending_file_changes[item_id] = (
                f"{counts}: {preview}" if preview else counts
            )
        elif method == "item/completed":
            self._pending_file_changes.pop(item_id, None)

    def _lookup_pending_file_change(self, item_id: str) -> Optional[str]:
        """Look up an in-progress fileChange item by id and summarize its
        changes for the approval prompt. Returns None when we don't have
        the item cached (e.g. approval arrived before item/started, or
        fileChange item content not tracked yet)."""
        if not item_id:
            return None
        cached = self._pending_file_changes.get(item_id)
        if not cached:
            return None
        return cached


def _apply_token_usage_notification(result: TurnResult, note: dict) -> None:
    """Capture Codex app-server token usage updates for caller accounting.

    Codex does not put token usage on turn/completed. It emits a separate
    thread/tokenUsage/updated notification containing cumulative totals and
    the latest turn breakdown.
    """
    if not isinstance(note, dict) or note.get("method") != "thread/tokenUsage/updated":
        return
    params = note.get("params") or {}
    token_usage = params.get("tokenUsage") or {}
    if not isinstance(token_usage, dict):
        return
    last = token_usage.get("last")
    total = token_usage.get("total")
    if isinstance(last, dict):
        result.token_usage_last = dict(last)
    if isinstance(total, dict):
        result.token_usage_total = dict(total)
    window = token_usage.get("modelContextWindow")
    if isinstance(window, int) and window > 0:
        result.model_context_window = window


def _apply_compaction_notification(result: TurnResult, note: dict) -> None:
    """Capture Codex-native context compaction boundaries.

    Recent app-server builds expose compaction as a ContextCompaction item.
    Older builds also emit the deprecated thread/compacted notification. Both
    mean the underlying Codex thread history has been compacted.
    """
    if not isinstance(note, dict):
        return
    method = note.get("method") or ""
    params = note.get("params") or {}
    if not isinstance(params, dict):
        return

    if method == "thread/compacted":
        result.compacted = True
        result.thread_id = params.get("threadId") or result.thread_id
        result.turn_id = params.get("turnId") or result.turn_id
        return

    if method not in {"item/started", "item/completed"}:
        return

    item = params.get("item") or {}
    if not isinstance(item, dict) or item.get("type") != "contextCompaction":
        return

    result.compacted = True
    result.thread_id = params.get("threadId") or result.thread_id
    result.turn_id = params.get("turnId") or result.turn_id


def _approval_choice_to_codex_decision(choice: str) -> str:
    """Map Hermes approval choices onto codex's CommandExecutionApprovalDecision
    / FileChangeApprovalDecision wire values.

    Hermes returns 'once', 'session', 'always', or 'deny'.
    Codex expects 'accept', 'acceptForSession', 'decline', or 'cancel'
    (verified against codex-rs/app-server-protocol/src/protocol/v2/item.rs
    on codex 0.130.0).

    This mapping is Codex-protocol-semantic and intentionally lives here,
    NOT in tools/approval.py: the Hermes approval mode/timeout resolution
    and the choice itself come from the shared core (tools/approval.py);
    only the wire-value translation is local.
    """
    if choice in {"once",}:
        return "accept"
    if choice in {"session", "always"}:
        return "acceptForSession"
    # "deny" and "timeout" both map to decline — codex has no wire value for
    # "prompt expired"; the Hermes-side messaging already distinguishes them.
    return "decline"


def _has_turn_aborted_marker(text: str) -> bool:
    """Return True if `text` contains any of the raw markers codex uses
    to signal a turn was aborted without emitting `turn/completed`.

    Codex emits `<turn_aborted>` (and sometimes `<turn_aborted/>`) as raw
    text inside agentMessage items when an interrupt or upstream error
    tears the turn down before the normal completion path fires. Mirrors
    openclaw beta.8's terminal-marker fix so we don't burn the full turn
    deadline waiting for a turn/completed that never comes.
    """
    if not text:
        return False
    for marker in _TURN_ABORTED_MARKERS:
        if marker in text:
            return True
    return False


def _get_hermes_version() -> str:
    """Best-effort Hermes version string for codex's userAgent line."""
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:  # pragma: no cover
        return "0.0.0"

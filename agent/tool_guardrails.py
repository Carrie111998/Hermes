"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed


IDEMPOTENT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "web_search",
        "web_extract",
        "session_search",
        "browser_snapshot",
        "browser_console",
        "browser_get_images",
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_read_multiple_files",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_list_directory_with_sizes",
        "mcp_filesystem_directory_tree",
        "mcp_filesystem_get_file_info",
        "mcp_filesystem_search_files",
    }
)

MUTATING_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "todo",
        "memory",
        "skill_manage",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_navigate",
        "send_message",
        "cronjob",
        "delegate_task",
        "process",
    }
)

DETERMINISTIC_BLOCKER_CLASSES = frozenset(
    {"missing_target", "permission", "invalid_workdir", "malformed_input"}
)
_TARGET_KEYS = frozenset(
    {
        "path", "file", "file_path", "filename", "target", "destination",
        "dest", "source", "src", "workdir", "cwd", "directory",
        # Tool-specific target identifiers.  These values remain private and
        # are only hashed for the in-memory ledger; content/prompt fields are
        # intentionally excluded so changing payload does not evade a blocker
        # for the same target.
        "url", "key", "name", "job_id", "message_id", "chat_id",
        "channel", "ref", "element", "goal", "script",
    }
)


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Hard stops
    are explicit opt-in so interactive CLI/TUI sessions get a gentle nudge unless
    the user enables circuit-breaker behavior in config.yaml.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    cross_turn_failure_halt_after: int = 3
    cross_turn_ledger_max_entries: int = 128
    cross_turn_ttl_seconds: int = 1800
    unattended_soft_mode: bool = False
    configuration_warning: str = ""
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
        *,
        default_hard_stop_enabled: bool = False,
    ) -> "ToolCallGuardrailConfig":
        """Build config from the ``tool_loop_guardrails`` config section.

        ``default_hard_stop_enabled`` lets unattended runtimes choose a safe
        default without changing the interactive CLI/TUI behavior. The
        ``auto`` value follows that runtime default. On unattended surfaces,
        explicitly disabling the stop requires ``unattended_soft_mode`` so a
        softened safety policy is observable.
        """
        if not isinstance(data, Mapping):
            return cls(hard_stop_enabled=default_hard_stop_enabled)

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls(hard_stop_enabled=default_hard_stop_enabled)
        hard_stop_value = data.get("hard_stop_enabled")
        soft_mode = _as_bool(data.get("unattended_soft_mode"), False)
        configuration_warning = ""
        if isinstance(hard_stop_value, str) and hard_stop_value.strip().lower() == "auto":
            hard_stop_enabled = default_hard_stop_enabled
        else:
            hard_stop_enabled = _as_bool(hard_stop_value, defaults.hard_stop_enabled)
            if default_hard_stop_enabled and not hard_stop_enabled and not soft_mode:
                hard_stop_enabled = True
                configuration_warning = (
                    "tool_loop_guardrails.hard_stop_enabled=false was ignored for "
                    "an unattended surface; set unattended_soft_mode=true to opt "
                    "into visibly degraded soft mode"
                )
        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=hard_stop_enabled,
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
            cross_turn_failure_halt_after=_positive_int(
                data.get("cross_turn_failure_halt_after"),
                defaults.cross_turn_failure_halt_after,
            ),
            cross_turn_ledger_max_entries=_positive_int(
                data.get("cross_turn_ledger_max_entries"),
                defaults.cross_turn_ledger_max_entries,
            ),
            cross_turn_ttl_seconds=_positive_int(
                data.get("cross_turn_ttl_seconds"),
                defaults.cross_turn_ttl_seconds,
            ),
            unattended_soft_mode=soft_mode,
            configuration_warning=configuration_warning,
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        # Argument digests and target identities are private controller state;
        # never put them into durable tool results or transcripts.
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Safety-fallback classifier used only when callers don't pass ``failed``.

    Mirrors ``agent.display._detect_tool_failure`` exactly so the guardrail
    never disagrees with the CLI's user-visible ``[error]`` tag. Production
    callers in ``run_agent.py`` always pass an explicit ``failed=`` derived
    from ``_detect_tool_failure``; this function exists so standalone callers
    (tests, tooling) still get consistent behavior.
    """
    if result is None:
        return False, ""
    if file_mutation_result_landed(tool_name, result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        return False, ""

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                return True, " [full]"

    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


def classify_failure_class(tool_name: str, result: str | None) -> str:
    """Classify deterministic blockers without retaining their raw text."""
    if not result:
        return "unknown"
    parsed = safe_json_loads(result)
    fragments: list[str] = []
    if isinstance(parsed, Mapping):
        for key in ("error", "stderr", "stdout", "message", "reason"):
            value = parsed.get(key)
            if value:
                fragments.append(str(value))
        exit_code = parsed.get("exit_code")
        if exit_code not in (None, 0) and not fragments:
            fragments.append(f"exit code {exit_code}")
    fragments.append(str(result)[:1200])
    text = " ".join(fragments).lower()
    if any(token in text for token in ("permission denied", "access denied", "approval denied", "not approved")):
        return "permission"
    if any(token in text for token in ("working directory", "workdir", "cwd", "directory does not exist")):
        return "invalid_workdir"
    if any(token in text for token in ("no such file", "file not found", "path not found", "does not exist", "missing target")):
        return "missing_target"
    if any(token in text for token in ("syntaxerror", "parse error", "invalid json", "malformed", "schema validation", "invalid syntax")):
        return "malformed_input"
    return "unknown"


def _target_identity(tool_name: str, args: Mapping[str, Any]) -> str:
    """Return a private target identity that never leaves the controller."""
    targets: list[str] = []
    for key, value in args.items():
        key_lower = str(key).lower()
        if key_lower in _TARGET_KEYS:
            targets.append(f"{key_lower}={value}")
    if not targets and tool_name == "terminal":
        command = str(args.get("command", ""))
        candidates = re.findall(r"(?:/[^\s'\"]+|[A-Za-z]:[\\/][^\s'\"]+)", command)
        targets.extend(candidates[:4])
    if not targets:
        targets.append(tool_name)
    canonical = json.dumps(sorted(targets), ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{tool_name}:{_sha256(canonical)}"


class ToolCallGuardrailController:
    """Bounded per-turn and cross-turn controller for tool-call loops."""

    def __init__(self, config: ToolCallGuardrailConfig | None = None):
        self.config = config or ToolCallGuardrailConfig()
        self._cross_turn_failures: dict[str, tuple[int, float, str]] = {}
        self._cross_turn_lock = threading.RLock()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None

    def reset_for_session(self) -> None:
        """Clear all state when the agent session identity changes."""
        with self._cross_turn_lock:
            self._cross_turn_failures.clear()
        self.reset_for_turn()

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        target_key = _target_identity(tool_name, _coerce_args(args))
        with self._cross_turn_lock:
            self._prune_cross_turn_failures()
            cross_record = self._cross_turn_failures.get(target_key)
        if cross_record is not None:
            cross_count, _last_seen, _failure_class = cross_record
            if cross_count >= self.config.cross_turn_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="cross_turn_deterministic_blocker",
                    message=(
                        f"Blocked {tool_name}: the same deterministic blocker persisted "
                        f"for this target across {cross_count} failed attempts. Stop "
                        "retrying this path; change the target or report the blocker."
                    ),
                    tool_name=tool_name,
                    count=cross_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same tool call failed {exact_count} "
                    "times with identical arguments. Stop retrying it unchanged; "
                    "change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"Blocked {tool_name}: this read-only call returned the same "
                            f"result {repeat_count} times. Stop repeating it unchanged; "
                            "use the result already provided or try a different query."
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)

        if failed:
            failure_class = classify_failure_class(tool_name, result)
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            if failure_class in DETERMINISTIC_BLOCKER_CLASSES:
                self._record_cross_turn_failure(
                    _target_identity(tool_name, args), failure_class
                )

            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this turn. "
                        "Stop retrying the same failing tool path and choose a different approach."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with identical arguments. "
                        "This looks like a loop; inspect the error and change strategy "
                        "instead of retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        with self._cross_turn_lock:
            self._cross_turn_failures.pop(_target_identity(tool_name, args), None)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query instead of "
                    "repeating it unchanged."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def _record_cross_turn_failure(self, target_key: str, failure_class: str) -> None:
        with self._cross_turn_lock:
            now = time.monotonic()
            previous = self._cross_turn_failures.get(target_key)
            if previous is not None and previous[2] == failure_class:
                count = previous[0] + 1
            else:
                count = 1
            self._cross_turn_failures[target_key] = (count, now, failure_class)
            self._prune_cross_turn_failures(now=now)

    def _prune_cross_turn_failures(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        ttl = self.config.cross_turn_ttl_seconds
        expired = [
            key for key, (_count, last_seen, _failure_class) in self._cross_turn_failures.items()
            if now - last_seen > ttl
        ]
        for key in expired:
            self._cross_turn_failures.pop(key, None)
        overflow = len(self._cross_turn_failures) - self.config.cross_turn_ledger_max_entries
        if overflow > 0:
            oldest = sorted(
                self._cross_turn_failures.items(), key=lambda item: item[1][1]
            )[:overflow]
            for key, _value in oldest:
                self._cross_turn_failures.pop(key, None)


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a blocked tool call."""
    return json.dumps(
        {
            "error": decision.message,
            "guardrail": decision.to_metadata(),
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    suffix = (
        f"\n\n[{label}: "
        f"{decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result or "") + suffix


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Action-oriented guidance for recovering from repeated tool failures."""
    common = (
        f"{tool_name} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, run a small diagnostic such as `pwd && ls -la` "
            "in the same tool, then try an absolute path, a simpler command, a different "
            "working directory, or a different tool such as read_file/write_file/patch."
        )
    return common + (
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    if parsed is not None:
        try:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            canonical = str(parsed)
    else:
        canonical = result or ""
    return _sha256(canonical)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

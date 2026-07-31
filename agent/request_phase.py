"""Per-turn request phase and repository-mutation safety.

Hermes should retain its normal ability to act on business systems.  This
module protects one narrower boundary: a read-only investigation must not
silently turn into source edits, and an explicitly requested implementation
must not overwrite a repository that was already dirty when the turn began.

The active policy is a ContextVar so concurrent gateway turns remain isolated
and tool worker threads receive the policy through the existing context-copy
helpers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from agent.runtime_cwd import resolve_agent_cwd


class RequestPhase(str, Enum):
    INVESTIGATION = "investigation"
    IMPLEMENTATION = "implementation"
    OPERATION = "operation"


_IMPLEMENTATION_PATTERNS = (
    re.compile(r"\b(?:implement|refactor)\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:please\s+)?(?:add|remove|delete|rename|move)\s+"
        r"(?:(?:a|an|the|that|this)\s+)?"
        r"(?:test|guardrail|function|method|class|module|file|directory|"
        r"source|code|prompt|skill|runtime|repository|repo)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:edit|modify|patch|update|change|write)\s+(?:the\s+)?"
        r"(?:code|source|repo(?:sitory)?|file(?:s)?|skill(?:s)?|prompt|"
        r"runtime|guardrail(?:s)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:fix|repair)\s+(?:the\s+)?"
        r"(?:bug|code|test(?:s)?|build|implementation|runtime)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:edit|modify|patch|update|change|write)\b.{0,80}"
        r"\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|rb|php|cs|cpp|c|h|"
        r"sql|sh|ps1|yaml|yml|toml|json|md)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:fix|repair|patch|simplify)\b.{0,80}\b(?:bug|code|source|"
        r"repo(?:sitory)?|file|function|method|class|module|test(?:s)?|"
        r"build|implementation|runtime|guardrail|prompt|skill)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgit\s+(?:commit|push)\b|"
        r"\b(?:commit|push|deploy|release|ship)\b.{0,80}\b(?:code|source|"
        r"repo(?:sitory)?|branch|commit|build|app(?:lication)?|service|"
        r"runtime|deployment|migration|release|pull request|pr)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bbuild\s+(?:the\s+|a\s+)?"
        r"(?:feature|system|integration|tool|app(?:lication)?|service|"
        r"automation|guardrail|plugin|skill)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:create|open)\s+(?:a\s+)?(?:pull request|pr|commit)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:improve|optimi[sz]e|streamline|harden|clean\s+up|"
        r"speed\s+up)\b.{0,100}\b(?:hermes|codex|prompt(?:\s+builder)?|"
        r"code|source\s+code|repo(?:sitory)?|worktree|module|runtime|"
        r"gateway|function|method|class|test\s+suite|software\s+build|"
        r"implementation|mcp|skill\s+loading)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\bmake\s+(?:(?:the|this|that|our)\s+)?"
        r"(?:hermes|codex|prompt(?:\s+builder)?|code|source\s+code|"
        r"repo(?:sitory)?|module|runtime|gateway|function|method|class|"
        r"test\s+suite|software\s+build|implementation|mcp)\b.{0,120}\b"
        r"(?:faster|safer|selective|reliable|simpler|cleaner|work|"
        r"stop|avoid|use|load|handle|support)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\badd\s+(?:(?:a|the|this|that)\s+)?"
        r"(?:regression|test)\s+(?:coverage|fixture|case(?:s)?)\b",
        re.IGNORECASE,
    ),
)

_INVESTIGATION_PATTERN = re.compile(
    r"\b(?:analy[sz]e|investigate|review|inspect|audit|assess|explain|"
    r"describe|map|inventory|look\s+into|figure\s+out|understand|"
    r"tell\s+me)\b",
    re.IGNORECASE,
)

_ANALYSIS_ONLY_AUTHORITY_PATTERN = re.compile(
    r"\b(?:whether|if)\s+(?:we|you|i)\s+should\b|"
    r"\bshould\s+(?:we|you|i)\b|"
    r"^\s*(?:can|could|would)\s+you\s+(?:analy[sz]e|review|assess|"
    r"explain|describe|tell\s+me)\b",
    re.IGNORECASE,
)

_EXPLICIT_EXECUTION_LINK_PATTERN = re.compile(
    r"\b(?:and|then|also|after\s+that)\s+(?:please\s+)?"
    r"(?:implement|refactor|patch|edit|modify)\b",
    re.IGNORECASE,
)

_EXECUTION_CONTINUATION_PATTERN = re.compile(
    r"^\s*(?:approved[.!]?\s*(?:now\s+)?)?(?:"
    r"fix\s+it|repair\s+it|make\s+it\s+work|make\s+(?:this|it)\s+happen|"
    r"go\s+ahead|do\s+it|proceed|ship\s+it(?:\s+live)?|"
    r"figure\s+out\s+how\s+to\s+make\s+(?:this|it)\s+happen"
    r")\b",
    re.IGNORECASE,
)

_PRIOR_IMPLEMENTATION_CONTEXT_PATTERN = re.compile(
    r"(?:"
    r"\b(?:implement(?:ation)?|refactor|edit|modify|patch|fix|repair|change|"
    r"write|update|add|remove|build|deploy|commit|push|ship|ready|complete|"
    r"finished)\b.{0,160}\b(?:code|source|repo(?:sitory)?|worktree|branch|"
    r"commit|pull request|pr|function|method|class|module|test(?:s)?|bug|"
    r"build|runtime|migration|schema)\b"
    r"|"
    r"\b(?:code|source|repo(?:sitory)?|worktree|branch|commit|pull request|"
    r"pr|function|method|class|module|test(?:s)?|bug|build|runtime|migration|"
    r"schema)\b.{0,160}\b(?:implement(?:ation)?|refactor|edit|modify|patch|"
    r"fix|repair|change|write|update|add|remove|build|deploy|commit|push|"
    r"ship|ready|complete|finished)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def _request_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_request_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_request_text(v) for v in value)
    return ""


def _recent_prior_text(prior_context: Any) -> str:
    """Return a bounded recent user/assistant subject for continuation checks."""

    if not isinstance(prior_context, (list, tuple)):
        return _request_text(prior_context)[-8000:]
    conversational = [
        item
        for item in prior_context
        if not isinstance(item, dict)
        or str(item.get("role", "")).lower() in {"user", "assistant"}
    ]
    return _request_text(conversational[-2:])[-8000:]


def classify_request_phase(
    user_request: Any,
    *,
    prior_context: Any = None,
) -> RequestPhase:
    """Classify the user's authority for this turn.

    Explicit implementation language wins over analysis language only when
    the user actually authorizes execution.  A question such as "analyze
    whether we should deploy" remains read-only even though it names a
    deployment.  Everything else defaults to an ordinary operation,
    preserving Hermes' native business agency.
    """

    text = _request_text(user_request)
    prior_text = _recent_prior_text(prior_context)
    if (
        _EXECUTION_CONTINUATION_PATTERN.search(text)
        and _PRIOR_IMPLEMENTATION_CONTEXT_PATTERN.search(prior_text)
    ):
        return RequestPhase.IMPLEMENTATION
    if (
        _ANALYSIS_ONLY_AUTHORITY_PATTERN.search(text)
        and not _EXPLICIT_EXECUTION_LINK_PATTERN.search(text)
    ):
        return RequestPhase.INVESTIGATION
    if any(pattern.search(text) for pattern in _IMPLEMENTATION_PATTERNS):
        return RequestPhase.IMPLEMENTATION
    if _INVESTIGATION_PATTERN.search(text):
        return RequestPhase.INVESTIGATION
    return RequestPhase.OPERATION


@dataclass(frozen=True)
class RepoSnapshot:
    root: Path
    dirty: bool
    probe_error: str = ""
    status_porcelain: str = ""
    head_oid: str = ""
    tree_oid: str = ""


@dataclass
class TurnPolicy:
    phase: RequestPhase
    request_text: str
    workspace: Path
    repo_snapshots: dict[str, RepoSnapshot] = field(default_factory=dict)
    workspace_probe_error: str = ""
    expected_repo_status: dict[str, str] = field(default_factory=dict)
    expected_repo_heads: dict[str, str] = field(default_factory=dict)
    expected_repo_trees: dict[str, str] = field(default_factory=dict)
    repo_drift_block: dict[str, str] = field(default_factory=dict)
    loaded_root_skills: list[str] = field(default_factory=list)
    skill_payload_chars: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


_ACTIVE_TURN_POLICY: ContextVar[Optional[TurnPolicy]] = ContextVar(
    "hermes_request_phase_policy",
    default=None,
)


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 3,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, stdin=subprocess.DEVNULL, **kwargs)


def _existing_directory(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    if not candidate.is_dir():
        candidate = candidate.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _probe_repo(
    path: Path,
    *,
    check_dirty: bool = True,
) -> tuple[Optional[RepoSnapshot], str]:
    candidate = _existing_directory(path)
    try:
        root_result = _run_git(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if root_result.returncode != 0:
        return None, ""

    root_text = root_result.stdout.strip()
    if not root_text:
        return None, "git returned an empty repository root"
    root = Path(root_text).resolve(strict=False)

    try:
        head_result = _run_git(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RepoSnapshot(root=root, dirty=True, probe_error=str(exc)), str(exc)
    if head_result.returncode == 0 and head_result.stdout.strip():
        head_oid = head_result.stdout.strip()
        try:
            tree_result = _run_git(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--verify",
                    "HEAD^{tree}",
                ]
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RepoSnapshot(
                root=root,
                dirty=True,
                probe_error=str(exc),
            ), str(exc)
        if tree_result.returncode != 0 or not tree_result.stdout.strip():
            error = tree_result.stderr.strip() or "git tree identity probe failed"
            return RepoSnapshot(
                root=root,
                dirty=True,
                probe_error=error,
            ), error
        tree_oid = tree_result.stdout.strip()
    else:
        # A newly initialized repository has no commit or tree yet. Preserve
        # its symbolic branch as a stable committed-state identity rather than
        # treating every empty repository as an unavailable probe.
        try:
            symbolic_result = _run_git(
                ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"]
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RepoSnapshot(
                root=root,
                dirty=True,
                probe_error=str(exc),
            ), str(exc)
        if symbolic_result.returncode != 0 or not symbolic_result.stdout.strip():
            error = head_result.stderr.strip() or "git HEAD identity probe failed"
            return RepoSnapshot(
                root=root,
                dirty=True,
                probe_error=error,
            ), error
        head_oid = f"unborn:{symbolic_result.stdout.strip()}"
        tree_oid = ""

    if not check_dirty:
        return RepoSnapshot(
            root=root,
            dirty=False,
            status_porcelain="",
            head_oid=head_oid,
            tree_oid=tree_oid,
        ), ""
    try:
        status_result = _run_git(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RepoSnapshot(root=root, dirty=True, probe_error=str(exc)), str(exc)
    if status_result.returncode != 0:
        error = status_result.stderr.strip() or "git status failed"
        return RepoSnapshot(root=root, dirty=True, probe_error=error), error
    status = status_result.stdout
    return RepoSnapshot(
        root=root,
        dirty=bool(status.strip()),
        status_porcelain=status,
        head_oid=head_oid,
        tree_oid=tree_oid,
    ), ""


def _repo_key(root: Path) -> str:
    return os.path.normcase(str(root.resolve(strict=False)))


def activate_turn_policy(
    user_request: Any,
    *,
    cwd: Optional[Path | str] = None,
    prior_context: Any = None,
) -> TurnPolicy:
    """Bind a fresh policy and capture the working repository baseline."""

    policy = _build_turn_policy(
        user_request,
        cwd=cwd,
        prior_context=prior_context,
    )
    _ACTIVE_TURN_POLICY.set(policy)
    return policy


def _build_turn_policy(
    user_request: Any,
    *,
    cwd: Optional[Path | str] = None,
    prior_context: Any = None,
) -> TurnPolicy:
    workspace = Path(cwd) if cwd is not None else resolve_agent_cwd()
    workspace = workspace.expanduser().resolve(strict=False)
    requested_phase = classify_request_phase(
        user_request,
        prior_context=prior_context,
    )
    parent_policy = _ACTIVE_TURN_POLICY.get()
    # Delegation and nested agent calls copy the parent's Context.  A child
    # may narrow its work, but must never manufacture implementation authority
    # that the owner did not grant to the parent turn.
    if (
        requested_phase is RequestPhase.IMPLEMENTATION
        and parent_policy is not None
        and parent_policy.phase is not RequestPhase.IMPLEMENTATION
    ):
        requested_phase = parent_policy.phase
    policy = TurnPolicy(
        phase=requested_phase,
        request_text=_request_text(user_request),
        workspace=workspace,
    )
    if policy.phase is RequestPhase.IMPLEMENTATION:
        snapshot, error = _probe_repo(workspace)
        if snapshot is not None:
            key = _repo_key(snapshot.root)
            policy.repo_snapshots[key] = snapshot
            policy.expected_repo_status[key] = snapshot.status_porcelain
            policy.expected_repo_heads[key] = snapshot.head_oid
            policy.expected_repo_trees[key] = snapshot.tree_oid
        elif error:
            # A broken/timed-out git probe must not silently disable the guard
            # in the very workspace the agent is about to edit.
            snapshot = RepoSnapshot(
                root=workspace,
                dirty=True,
                probe_error=error,
            )
            key = _repo_key(snapshot.root)
            policy.repo_snapshots[key] = snapshot
            policy.expected_repo_status[key] = snapshot.status_porcelain
            policy.expected_repo_heads[key] = snapshot.head_oid
            policy.expected_repo_trees[key] = snapshot.tree_oid
        policy.workspace_probe_error = error
    return policy


def push_turn_policy(
    user_request: Any,
    *,
    cwd: Optional[Path | str] = None,
    prior_context: Any = None,
) -> Token:
    """Bind a policy and return the token needed to restore the caller."""

    return _ACTIVE_TURN_POLICY.set(
        _build_turn_policy(
            user_request,
            cwd=cwd,
            prior_context=prior_context,
        )
    )


def reset_turn_policy(token: Token) -> None:
    _ACTIVE_TURN_POLICY.reset(token)


def current_turn_policy() -> Optional[TurnPolicy]:
    return _ACTIVE_TURN_POLICY.get()


def clear_turn_policy() -> None:
    """Clear the policy in tests or non-agent embedding code."""

    _ACTIVE_TURN_POLICY.set(None)


def _snapshot_for_path(policy: TurnPolicy, path: Path) -> Optional[RepoSnapshot]:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = policy.workspace / resolved
    resolved = resolved.resolve(strict=False)

    with policy.lock:
        for snapshot in policy.repo_snapshots.values():
            try:
                common = os.path.commonpath((str(resolved), str(snapshot.root)))
                if os.path.normcase(common) == os.path.normcase(str(snapshot.root)):
                    return snapshot
            except ValueError:
                continue

        snapshot, error = _probe_repo(
            resolved,
            check_dirty=policy.phase is RequestPhase.IMPLEMENTATION,
        )
        if snapshot is None and error:
            root = _existing_directory(resolved)
            snapshot = RepoSnapshot(root=root, dirty=True, probe_error=error)
        if snapshot is not None:
            key = _repo_key(snapshot.root)
            policy.repo_snapshots[key] = snapshot
            policy.expected_repo_status.setdefault(
                key,
                snapshot.status_porcelain,
            )
            policy.expected_repo_heads.setdefault(key, snapshot.head_oid)
            policy.expected_repo_trees.setdefault(key, snapshot.tree_oid)
        return snapshot


_TERMINAL_REPO_MUTATION = re.compile(
    r"(?:"
    r"\bgit\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?"
    r"(?:add|am|apply|checkout|cherry-pick|clean|"
    r"commit|merge|mv|rebase|reset|restore|revert|rm|stash|switch|tag)\b|"
    r"\bgit\s+worktree\s+(?:add|move|remove|prune)\b|"
    r"\bgit\s+clone\b|"
    r"\b(?:rm|rmdir|mv|cp|touch|mkdir|truncate)\b|"
    r"\b(?:sed\s+-i|perl\s+-pi|tee)\b|"
    r"\bapply_patch\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|add|remove|update)\b|"
    r"\b(?:ruff\s+format|prettier\b[^\r\n]*--write)\b|"
    r"\b(?:Set|Add|Clear)-Content\b|"
    r"\b(?:Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item)\b|"
    r"\b(?:WriteAllText|WriteAllBytes|write_text|write_bytes|unlink)\b|"
    r"(?<![<>=])>{1,2}(?![=>])"
    r")",
    re.IGNORECASE,
)

_SAFE_DIRTY_BOOTSTRAP = re.compile(
    r"^\s*git\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?"
    r"(?:worktree\s+add|clone)\b[^;&|><`\r\n]*\s*$",
    re.IGNORECASE,
)

_GIT_C_TARGET = re.compile(
    r"\bgit\s+-C\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
    re.IGNORECASE,
)

_ABSOLUTE_COMMAND_PATH = re.compile(
    r"(?:\"((?:[A-Za-z]:[\\/]|/)[^\"]+)\"|"
    r"'((?:[A-Za-z]:[\\/]|/)[^']+)'|"
    r"((?:[A-Za-z]:[\\/]|/)[^\s;|&]+))"
)

_RELATIVE_COMMAND_PATH = re.compile(
    r"(?:^|[\s=(,])(?:"
    r"\"((?:\.{1,2}[\\/])[^\"\r\n]+)\"|"
    r"'((?:\.{1,2}[\\/])[^'\r\n]+)'|"
    r"((?:\.{1,2}[\\/])[^\s;|&,)\r\n]+)"
    r")"
)

_EXECUTE_CODE_MUTATION = re.compile(
    r"(?:write_file|patch|WriteAllText|WriteAllBytes|write_text|write_bytes|"
    r"writeFile(?:Sync)?|appendFile(?:Sync)?|copyFile(?:Sync)?|"
    r"\bopen\s*\([^\)]*,\s*(?:mode\s*=\s*)?['\"][wax+]|"
    r"os\.(?:remove|unlink|rename|replace|mkdir|makedirs|rmdir)|"
    r"shutil\.(?:copy|copy2|copyfile|move|rmtree)|"
    r"\.(?:touch|mkdir|unlink|rename|replace|rmdir)\s*\()",
    re.IGNORECASE,
)

_LOCAL_INTERPRETER_COMMAND = re.compile(
    r"^\s*(?:"
    r"(?:[A-Za-z]:[\\/]|/)?[^\s\"']*[\\/]?"
    r"(?:python(?:\d+(?:\.\d+)*)?|py|node|deno|ruby|php)"
    r"(?:\.exe)?"
    r")\b",
    re.IGNORECASE,
)

_READ_ONLY_TERMINAL_COMMANDS = frozenset(
    {
        "cat",
        "dir",
        "findstr",
        "get-childitem",
        "get-command",
        "get-content",
        "get-item",
        "get-itemproperty",
        "get-location",
        "grep",
        "head",
        "ls",
        "measure-object",
        "more",
        "pwd",
        "resolve-path",
        "rg",
        "select-string",
        "tail",
        "test-path",
        "type",
        "wc",
    }
)

_READ_ONLY_GIT_COMMANDS = frozenset(
    {
        "cat-file",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "status",
    }
)

_SHELL_WORD = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')

_SKILL_MUTATION_ACTIONS = {
    "create",
    "edit",
    "patch",
    "delete",
    "write_file",
    "remove_file",
}

_LOCAL_MUTATION_SUSPENDED = threading.Event()
_LOCAL_MUTATION_SUSPENSION_LOCK = threading.Lock()
_LOCAL_MUTATION_SUSPENSION_REASON = ""


def suspend_local_mutations(reason: str) -> None:
    """Fail closed on local self-improvement once gateway drain begins."""

    global _LOCAL_MUTATION_SUSPENSION_REASON
    with _LOCAL_MUTATION_SUSPENSION_LOCK:
        _LOCAL_MUTATION_SUSPENSION_REASON = str(reason or "gateway drain")
        _LOCAL_MUTATION_SUSPENDED.set()


def resume_local_mutations() -> None:
    """Reset the process-local drain guard at a fresh gateway start."""

    global _LOCAL_MUTATION_SUSPENSION_REASON
    with _LOCAL_MUTATION_SUSPENSION_LOCK:
        _LOCAL_MUTATION_SUSPENSION_REASON = ""
        _LOCAL_MUTATION_SUSPENDED.clear()


def _local_mutation_suspension_reason() -> str:
    with _LOCAL_MUTATION_SUSPENSION_LOCK:
        if _LOCAL_MUTATION_SUSPENDED.is_set():
            return _LOCAL_MUTATION_SUSPENSION_REASON or "gateway drain"
    try:
        from gateway.status import gateway_owner_hold_targets_self

        if gateway_owner_hold_targets_self():
            return "explicit gateway owner hold"
    except Exception:
        pass
    return ""


def _local_mutation_requested(function_name: str, args: dict[str, Any]) -> bool:
    if function_name in {"write_file", "patch"}:
        return True
    if function_name == "skill_manage":
        return str(args.get("action", "") or "").lower() in _SKILL_MUTATION_ACTIONS
    if function_name == "terminal":
        command = args.get("command")
        return not isinstance(command, str) or not _terminal_read_is_proven(command)
    if function_name == "execute_code":
        return True
    return False


MAX_SKILL_PAYLOAD_CHARS_PER_RESULT = 32_000
MAX_SKILL_PAYLOAD_CHARS_PER_TURN = 64_000


def _protected_skill_roots() -> tuple[Path, ...]:
    """Return profile-local and configured external skill roots.

    Skill routing belongs to skill metadata and the prompt overlay.  The core
    runtime only needs the generic filesystem boundary: every installed skill
    tree is source-like state and therefore requires implementation authority
    before a local write, even when the tree is not itself a Git repository.
    """

    try:
        from agent.skill_utils import get_all_skills_dirs

        roots = get_all_skills_dirs()
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            roots = [get_hermes_home() / "skills"]
        except Exception:
            roots = []
    resolved: list[Path] = []
    for root in roots:
        try:
            candidate = Path(root).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError):
            continue
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _protected_skill_root_for(path: Path) -> Optional[Path]:
    for root in _protected_skill_roots():
        if _path_is_within(path, root):
            return root
    return None


def _terminal_embeds_local_mutation(command: str) -> bool:
    """Detect common interpreter wrappers around direct filesystem writes."""

    return bool(
        _LOCAL_INTERPRETER_COMMAND.search(command)
        and _EXECUTE_CODE_MUTATION.search(command)
    )


def _mentioned_paths(value: str, *, base: Path) -> list[Path]:
    """Resolve explicit absolute/relative paths embedded in command text."""

    targets: list[Path] = []
    for pattern in (_ABSOLUTE_COMMAND_PATH, _RELATIVE_COMMAND_PATH):
        for match in pattern.finditer(value):
            raw = next((item for item in match.groups() if item), None)
            if not raw:
                continue
            cleaned = raw.rstrip(",)")
            candidate = Path(cleaned).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            targets.append(candidate.resolve(strict=False))
    return list(dict.fromkeys(targets))


def _skill_payload_budget_error(policy: TurnPolicy) -> str:
    return (
        "Skill payload safety block: this turn has used "
        f"{policy.skill_payload_chars:,} of the "
        f"{MAX_SKILL_PAYLOAD_CHARS_PER_TURN:,}-character skill-content "
        "budget. Continue with the skills already loaded, request one exact "
        "small supporting file if it fits, or end with one exact blocker. "
        "Do not load another broad skill packet."
    )


def _skill_headings(content: str) -> list[str]:
    """Expose bounded navigation without returning partial instructions."""

    headings: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not re.match(r"^#{1,6}\s+\S", stripped):
            continue
        headings.append(stripped[:160])
        if len(headings) >= 24:
            break
    return headings


def _bounded_linked_files(value: Any) -> Optional[dict[str, list[str]]]:
    if not isinstance(value, dict):
        return None
    bounded: dict[str, list[str]] = {}
    item_count = 0
    char_count = 0
    for category, raw_items in value.items():
        if not isinstance(raw_items, list):
            continue
        items: list[str] = []
        for raw_item in raw_items:
            item = str(raw_item)[:240]
            if item_count >= 40 or char_count + len(item) > 4_000:
                break
            items.append(item)
            item_count += 1
            char_count += len(item)
        if items:
            bounded[str(category)[:80]] = items
        if item_count >= 40 or char_count >= 4_000:
            break
    return bounded or None


def enforce_skill_payload_budget(args: dict[str, Any], result: str) -> str:
    """Bound cumulative skill content using the payload actually returned.

    The request guard cannot know a rendered skill's size before execution.
    This post-dispatch seam accounts the actual ``content`` field atomically,
    delivers a complete result only when it fits. Oversized results fail
    closed with bounded navigation metadata; partial governing instructions
    never masquerade as a successfully loaded skill.
    """

    policy = current_turn_policy()
    if policy is None:
        return result

    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Skill payload safety block: skill_view returned an "
                    "unreadable payload, so it was rejected instead of being "
                    "added to the model context."
                ),
            },
            ensure_ascii=False,
        )

    if not isinstance(parsed, dict) or not parsed.get("success"):
        return result

    content = parsed.get("content")
    if not isinstance(content, str):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Skill payload safety block: a successful skill_view "
                    "response had no text content, so it was rejected."
                ),
            },
            ensure_ascii=False,
        )

    original_chars = len(content)
    skill_name = str(parsed.get("name") or args.get("name") or "")
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    prior_receipt = parsed.get("payload_budget")
    if isinstance(prior_receipt, dict) and prior_receipt.get("accounted"):
        receipt_matches = (
            prior_receipt.get("limit_chars")
            == MAX_SKILL_PAYLOAD_CHARS_PER_TURN
            and prior_receipt.get("per_result_limit_chars")
            == MAX_SKILL_PAYLOAD_CHARS_PER_RESULT
            and prior_receipt.get("returned_content_chars") == original_chars
            and prior_receipt.get("content_sha256") == content_sha256
        )
        if receipt_matches:
            return result
        return json.dumps(
            {
                "success": False,
                "name": skill_name,
                "error": (
                    "Skill payload safety block: the skill result changed "
                    "after its payload was accounted. The transformed packet "
                    "was rejected instead of reaching the model."
                ),
                "payload_budget": {
                    "limit_chars": MAX_SKILL_PAYLOAD_CHARS_PER_TURN,
                    "per_result_limit_chars": (
                        MAX_SKILL_PAYLOAD_CHARS_PER_RESULT
                    ),
                    "used_chars": policy.skill_payload_chars,
                    "original_content_chars": original_chars,
                    "returned_content_chars": 0,
                    "blocked": True,
                    "reason": "post_accounting_change",
                    "no_partial_content_loaded": True,
                },
            },
            ensure_ascii=False,
        )
    with policy.lock:
        turn_remaining = max(
            0,
            MAX_SKILL_PAYLOAD_CHARS_PER_TURN - policy.skill_payload_chars,
        )
        if (
            original_chars <= MAX_SKILL_PAYLOAD_CHARS_PER_RESULT
            and original_chars <= turn_remaining
        ):
            policy.skill_payload_chars += original_chars
            parsed["payload_budget"] = {
                "limit_chars": MAX_SKILL_PAYLOAD_CHARS_PER_TURN,
                "per_result_limit_chars": MAX_SKILL_PAYLOAD_CHARS_PER_RESULT,
                "used_chars": policy.skill_payload_chars,
                "remaining_chars": (
                    MAX_SKILL_PAYLOAD_CHARS_PER_TURN
                    - policy.skill_payload_chars
                ),
                "original_content_chars": original_chars,
                "returned_content_chars": original_chars,
                "content_sha256": content_sha256,
                "accounted": True,
                "truncated": False,
                "blocked": False,
            }
            return json.dumps(parsed, ensure_ascii=False)

        reason = (
            "per_result_limit"
            if original_chars > MAX_SKILL_PAYLOAD_CHARS_PER_RESULT
            else "turn_limit"
        )
        if reason == "per_result_limit":
            error = (
                f"Skill payload safety block: `{skill_name}` rendered "
                f"{original_chars:,} characters, above the "
                f"{MAX_SKILL_PAYLOAD_CHARS_PER_RESULT:,}-character per-result "
                "limit. No partial instructions were loaded. Use the bounded "
                "headings and linked-file names below to request one exact "
                "smaller supporting file, or split the root skill packet."
            )
        else:
            error = (
                f"{_skill_payload_budget_error(policy)} The requested "
                f"`{skill_name}` packet is {original_chars:,} characters, so "
                "it was not partially loaded."
            )
        blocked_result = {
            "success": False,
            "name": skill_name,
            "file": parsed.get("file"),
            "description": str(parsed.get("description") or "")[:500] or None,
            "error": error,
            "available_headings": _skill_headings(content),
            "linked_files": _bounded_linked_files(parsed.get("linked_files")),
            "payload_budget": {
                "limit_chars": MAX_SKILL_PAYLOAD_CHARS_PER_TURN,
                "per_result_limit_chars": MAX_SKILL_PAYLOAD_CHARS_PER_RESULT,
                "used_chars": policy.skill_payload_chars,
                "remaining_chars": turn_remaining,
                "original_content_chars": original_chars,
                "returned_content_chars": 0,
                "truncated": False,
                "blocked": True,
                "reason": reason,
                "no_partial_content_loaded": True,
            },
        }
        return json.dumps(blocked_result, ensure_ascii=False)


def _patch_paths(args: dict[str, Any]) -> Iterable[Path]:
    path = args.get("path")
    if isinstance(path, str) and path.strip():
        yield Path(path.strip())
    patch_text = args.get("patch")
    if not isinstance(patch_text, str):
        return
    for match in re.finditer(
        r"^\*\*\*\s*(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$",
        patch_text,
        re.MULTILINE,
    ):
        yield Path(match.group(1).strip())
    for match in re.finditer(
        r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+?)\s*$",
        patch_text,
        re.MULTILINE,
    ):
        yield Path(match.group(1).strip())
        yield Path(match.group(2).strip())


def _shell_words(command: str) -> list[str]:
    return [
        token[1:-1]
        if len(token) >= 2
        and token[0] == token[-1]
        and token[0] in {"'", '"'}
        else token
        for token in _SHELL_WORD.findall(command)
    ]


def _git_read_is_proven(words: list[str]) -> bool:
    """Accept only Git forms whose command itself is observational."""

    index = 1
    while index < len(words):
        token = words[index]
        if token == "-C":
            index += 2
            continue
        if token.lower() == "--no-pager":
            index += 1
            continue
        break
    if index >= len(words):
        return False

    command = words[index].lower()
    args = [token.lower() for token in words[index + 1 :]]
    if any(
        token == "--ext-diff"
        or token == "--textconv"
        or token == "--output"
        or token.startswith("--output=")
        for token in args
    ):
        return False
    if command in _READ_ONLY_GIT_COMMANDS:
        return True
    if command == "branch":
        return bool(args) and all(
            token in {
                "--all",
                "--list",
                "--remotes",
                "--show-current",
                "-a",
                "-r",
                "-v",
                "-vv",
            }
            for token in args
        )
    if command == "config":
        return any(
            token in {
                "--get",
                "--get-all",
                "--get-regexp",
                "--get-urlmatch",
                "--list",
                "-l",
            }
            for token in args
        )
    if command == "remote":
        return bool(args) and args[0] in {
            "--verbose",
            "-v",
            "get-url",
        }
    if command == "submodule":
        return bool(args) and args[0] == "status"
    if command == "tag":
        return bool(args) and args[0] in {"--list", "-l"}
    if command == "worktree":
        return bool(args) and args[0] == "list"
    return False


def _terminal_read_is_proven(command: str) -> bool:
    """Return true only for a deliberately small, shell-free read surface.

    Repository investigations already have dedicated file/search tools.  This
    allowlist keeps common diagnostics available while refusing interpreters,
    script blocks, substitutions, pipes, redirections, and arbitrary binaries
    that can mutate files under a read-only-looking spelling.
    """

    cleaned = command.strip()
    if not cleaned:
        return True
    if (
        any(marker in cleaned for marker in ("\r", "\n", "`", "$(", "|", "&", ">", "{", "}"))
        or re.search(r"(?<![<])<(?![<=])", cleaned)
    ):
        return False

    segments = [segment.strip() for segment in cleaned.split(";")]
    if not segments or any(not segment for segment in segments):
        return False
    for segment in segments:
        words = _shell_words(segment)
        if not words:
            return False
        raw_executable = words[0]
        # Trust only a bare executable resolved by the controlled process PATH.
        # Taking Path(...).name let an attacker-controlled /tmp/git or
        # C:\temp\cat.exe inherit the trusted basename and execute arbitrary
        # effects during an investigation.
        if (
            "/" in raw_executable
            or "\\" in raw_executable
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", raw_executable)
        ):
            return False
        executable = raw_executable.lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        if executable == "git":
            if not _git_read_is_proven(words):
                return False
            continue
        if executable not in _READ_ONLY_TERMINAL_COMMANDS:
            return False
        if executable == "rg" and any(
            token.lower() == "--pre"
            or token.lower().startswith("--pre=")
            or token.lower().startswith("--pre-glob")
            for token in words[1:]
        ):
            return False
    return True


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            (
                str(path.resolve(strict=False)),
                str(root.resolve(strict=False)),
            )
        )
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(
        str(root.resolve(strict=False))
    )


def _bootstrap_destinations(
    command: str,
    *,
    base: Path,
) -> Optional[list[Path]]:
    """Resolve every filesystem destination of a standalone bootstrap.

    The exemption is intentionally narrow.  Dynamic shell expansion and an
    implicit clone destination are refused because they cannot be proven
    outside a dirty source checkout before execution.
    """

    if not _SAFE_DIRTY_BOOTSTRAP.fullmatch(command):
        return None
    if "$" in command or "%" in command:
        return None
    words = _shell_words(command)
    if not words or words[0].lower() != "git":
        return None

    index = 1
    if index < len(words) and words[index] == "-C":
        index += 2
    elif index < len(words) and words[index].lower() == "-c":
        return None
    if index >= len(words):
        return None

    def exact(raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return candidate.resolve(strict=False)

    if (
        words[index].lower() == "worktree"
        and index + 1 < len(words)
        and words[index + 1].lower() == "add"
    ):
        args = words[index + 2 :]
        destination: Optional[str] = None
        option_takes_value = {"-b", "-B", "--reason"}
        arg_index = 0
        while arg_index < len(args):
            token = args[arg_index]
            if token in option_takes_value:
                arg_index += 2
                continue
            if token.startswith("-"):
                arg_index += 1
                continue
            destination = token
            break
        return [exact(destination)] if destination else None

    if words[index].lower() != "clone":
        return None
    args = words[index + 1 :]
    option_takes_value = {
        "-b",
        "--branch",
        "--depth",
        "--filter",
        "--jobs",
        "-j",
        "--origin",
        "-o",
        "--reference",
        "--reference-if-able",
        "--separate-git-dir",
        "--shallow-exclude",
        "--template",
        "-u",
        "--upload-pack",
    }
    positional: list[str] = []
    extra_destinations: list[Path] = []
    arg_index = 0
    while arg_index < len(args):
        token = args[arg_index]
        lower = token.lower()
        if lower.startswith("--separate-git-dir="):
            extra_destinations.append(exact(token.split("=", 1)[1]))
            arg_index += 1
            continue
        if token in option_takes_value:
            if arg_index + 1 >= len(args):
                return None
            if token == "--separate-git-dir":
                extra_destinations.append(exact(args[arg_index + 1]))
            arg_index += 2
            continue
        if token.startswith("-"):
            arg_index += 1
            continue
        positional.append(token)
        arg_index += 1

    if len(positional) != 2:
        return None
    return [exact(positional[1]), *extra_destinations]


def _skill_mutation_targets(
    args: dict[str, Any],
) -> tuple[list[Path], str]:
    """Resolve the same skill target the registered writer will use."""

    action = str(args.get("action", "") or "").lower()
    if action not in _SKILL_MUTATION_ACTIONS:
        return [], ""
    name = str(args.get("name", "") or "").strip()
    if not name:
        return [], ""
    try:
        from tools import skill_manager_tool

        if action == "create":
            category = args.get("category")
            target = (
                skill_manager_tool._resolve_skill_dir(name)
                if category is None
                else skill_manager_tool._resolve_skill_dir(name, str(category))
            )
            return [target], ""

        existing = skill_manager_tool._find_skill(name)
        if not existing:
            return [], ""
        skill_dir = Path(existing["path"]).expanduser().resolve(strict=False)
        file_path = str(args.get("file_path", "") or "").strip()
        if action in {"patch", "write_file", "remove_file"} and file_path:
            target, error = skill_manager_tool._resolve_skill_target(
                skill_dir,
                file_path,
            )
            if error:
                # The handler will return its more specific validation error,
                # but the repository probe must still cover the skill root.
                return [skill_dir], ""
            if target is not None:
                return [target], ""
        if action == "delete":
            return [skill_dir], ""
        return [skill_dir / "SKILL.md"], ""
    except Exception as exc:
        return [], str(exc)


def _repo_mutation_targets(
    policy: TurnPolicy,
    function_name: str,
    args: dict[str, Any],
) -> tuple[list[Path], Optional[list[Path]], str]:
    return _repo_mutation_targets_at_cwd(
        policy,
        function_name,
        args,
        trusted_cwd=None,
    )


def _repo_mutation_targets_at_cwd(
    policy: TurnPolicy,
    function_name: str,
    args: dict[str, Any],
    *,
    trusted_cwd: Optional[Path | str],
) -> tuple[list[Path], Optional[list[Path]], str]:
    workdir = args.get("workdir") if function_name == "terminal" else None
    base = Path(
        workdir
        if isinstance(workdir, str) and workdir.strip()
        else trusted_cwd
        if trusted_cwd is not None and str(trusted_cwd).strip()
        else policy.workspace
    ).expanduser().resolve(strict=False)

    def exact_target(path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded.resolve(strict=False)
        return (base / expanded).resolve(strict=False)

    if function_name == "write_file":
        path = args.get("path")
        return (
            [exact_target(Path(path))]
            if isinstance(path, str) and path.strip()
            else []
        ), None, ""
    if function_name == "patch":
        paths = [exact_target(path) for path in _patch_paths(args)]
        return (paths or [base]), None, ""
    if function_name == "skill_manage":
        targets, error = _skill_mutation_targets(args)
        return targets, None, error
    if function_name == "terminal":
        command = args.get("command")
        if not isinstance(command, str) or not (
            _TERMINAL_REPO_MUTATION.search(command)
            or _terminal_embeds_local_mutation(command)
        ):
            return [], None, ""
        targets = [base]
        for match in _GIT_C_TARGET.finditer(command):
            target = next(
                (value for value in match.groups() if value),
                None,
            )
            if target:
                targets.append(exact_target(Path(target)))
        targets.extend(_mentioned_paths(command, base=base))
        # Preserve order for deterministic diagnostics while avoiding repeated
        # git probes for a workdir also named explicitly in the command.
        unique_targets = list(dict.fromkeys(targets))
        return (
            unique_targets,
            _bootstrap_destinations(command, base=base),
            "",
        )
    if function_name == "execute_code":
        code = args.get("code")
        if isinstance(code, str) and _EXECUTE_CODE_MUTATION.search(code):
            targets = [base]
            targets.extend(_mentioned_paths(code, base=base))
            return list(dict.fromkeys(targets)), None, ""
    return [], None, ""


def _investigation_effect_block(
    policy: TurnPolicy,
    function_name: str,
    args: Mapping[str, Any],
) -> Optional[str]:
    """Permit only tools explicitly registered as read-only in investigation."""

    if policy.phase is not RequestPhase.INVESTIGATION:
        return None
    try:
        from tools.registry import ToolEffect, registry

        effect = registry.get_effect(function_name, args)
    except Exception as exc:
        return (
            "Request-phase safety block: Hermes could not verify that "
            f"`{function_name}` is read-only ({exc}). No effect was executed."
        )
    if effect is ToolEffect.READ_ONLY:
        return None
    return (
        "Request-phase safety block: investigation permits only registered "
        f"read-only tools. `{function_name}` is classified as {effect.value}; "
        "no effect was executed. Ask for the operation or implementation "
        "explicitly if a change is intended."
    )


def _reprobe_mutation_snapshots(
    policy: TurnPolicy,
    snapshots: Iterable[RepoSnapshot],
) -> Optional[str]:
    """Fail closed if repository state drifted since the last landed effect."""

    unique = {
        _repo_key(snapshot.root): snapshot
        for snapshot in snapshots
    }
    for key, baseline in unique.items():
        prior_block = policy.repo_drift_block.get(key)
        if prior_block:
            return prior_block
        current, error = _probe_repo(baseline.root, check_dirty=True)
        if current is None or error:
            detail = error or "repository probe returned no snapshot"
            block = (
                "Repository safety block: Hermes could not revalidate the "
                f"target repository immediately before mutation: "
                f"{baseline.root} ({detail}). No mutation was executed."
            )
            policy.repo_drift_block[key] = block
            return block
        expected = policy.expected_repo_status.get(
            key,
            baseline.status_porcelain,
        )
        expected_head = policy.expected_repo_heads.get(key, baseline.head_oid)
        expected_tree = policy.expected_repo_trees.get(key, baseline.tree_oid)
        if (
            current.status_porcelain != expected
            or current.head_oid != expected_head
            or current.tree_oid != expected_tree
        ):
            block = (
                "Repository safety block: the target repository changed after "
                "this turn's last verified state. Preserve the concurrent work "
                f"at {baseline.root} and continue in a fresh isolated worktree."
            )
            policy.repo_drift_block[key] = block
            return block
    return None


def _tool_result_failed(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("error")) or result.get("success") is False
    if not isinstance(result, str):
        return True
    stripped = result.strip()
    if not stripped:
        return False
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return stripped.lower().startswith(("error", "[tool_error]"))
    if not isinstance(parsed, dict):
        return False
    if parsed.get("error") or parsed.get("success") is False:
        return True
    exit_code = parsed.get("exit_code")
    return exit_code is not None and exit_code != 0


def _tool_result_mapping(result: Any) -> Optional[dict[str, Any]]:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_effect_is_verified(
    function_name: str,
    args: dict[str, Any],
    result: Any,
) -> bool:
    """Accept progression receipts only from known, read-backed tool shapes."""

    parsed = _tool_result_mapping(result)
    if parsed is None or _tool_result_failed(parsed):
        return False
    if function_name == "write_file":
        return bool(parsed.get("resolved_path")) and bool(
            parsed.get("files_modified")
        )
    if function_name == "patch":
        return parsed.get("success") is True and bool(
            parsed.get("files_modified")
        )
    if function_name == "terminal":
        return (
            args.get("background") is not True
            and parsed.get("exit_code") == 0
        )
    if function_name == "skill_manage":
        return parsed.get("success") is True
    return False


def _verified_effect_may_change_commit_identity(
    function_name: str,
    args: dict[str, Any],
) -> bool:
    """Only an explicit successful Git lifecycle command may advance HEAD."""

    if function_name != "terminal":
        return False
    command = args.get("command")
    if not isinstance(command, str):
        return False
    return bool(
        re.search(
            r"\bgit\s+(?:-C\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+)?"
            r"(?:commit|am|cherry-pick|merge|rebase|reset|revert)\b",
            command,
            re.IGNORECASE,
        )
    )


def record_tool_effect_result(
    function_name: str,
    args: dict[str, Any],
    result: Any,
    *,
    trusted_cwd: Optional[Path | str] = None,
) -> None:
    """Advance the verified repository state only after a landed mutation."""

    policy = current_turn_policy()
    if policy is None or policy.phase is not RequestPhase.IMPLEMENTATION:
        return
    targets, _, resolution_error = _repo_mutation_targets_at_cwd(
        policy,
        function_name,
        args,
        trusted_cwd=trusted_cwd,
    )
    if resolution_error or not targets:
        return
    snapshots = [
        snapshot
        for target in targets
        if (snapshot := _snapshot_for_path(policy, target)) is not None
    ]
    failed = _tool_result_failed(result)
    verified = _tool_effect_is_verified(function_name, args, result)
    may_change_identity = _verified_effect_may_change_commit_identity(
        function_name,
        args,
    )
    with policy.lock:
        for baseline in {
            _repo_key(snapshot.root): snapshot
            for snapshot in snapshots
        }.values():
            key = _repo_key(baseline.root)
            current, error = _probe_repo(baseline.root, check_dirty=True)
            if current is None or error:
                policy.repo_drift_block[key] = (
                    "Repository safety block: Hermes could not read back the "
                    f"repository after `{function_name}` at {baseline.root}. "
                    "Preserve the work and continue only in a fresh isolated "
                    "worktree."
                )
                continue
            expected = policy.expected_repo_status.get(
                key,
                baseline.status_porcelain,
            )
            expected_head = policy.expected_repo_heads.get(
                key,
                baseline.head_oid,
            )
            expected_tree = policy.expected_repo_trees.get(
                key,
                baseline.tree_oid,
            )
            status_changed = current.status_porcelain != expected
            identity_changed = (
                current.head_oid != expected_head
                or current.tree_oid != expected_tree
            )
            if failed and (status_changed or identity_changed):
                policy.repo_drift_block[key] = (
                    "Repository safety block: a failed or interrupted "
                    f"`{function_name}` changed {baseline.root}. Preserve the "
                    "partial work and continue only in a fresh isolated "
                    "worktree."
                )
                continue
            if (
                not failed
                and identity_changed
                and not may_change_identity
            ):
                policy.repo_drift_block[key] = (
                    "Repository safety block: committed repository identity "
                    f"changed during `{function_name}` at {baseline.root}, "
                    "which that verified Hermes effect was not authorized to "
                    "do. Preserve the concurrent work and continue only in a "
                    "fresh isolated worktree."
                )
                continue
            if not failed and not verified and (status_changed or identity_changed):
                policy.repo_drift_block[key] = (
                    "Repository safety block: repository state changed during "
                    f"`{function_name}` at {baseline.root}, but Hermes did not "
                    "receive a verified effect receipt. Preserve the unknown "
                    "outcome and continue only in a fresh isolated worktree."
                )
                continue
            if not failed and verified:
                policy.expected_repo_status[key] = current.status_porcelain
                policy.expected_repo_heads[key] = current.head_oid
                policy.expected_repo_trees[key] = current.tree_oid


def guard_tool_call(
    function_name: str,
    args: dict[str, Any],
    *,
    trusted_cwd: Optional[Path | str] = None,
) -> Optional[str]:
    """Return a concise block reason, or ``None`` when the call is allowed."""

    suspension_reason = _local_mutation_suspension_reason()
    if suspension_reason and _local_mutation_requested(function_name, args):
        return (
            "Gateway shutdown safety block: local source and skill writes are "
            f"suspended during {suspension_reason}. Finish the shutdown; resume "
            "the work only in a fresh explicitly started run."
        )

    policy = current_turn_policy()
    if policy is None:
        return None

    if function_name == "skill_view":
        skill_name = str(args.get("name", "") or "").strip()
        skill_key = (
            skill_name
            if ":" in skill_name
            else re.split(r"[\\/]", skill_name)[-1]
        )
        file_path = str(args.get("file_path", "") or "").strip()
        if skill_name:
            with policy.lock:
                if (
                    policy.skill_payload_chars
                    >= MAX_SKILL_PAYLOAD_CHARS_PER_TURN
                ):
                    return _skill_payload_budget_error(policy)
                already_loaded = skill_key in policy.loaded_root_skills
                if file_path:
                    if not already_loaded:
                        return (
                            "Skill-selection safety block: load the root skill "
                            f"`{skill_name}` before requesting its supporting files."
                        )
                    return None
                if not already_loaded:
                    policy.loaded_root_skills.append(skill_key)

    if (
        function_name == "skill_manage"
        and str(args.get("action", "")).lower() in _SKILL_MUTATION_ACTIONS
        and policy.phase is not RequestPhase.IMPLEMENTATION
    ):
        return (
            f"Request-phase safety block: this turn is {policy.phase.value}, so it "
            "may inspect skills but may not rewrite them. Skill changes require an "
            "explicit implementation instruction."
        )

    # Arbitrary shell and Python cannot be made repository-read-only with a
    # write-pattern denylist. During an investigation, keep the small set of
    # proven read commands and require all other local execution to wait for
    # explicit implementation authority. Ordinary business operations retain
    # their native provider/RPC path.
    if policy.phase is RequestPhase.INVESTIGATION and function_name in {
        "terminal",
        "execute_code",
    }:
        context_paths = [policy.workspace]
        if trusted_cwd is not None and str(trusted_cwd).strip():
            context_paths.append(Path(trusted_cwd))
        workdir = args.get("workdir")
        if (
            function_name == "terminal"
            and isinstance(workdir, str)
            and workdir.strip()
        ):
            context_paths.append(Path(workdir))
        referenced_text = args.get(
            "command" if function_name == "terminal" else "code"
        )
        if isinstance(referenced_text, str):
            reference_base = next(
                (
                    Path(candidate).expanduser().resolve(strict=False)
                    for candidate in (
                        workdir,
                        trusted_cwd,
                        policy.workspace,
                    )
                    if candidate is not None and str(candidate).strip()
                ),
                policy.workspace,
            )
            context_paths.extend(
                _mentioned_paths(referenced_text, base=reference_base)
            )
        in_repository = any(
            _snapshot_for_path(policy, path) is not None
            for path in context_paths
        )
        in_skill_source = any(
            _protected_skill_root_for(path.expanduser().resolve(strict=False))
            is not None
            for path in context_paths
        )
        if in_repository or in_skill_source:
            if function_name == "execute_code":
                return (
                    "Request-phase safety block: this investigation may inspect "
                    "repository files but may not run arbitrary local code. Use "
                    "the read/search tools, or obtain an explicit implementation "
                    "instruction."
                )
            command = args.get("command")
            if not isinstance(command, str) or not _terminal_read_is_proven(
                command
            ):
                return (
                    "Request-phase safety block: this investigation allows only "
                    "proven read-only repository commands. Use the read/search "
                    "tools, or obtain an explicit implementation instruction."
                )

    targets, bootstrap_destinations, resolution_error = (
        _repo_mutation_targets_at_cwd(
            policy,
            function_name,
            args,
            trusted_cwd=trusted_cwd,
        )
    )
    if resolution_error:
        return (
            "Repository safety block: Hermes could not resolve the exact "
            f"mutation target before execution ({resolution_error})."
        )
    protected_skill_target = next(
        (
            (target, root)
            for target in targets
            if (root := _protected_skill_root_for(target)) is not None
        ),
        None,
    )
    if (
        protected_skill_target is not None
        and policy.phase is not RequestPhase.IMPLEMENTATION
    ):
        target, root = protected_skill_target
        return (
            f"Request-phase safety block: this turn is {policy.phase.value}, so "
            "installed skill/source inspection is allowed but local changes are "
            f"not. The protected target {target} is inside {root}. Continue "
            "read-only or obtain an explicit implementation instruction."
        )
    snapshots = [
        snapshot
        for target in targets
        if (snapshot := _snapshot_for_path(policy, target)) is not None
    ]
    if not snapshots:
        return _investigation_effect_block(policy, function_name, args)

    if policy.phase is not RequestPhase.IMPLEMENTATION:
        return (
            f"Request-phase safety block: this turn is {policy.phase.value}, so "
            "repository inspection is allowed but source changes are not. Continue "
            "read-only or obtain an explicit implementation instruction."
        )

    drift_block = _reprobe_mutation_snapshots(policy, snapshots)
    if drift_block is not None:
        return drift_block

    dirty_snapshots = [
        snapshot for snapshot in snapshots if snapshot.dirty
    ]
    if dirty_snapshots and bootstrap_destinations is not None:
        if bootstrap_destinations and all(
            not _path_is_within(destination, dirty.root)
            for destination in bootstrap_destinations
            for dirty in dirty_snapshots
        ):
            return None

    dirty = dirty_snapshots[0] if dirty_snapshots else None
    if dirty is not None:
        detail = (
            f" ({dirty.probe_error})" if dirty.probe_error else ""
        )
        return (
            "Repository safety block: the target repository was already dirty "
            f"when this turn captured its baseline: {dirty.root}{detail}. Preserve "
            "those changes and use a clean isolated worktree before editing."
        )

    return None


__all__ = [
    "MAX_SKILL_PAYLOAD_CHARS_PER_RESULT",
    "MAX_SKILL_PAYLOAD_CHARS_PER_TURN",
    "RequestPhase",
    "TurnPolicy",
    "activate_turn_policy",
    "classify_request_phase",
    "clear_turn_policy",
    "current_turn_policy",
    "enforce_skill_payload_budget",
    "guard_tool_call",
    "record_tool_effect_result",
    "push_turn_policy",
    "reset_turn_policy",
    "resume_local_mutations",
    "suspend_local_mutations",
]

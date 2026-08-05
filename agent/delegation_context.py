"""Context-local state for delegate_task child execution.

The parent Hermes process may itself be a Kanban dispatcher worker with
HERMES_KANBAN_* variables in process env. delegate_task children run inside the
same Python process, but they are not dispatcher-owned Kanban workers. This
module lets code paths that resolve tool schemas or spawn subprocesses fail
closed for delegated children without mutating global os.environ for the parent.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import os
import re
import shlex
from typing import Iterator, Mapping, MutableMapping


@dataclass(frozen=True)
class DelegatedApprovalScope:
    """Serializable approval boundary inherited by ``delegate_task`` children."""

    enabled: bool
    approved_mission_summary: str
    allowed_workspace_path: str
    allow_local_non_destructive: bool = True
    allow_external_transmission: bool = False
    allow_destructive: bool = False
    allow_credentials: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_TERMINAL_EXTERNAL_RE = re.compile(
    r"(?:https?://|\b(?:curl|wget|scp|sftp|ssh|rsync|nc|ncat|ftp|rclone|gsutil|"
    r"mail|sendmail|mutt)\b|\baws\s+s3\b|\b(?:npm|pnpm|yarn)\s+publish\b|"
    r"\btwine\s+upload\b|\bdocker\s+push\b|"
    r"\b(?:vercel|netlify)\s+(?:deploy|publish)\b|\bgit\b.*\b(?:clone|fetch|pull)\b)",
    re.IGNORECASE,
)
_TERMINAL_DESTRUCTIVE_RE = re.compile(
    r"(?:^|[;&|]\s*|\bsudo\s+)(?:rm|rmdir|unlink|shred|truncate|mkfs|dd|mv)\b|"
    r"\bfind\b[^;&|]*\s-delete\b",
    re.IGNORECASE,
)
_TERMINAL_CREDENTIAL_RE = re.compile(
    r"(?:\b(?:auth|login|logout|account|passwd|password|credential|secret|token|"
    r"keychain)\b|(?:^|[/\\])\.ssh(?:[/\\]|$)|(?:^|[/\\])\.env(?:\.|[/\\]|$)|"
    r"\[REDACTED PRIVATE KEY\]|«redacted|\*\*\*)",
    re.IGNORECASE,
)
_TERMINAL_GIT_CHANGE_RE = re.compile(
    r"(?:\bgit\b[^;&|]*\b(?:commit|push|merge|request-pull|reset|clean|tag|"
    r"branch|checkout|switch|rebase|cherry-pick|revert|stash)\b|"
    r"\b(?:gh\s+pr|glab\s+mr)\b)",
    re.IGNORECASE,
)
_TERMINAL_PAYMENT_RE = re.compile(
    r"\b(?:stripe|paypal|payment|checkout|invoice|charge|purchase|billing)\b",
    re.IGNORECASE,
)
_TERMINAL_GLOBAL_MANAGER_RE = re.compile(
    r"(?:\b(?:brew|apt|apt-get|yum|dnf|pacman|apk|choco|winget|pipx)\s+install\b|"
    r"\b(?:npm|pnpm|yarn)\b[^;&|]*(?:\s-g\b|\s--global\b)|"
    r"\b(?:cargo|gem|go)\s+install\b|\buv\s+tool\s+install\b|"
    r"\bdotnet\s+tool\s+install\b|\buv\s+pip\s+install\b[^;&|]*\s--system\b)",
    re.IGNORECASE,
)
_INLINE_EXEC_RE = re.compile(
    r"(?:^|\s|[;&|]\s*|\benv\s+)(?:[^\s;&|]*/)?"
    r"(?:python(?:\d+(?:\.\d+)*)?|node|ruby|perl)\s+(?:[^;&|]*\s)?-(?:c|e)\b|"
    r"(?:^|\s|[;&|]\s*|\benv\s+)(?:[^\s;&|]*/)?(?:sh|bash|zsh)\s+(?:[^;&|]*\s)?-c\b",
    re.IGNORECASE,
)

_LOCAL_PATH_TOOLS = frozenset({"read_file", "search_files", "write_file", "patch"})
_PATH_ARG_NAMES = frozenset({
    "path", "file_path", "filepath", "directory", "dir", "root", "cwd",
    "workdir", "input_path", "output_path", "source_path", "target_path",
})
_COMPACT_PATH_ARG_NAMES = frozenset(
    re.sub(r"[^a-z0-9]", "", key.lower()) for key in _PATH_ARG_NAMES
)
_OPERATION_KEYS = frozenset({
    "action", "operation", "op", "method", "mode", "command", "request_type", "type",
})


def _normalise_path(value: object) -> str:
    if not value:
        return ""
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))
    except (OSError, TypeError, ValueError):
        return ""


def _path_within(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except (OSError, ValueError):
        return False


def _scope_workspace(scope: DelegatedApprovalScope) -> str:
    return _normalise_path(scope.allowed_workspace_path)


def _resolve_local_path(value: object, task_id: str) -> str:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return ""
    try:
        from tools.file_tools import _resolve_path_for_task

        return _normalise_path(_resolve_path_for_task(str(value), task_id or "default"))
    except Exception:
        return ""


def _path_argument_values(value: object) -> list[object]:
    """Collect path-like arguments independent of snake/camel-case spelling."""
    paths: list[object] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                compact_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if compact_key in _COMPACT_PATH_ARG_NAMES:
                    if isinstance(child, (list, tuple)):
                        paths.extend(child)
                    elif child:
                        paths.append(child)
                elif isinstance(child, (dict, list, tuple)):
                    _walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(value)
    return paths


def _operation_tokens(value: object) -> set[str]:
    tokens: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).lower() in _OPERATION_KEYS and isinstance(child, str):
                    token = re.sub(r"[^a-z0-9]", "", child.lower())
                    if token:
                        tokens.add(token)
                elif isinstance(child, (dict, list, tuple)):
                    _walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(value)
    return tokens


def _truthy_nested(value: object, target_key: str) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() == target_key and child is True:
                return True
            if isinstance(child, (dict, list, tuple)) and _truthy_nested(child, target_key):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_truthy_nested(child, target_key) for child in value)
    return False


def _operations_match(operations: set[str], verbs: set[str]) -> bool:
    return any(
        operation == verb or operation.startswith(verb) or operation.endswith(verb)
        for operation in operations
        for verb in verbs
    )


def _external_tool_denial_reason(function_name: str, function_args: dict) -> str | None:
    name = function_name.lower()
    operations = _operation_tokens(function_args)
    email_draft_send = False

    if function_name == "skill_manage":
        return "skill_mutation"

    if "google_workspace" in name:
        if "manage_email" in name:
            sends = {"send", "sendemail", "reply", "replyall", "forward"}
            if _operations_match(operations, sends):
                email_draft_send = _truthy_nested(function_args, "draft")
                if not email_draft_send:
                    return "external_transmission"
        if "calendar" in name and _operations_match(operations, {
            "create", "update", "delete", "quickadd", "move", "patch",
        }):
            return "external_calendar_mutation"
        if "drive" in name and _operations_match(operations, {
            "upload", "share", "unshare", "update", "delete", "copy", "comment",
            "create", "move", "patch", "write",
        }):
            return "external_drive_mutation"
        if ("docs" in name or "document" in name) and _operations_match(operations, {
            "create", "write", "inserttext", "replacetext", "update", "delete", "patch",
            "batchupdate",
        }):
            return "external_docs_mutation"
        if "sheet" in name and _operations_match(operations, {
            "write", "create", "append", "update", "clear", "delete", "insert",
            "add", "remove", "move", "copy", "resize", "format", "batchupdate", "patch",
        }):
            return "external_sheets_mutation"

    if "browser" in name or "aside" in name:
        arbitrary = {"execute", "evaluate", "eval", "script", "javascript", "repl"}
        interactive = {"click", "type", "fill", "submit", "upload", "download", "drag"}
        has_arbitrary_payload = any(
            str(key).lower() in {"code", "script", "expression", "javascript"}
            for key in function_args
        )
        if has_arbitrary_payload or _operations_match(operations, arbitrary | interactive) or any(
            marker in name for marker in (
                "execute", "evaluate", "eval", "script", "javascript", "repl",
                "click", "type", "fill", "upload",
            )
        ):
            return "external_browser_mutation"

    if any(service in name for service in ("gjc", "pencil", "kordoc")):
        mutations = {"patch", "fill", "write", "create", "update", "delete", "insert", "replace", "apply"}
        name_mutations = mutations | {
            "batch", "batchdesign", "design", "execute", "modify", "render", "set",
        }
        compact_name = re.sub(r"[^a-z0-9]", "", name)
        if _operations_match(operations, mutations) or any(marker in compact_name for marker in name_mutations):
            return "external_document_mutation"

    if any(marker in name for marker in ("send_message", "publish", "upload")):
        return "external_transmission"
    if not email_draft_send and _operations_match(
        operations, {"send", "upload", "publish", "post", "share", "unshare"}
    ):
        return "external_transmission"
    if any(marker in name for marker in ("payment", "checkout", "purchase", "charge")):
        return "payment"
    if any(marker in name for marker in (
        "credential", "account_auth", "login", "logout", "oauth", "token", "secret",
    )):
        return "credentials_or_account"
    if any(marker in name for marker in (
        "git_commit", "git_push", "git_merge", "git_reset", "git_clean", "git_tag",
        "git_branch", "create_pr", "create_pull_request", "merge_pull_request",
    )):
        return "git_state_change"
    if _operations_match(operations, {"delete", "bulkmove"}) and "google_workspace" not in name:
        return "destructive"
    return None


def _known_scoped_read_tool(function_name: str, function_args: dict) -> bool:
    """Allow only reviewed read/draft operations on established providers."""
    name = function_name.lower()
    operations = _operation_tokens(function_args)
    read_verbs = {
        "get", "list", "read", "search", "agenda", "triage", "freebusy",
        "labels", "threads", "export", "download", "view", "snapshot",
    }

    if "google_workspace" in name:
        if "manage_email" in name and _truthy_nested(function_args, "draft"):
            return _operations_match(
                operations, {"send", "sendemail", "reply", "replyall", "forward"}
            )
        return bool(operations) and all(
            _operations_match({operation}, read_verbs) for operation in operations
        )
    if "aside" in name:
        return bool(operations) and all(
            _operations_match({operation}, {"get", "list", "read", "snapshot", "attach"})
            for operation in operations
        )
    if "pencil" in name:
        compact_name = re.sub(r"[^a-z0-9]", "", name)
        return any(marker in compact_name for marker in ("get", "snapshot"))
    return False


def customer_send_denial_reason(function_name: str, function_args: dict) -> str | None:
    """Return a reason only for customer-facing send/post operations.

    Draft creation is deliberately allowed. Upload/share mutations remain under
    the broader delegated-scope policy rather than this root send gate.
    """
    name = function_name.lower()
    compact_name = re.sub(r"[^a-z0-9]", "", name)
    operations = _operation_tokens(function_args)
    if "manage_email" in name:
        sends = {"send", "sendemail", "reply", "replyall", "forward"}
        if _operations_match(operations, sends) and not _truthy_nested(
            function_args, "draft"
        ):
            return "customer_send_approval_required"
        return None
    if any(marker in compact_name for marker in (
        "send", "reply", "forward", "publish", "postmessage",
        "addcomment", "createcomment", "replycomment",
    )) or _operations_match(
        operations, {"send", "reply", "replyall", "forward", "post", "publish"}
    ):
        return "customer_send_approval_required"
    return None


def customer_send_targets(function_args: dict) -> tuple[str, ...]:
    """Extract destination identifiers that an explicit approval must name."""
    target_keys = {
        "to", "cc", "bcc", "recipient", "recipients", "target",
        "channel", "channel_id", "chat_id", "room_id", "user_id",
        "message_id", "thread_id", "comment_id",
    }
    compact_target_keys = {
        re.sub(r"[^a-z0-9]", "", key.lower()) for key in target_keys
    }
    values: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                compact_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if compact_key in compact_target_keys:
                    if isinstance(child, str) and child.strip():
                        values.append(child.strip())
                    elif isinstance(child, (list, tuple)):
                        values.extend(
                            str(item).strip()
                            for item in child
                            if isinstance(item, (str, int)) and str(item).strip()
                        )
                elif isinstance(child, (dict, list, tuple)):
                    _walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(function_args)
    return tuple(dict.fromkeys(values))


def _venv_pip_install_is_scoped(tokens: list[str], workspace: str, base_dir: str) -> bool:
    lowered = [token.lower() for token in tokens]
    if "--user" in lowered or any(token == "sudo" for token in lowered):
        return False

    pip_index = None
    executable_index = None
    for index, token in enumerate(tokens):
        base = os.path.basename(token).lower()
        if re.fullmatch(r"pip\d*(?:\.\d+)*", base):
            pip_index = index
            executable_index = index
            break
        if re.fullmatch(r"python\d*(?:\.\d+)*", base):
            if index + 2 < len(tokens) and tokens[index + 1:index + 3] == ["-m", "pip"]:
                pip_index = index + 2
                executable_index = index
                break
    if pip_index is None or "install" not in lowered[pip_index + 1:]:
        return True

    install_args = tokens[pip_index + 1:]
    for index, token in enumerate(install_args):
        target = None
        if token in {"-t", "--target", "--prefix", "--root"} and index + 1 < len(install_args):
            target = install_args[index + 1]
        elif any(token.startswith(prefix) for prefix in ("--target=", "--prefix=", "--root=")):
            target = token.split("=", 1)[1]
        if target:
            resolved_target = _normalise_path(
                target if os.path.isabs(target) else os.path.join(base_dir, target)
            )
            return _path_within(resolved_target, workspace)

    executable = tokens[executable_index or 0]
    if "/" in executable or "\\" in executable:
        resolved = _normalise_path(
            executable if os.path.isabs(executable) else os.path.join(base_dir, executable)
        )
        components = {part.lower() for part in resolved.replace("\\", "/").split("/")}
        return _path_within(resolved, workspace) and bool(components & {".venv", "venv"})

    active_venv = _normalise_path(os.environ.get("VIRTUAL_ENV"))
    if active_venv and _path_within(active_venv, workspace):
        return True
    for index, token in enumerate(tokens[:-1]):
        if token in {"source", "."}:
            activate = _normalise_path(os.path.join(base_dir, tokens[index + 1]))
            components = {part.lower() for part in activate.replace("\\", "/").split("/")}
            if _path_within(activate, workspace) and bool(components & {".venv", "venv"}):
                return True
    return False


def delegated_terminal_denial_reason(
    scope: DelegatedApprovalScope,
    command: str,
    *,
    workdir: object = None,
    task_id: str = "default",
) -> str | None:
    """Classify unsafe shell calls and require parent execution for the rest."""
    if not scope.allow_local_non_destructive:
        return "local_non_destructive_disabled"
    workspace = _scope_workspace(scope)
    if not workspace:
        return "workspace_escape"
    if not isinstance(command, str) or not command.strip():
        return "unbounded_shell_execution"
    if any(marker in command for marker in ("$(", "`", "<<", "<(", ">(")) or _INLINE_EXEC_RE.search(command):
        return "unbounded_shell_execution"
    if _TERMINAL_EXTERNAL_RE.search(command):
        return "external_transmission"
    if _TERMINAL_DESTRUCTIVE_RE.search(command):
        return "destructive"
    if _TERMINAL_CREDENTIAL_RE.search(command):
        return "credentials_or_account"
    try:
        from agent.redact import redact_sensitive_text

        if redact_sensitive_text(command, force=True, redact_url_credentials=True) != command:
            return "credentials_or_account"
    except Exception:
        return "credentials_or_account"
    if _TERMINAL_GIT_CHANGE_RE.search(command):
        return "git_state_change"
    if _TERMINAL_PAYMENT_RE.search(command):
        return "payment"
    if _TERMINAL_GLOBAL_MANAGER_RE.search(command):
        return "global_package_install"

    try:
        from tools.file_tools import _resolve_base_dir

        task_base = _normalise_path(_resolve_base_dir(task_id or "default"))
    except Exception:
        task_base = ""
    base_dir = _normalise_path(
        workdir if workdir and os.path.isabs(str(workdir)) else os.path.join(task_base, str(workdir or ""))
    )
    if not _path_within(base_dir, workspace):
        return "workspace_escape"

    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return "unbounded_shell_execution"
    if not _venv_pip_install_is_scoped(tokens, workspace, base_dir):
        return "global_package_install"

    for token in tokens:
        candidate = token.strip().lstrip("0123456789<>").rstrip(",;)")
        if "=" in candidate and candidate.startswith("-"):
            candidate = candidate.split("=", 1)[1]
        if not candidate or candidate in {".", ".."} or "://" in candidate:
            if candidate == "..":
                return "workspace_escape"
            continue
        if "$" in candidate and ("/" in candidate or "\\" in candidate):
            return "workspace_escape"
        expanded = os.path.expandvars(os.path.expanduser(candidate))
        if os.path.isabs(expanded) or expanded.startswith(("./", "../")) or "/" in expanded or "\\" in expanded:
            resolved = _normalise_path(
                expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)
            )
            if not _path_within(resolved, workspace):
                return "workspace_escape"
    # A child that may write workspace files can create then execute a script;
    # regex inspection cannot make that shell boundary safe without a sandbox.
    return "terminal_requires_parent_execution"


def delegated_tool_denial_reason(
    scope: DelegatedApprovalScope,
    function_name: str,
    function_args: dict,
    *,
    task_id: str = "default",
) -> str | None:
    """Preflight one delegated child tool call without executing side effects."""
    if scope is None or not getattr(scope, "enabled", False):
        return None
    workspace = _scope_workspace(scope)
    if not workspace:
        return "workspace_escape"
    if not scope.allow_local_non_destructive:
        return "local_non_destructive_disabled"

    external_reason = _external_tool_denial_reason(function_name, function_args)
    if external_reason:
        return external_reason

    if function_name == "clarify":
        return "user_interaction"
    if function_name == "execute_code":
        # execute_code dispatches nested RPC tool calls without the owning agent
        # object, so its inner calls cannot inherit this approval scope safely.
        return "nested_tool_execution"
    if function_name == "terminal":
        return delegated_terminal_denial_reason(
            scope,
            str(function_args.get("command") or ""),
            workdir=function_args.get("workdir"),
            task_id=task_id,
        )

    lower_name = function_name.lower()
    paths = _path_argument_values(function_args)
    path_tool = function_name in _LOCAL_PATH_TOOLS or (
        ("file" in lower_name or "document" in lower_name or "kordoc" in lower_name)
        and bool(paths)
    ) or bool(paths)
    if path_tool:
        if not scope.allow_local_non_destructive:
            return "local_non_destructive_disabled"
        if function_name == "search_files" and not paths:
            paths = ["."]
        if function_name == "patch" and str(function_args.get("mode") or "") == "patch":
            patch_text = str(function_args.get("patch") or "")
            if re.search(r"^\*\*\* Delete File:", patch_text, re.MULTILINE):
                return "destructive"
            paths.extend(re.findall(r"^\*\*\* (?:Add|Update) File:\s*(.+)$", patch_text, re.MULTILINE))
        if not paths:
            return "workspace_escape"
        for raw_path in paths:
            resolved = _resolve_local_path(raw_path, task_id)
            if not _path_within(resolved, workspace):
                return "workspace_escape"
        return None

    if function_name == "todo" or _known_scoped_read_tool(
        function_name, function_args
    ):
        return None
    return "unapproved_tool"

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar(
    "hermes_delegated_child_context",
    default=False,
)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity.

    Child construction calls ``set_current_session_id`` internally, so even a
    context entered without an id must restore the parent's ContextVar.  Child
    execution passes its explicit id and receives it only for this scope.
    """
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        # Import lazily: session_context calls is_delegated_child_context() when
        # deciding whether the compatibility os.environ mirror is safe.
        from gateway.session_context import scoped_current_session_id

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    import os

    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(
        os.environ.get(DELEGATED_CHILD_ENV_MARKER)
    )


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Return *env* with dispatcher-only Kanban variables removed."""
    cleaned = dict(env)
    for key in KANBAN_ENV_KEYS:
        cleaned.pop(key, None)
    cleaned[DELEGATED_CHILD_ENV_MARKER] = "1"
    return cleaned


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return an env override only when delegated-child lineage must cross fork.

    Most subprocess call sites historically used ``env=None`` to inherit the
    process environment.  In a ``delegate_task`` child, inheriting as-is leaks
    parent dispatcher ``HERMES_KANBAN_*`` vars while losing the ContextVar in
    the new process.  This helper preserves normal ``env=None`` semantics for
    non-delegated calls, and only materializes a scrubbed env when the lineage
    marker must be propagated across a child-process boundary.
    """
    if not is_delegated_child_process_context():
        return None if env is None else dict(env)

    if env is None:
        import os

        env = os.environ
    return scrub_kanban_env(env)

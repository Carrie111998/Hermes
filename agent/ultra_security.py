"""Minimal multi-user security kernel for tool execution."""

from __future__ import annotations

import contextvars
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CURRENT_PRINCIPAL: contextvars.ContextVar["Principal | None"] = contextvars.ContextVar(
    "ultra_current_principal",
    default=None,
)
_CURRENT_SANDBOX_LEASE: contextvars.ContextVar["SandboxLease | None"] = contextvars.ContextVar(
    "ultra_current_sandbox_lease",
    default=None,
)


HIGH_RISK_TOOLS = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_press",
        "computer_use",
        "delegate_task",
        "cronjob",
        "send_message",
        "image_generate",
        "video_generate",
        "text_to_speech",
        "transcribe",
    }
)

WRITE_TOOLS = frozenset({"write_file", "patch", "memory", "todo"})
SANDBOX_LEASE_REQUIRED_TOOLS = frozenset({"terminal", "execute_code"})


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    workspace_id: str
    project_id: str
    user_id: str
    roles: tuple[str, ...] = ("member",)
    session_id: str = ""
    source: str = "context"

    def is_complete(self) -> bool:
        return all(
            bool(value)
            for value in (
                self.tenant_id,
                self.workspace_id,
                self.project_id,
                self.user_id,
            )
        )


@dataclass(frozen=True)
class SandboxLease:
    sandbox_id: str
    tenant_id: str
    workspace_id: str
    project_id: str
    session_id: str
    owner_user_id: str
    status: str = "active"
    expires_at: float = 0.0
    source: str = "context"

    def is_active(self, now: float | None = None) -> bool:
        if self.status != "active":
            return False
        if not self.expires_at:
            return True
        return self.expires_at > (now if now is not None else time.time())

    def matches_principal(self, principal: Principal) -> bool:
        return (
            self.tenant_id == principal.tenant_id
            and self.workspace_id == principal.workspace_id
            and self.project_id == principal.project_id
            and self.session_id == principal.session_id
            and self.owner_user_id == principal.user_id
        )


@dataclass(frozen=True)
class ToolRequest:
    tool_name: str
    args: dict[str, Any]
    task_id: str = ""
    session_id: str = ""
    tool_call_id: str = ""
    turn_id: str = ""
    api_request_id: str = ""
    sandbox_id: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    decision_id: str
    allowed: bool
    reason: str
    action: str
    risk: str
    tool_name: str
    created_at: float
    tenant_id: str = ""
    workspace_id: str = ""
    project_id: str = ""
    user_id: str = ""
    roles: tuple[str, ...] = ()
    session_id: str = ""
    task_id: str = ""
    tool_call_id: str = ""
    turn_id: str = ""
    api_request_id: str = ""
    arg_keys: tuple[str, ...] = ()
    sandbox_id: str = ""


def set_current_principal(principal: Principal | None) -> contextvars.Token:
    """Bind a server-resolved principal to the current agent turn."""

    return _CURRENT_PRINCIPAL.set(principal)


def reset_current_principal(token: contextvars.Token) -> None:
    _CURRENT_PRINCIPAL.reset(token)


def get_current_principal() -> Principal | None:
    return _CURRENT_PRINCIPAL.get()


def set_current_sandbox_lease(lease: SandboxLease | None) -> contextvars.Token:
    return _CURRENT_SANDBOX_LEASE.set(lease)


def reset_current_sandbox_lease(token: contextvars.Token) -> None:
    _CURRENT_SANDBOX_LEASE.reset(token)


def get_current_sandbox_lease() -> SandboxLease | None:
    return _CURRENT_SANDBOX_LEASE.get()


def issue_sandbox_lease(
    principal: Principal,
    *,
    sandbox_id: str = "",
    status: str = "active",
    expires_at: float | None = None,
    ttl_seconds: int = 8 * 60 * 60,
    source: str = "session_context",
) -> SandboxLease:
    return SandboxLease(
        sandbox_id=sandbox_id or f"sbx_{uuid.uuid4().hex}",
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
        project_id=principal.project_id,
        session_id=principal.session_id,
        owner_user_id=principal.user_id,
        status=status or "active",
        expires_at=expires_at if expires_at is not None else time.time() + ttl_seconds,
        source=source,
    )


def resolve_principal(*, session_id: str = "", user_id: str = "") -> Principal:
    """Resolve the current principal.

    P0 keeps Hermes-compatible local execution working by synthesizing a local
    owner principal when no server principal has been bound. Multi-user gateway
    code should bind a real Principal before the agent turn starts.
    """

    principal = get_current_principal()
    if principal is not None:
        return principal

    try:
        from gateway.session_context import get_session_env

        gateway_user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        gateway_session_id = get_session_env("HERMES_SESSION_ID", "")
        session_key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception:
        gateway_user_id = ""
        gateway_session_id = ""
        session_key = ""

    resolved_session_id = session_id or gateway_session_id or session_key or "local-session"
    resolved_user_id = user_id or gateway_user_id or "local-user"
    return Principal(
        tenant_id="local-tenant",
        workspace_id="local-workspace",
        project_id=resolved_session_id or "local-project",
        user_id=resolved_user_id,
        roles=("owner",),
        session_id=resolved_session_id,
        source="local_fallback",
    )


def classify_tool_risk(tool_name: str) -> str:
    if tool_name in HIGH_RISK_TOOLS:
        return "high"
    if tool_name in WRITE_TOOLS:
        return "write"
    return "read"


def action_for_tool(tool_name: str) -> str:
    if tool_name in {"image_generate", "video_generate", "text_to_speech", "transcribe"}:
        return "media.generate"
    if tool_name in {"terminal", "execute_code"}:
        return "sandbox.execute"
    if tool_name in {"write_file", "patch"}:
        return "file.write"
    if tool_name.startswith("browser_") or tool_name == "computer_use":
        return "browser.use"
    if tool_name == "send_message":
        return "message.send"
    if tool_name == "cronjob":
        return "cron.manage"
    return "tool.run"


class PolicyChecker:
    """Process-local authorization policy for the P0 kernel."""

    def authorize(
        self,
        principal: Principal | None,
        request: ToolRequest,
        sandbox_lease: SandboxLease | None = None,
    ) -> PolicyDecision:
        risk = classify_tool_risk(request.tool_name)
        action = action_for_tool(request.tool_name)
        decision_id = f"dec_{uuid.uuid4().hex}"
        arg_keys = tuple(sorted(str(k) for k in request.args.keys()))

        if principal is None or not principal.is_complete():
            return PolicyDecision(
                decision_id=decision_id,
                allowed=False,
                reason="missing_principal",
                action=action,
                risk=risk,
                tool_name=request.tool_name,
                created_at=time.time(),
                session_id=request.session_id,
                task_id=request.task_id,
                tool_call_id=request.tool_call_id,
                turn_id=request.turn_id,
                api_request_id=request.api_request_id,
                arg_keys=arg_keys,
                sandbox_id=request.sandbox_id,
            )

        roles = tuple(principal.roles or ())
        if "blocked" in roles:
            return self._decision(
                decision_id,
                False,
                "principal_blocked",
                action,
                risk,
                principal,
                request,
                arg_keys,
                sandbox_id=request.sandbox_id,
            )

        if risk in {"high", "write"} and not set(roles).intersection(
            {"owner", "admin", "member"}
        ):
            return self._decision(
                decision_id,
                False,
                "insufficient_role",
                action,
                risk,
                principal,
                request,
                arg_keys,
                sandbox_id=request.sandbox_id,
            )

        if (
            request.tool_name in SANDBOX_LEASE_REQUIRED_TOOLS
            and principal.source != "local_fallback"
        ):
            lease = sandbox_lease
            if lease is None:
                return self._decision(
                    decision_id,
                    False,
                    "missing_sandbox_lease",
                    action,
                    risk,
                    principal,
                    request,
                    arg_keys,
                    sandbox_id=request.sandbox_id,
                )
            if not lease.is_active():
                return self._decision(
                    decision_id,
                    False,
                    "sandbox_lease_inactive",
                    action,
                    risk,
                    principal,
                    request,
                    arg_keys,
                    sandbox_id=lease.sandbox_id,
                )
            if not lease.matches_principal(principal):
                return self._decision(
                    decision_id,
                    False,
                    "sandbox_lease_mismatch",
                    action,
                    risk,
                    principal,
                    request,
                    arg_keys,
                    sandbox_id=lease.sandbox_id,
                )
            request = ToolRequest(
                tool_name=request.tool_name,
                args=request.args,
                task_id=request.task_id,
                session_id=request.session_id,
                tool_call_id=request.tool_call_id,
                turn_id=request.turn_id,
                api_request_id=request.api_request_id,
                sandbox_id=lease.sandbox_id,
            )

        return self._decision(
            decision_id,
            True,
            "allowed",
            action,
            risk,
            principal,
            request,
            arg_keys,
            sandbox_id=request.sandbox_id,
        )

    @staticmethod
    def _decision(
        decision_id: str,
        allowed: bool,
        reason: str,
        action: str,
        risk: str,
        principal: Principal,
        request: ToolRequest,
        arg_keys: tuple[str, ...],
        sandbox_id: str = "",
    ) -> PolicyDecision:
        return PolicyDecision(
            decision_id=decision_id,
            allowed=allowed,
            reason=reason,
            action=action,
            risk=risk,
            tool_name=request.tool_name,
            created_at=time.time(),
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            project_id=principal.project_id,
            user_id=principal.user_id,
            roles=tuple(principal.roles or ()),
            session_id=request.session_id or principal.session_id,
            task_id=request.task_id,
            tool_call_id=request.tool_call_id,
            turn_id=request.turn_id,
            api_request_id=request.api_request_id,
            arg_keys=arg_keys,
            sandbox_id=sandbox_id or request.sandbox_id,
        )


class DecisionLog:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _path(self) -> Path:
        if self.path is not None:
            return self.path
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "logs" / "security_decisions.jsonl"

    def append(self, decision: PolicyDecision) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = decision_record(decision)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            # chmod is best-effort on filesystems that do not support POSIX modes.
            return


def decision_record(decision: PolicyDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "action": decision.action,
        "risk": decision.risk,
        "tool_name": decision.tool_name,
        "created_at": decision.created_at,
        "tenant_id": decision.tenant_id,
        "workspace_id": decision.workspace_id,
        "project_id": decision.project_id,
        "user_id": decision.user_id,
        "roles": list(decision.roles),
        "session_id": decision.session_id,
        "task_id": decision.task_id,
        "tool_call_id": decision.tool_call_id,
        "turn_id": decision.turn_id,
        "api_request_id": decision.api_request_id,
        "arg_keys": list(decision.arg_keys),
        "sandbox_id": decision.sandbox_id,
    }


def decision_to_trace(decision: PolicyDecision) -> dict[str, Any]:
    return {
        "name": "ultra_policy_checker",
        "decision_id": decision.decision_id,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "action": decision.action,
        "risk": decision.risk,
    }


def decision_to_tool_error(decision: PolicyDecision) -> str:
    return json.dumps(
        {
            "error": "Tool blocked by policy",
            "error_type": "policy_denied",
            "decision_id": decision.decision_id,
            "reason": decision.reason,
            "action": decision.action,
            "tool_name": decision.tool_name,
        },
        ensure_ascii=False,
    )


def authorize_tool_call(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    sandbox_id: str = "",
    principal: Principal | None = None,
    sandbox_lease: SandboxLease | None = None,
    decision_log: DecisionLog | None = None,
    checker: PolicyChecker | None = None,
) -> PolicyDecision:
    request = ToolRequest(
        tool_name=tool_name,
        args=args if isinstance(args, dict) else {},
        task_id=task_id or "",
        session_id=session_id or "",
        tool_call_id=tool_call_id or "",
        turn_id=turn_id or "",
        api_request_id=api_request_id or "",
        sandbox_id=sandbox_id or "",
    )
    resolved_principal = principal if principal is not None else resolve_principal(
        session_id=request.session_id
    )
    resolved_lease = sandbox_lease if sandbox_lease is not None else get_current_sandbox_lease()
    policy = checker or PolicyChecker()
    decision = policy.authorize(resolved_principal, request, resolved_lease)
    log = decision_log or DecisionLog()
    try:
        log.append(decision)
    except Exception as exc:
        decision = PolicyDecision(
            decision_id=f"dec_{uuid.uuid4().hex}",
            allowed=False,
            reason=f"decision_log_unavailable:{type(exc).__name__}",
            action=request.tool_name,
            risk=classify_tool_risk(request.tool_name),
            tool_name=request.tool_name,
            created_at=time.time(),
            tenant_id=getattr(resolved_principal, "tenant_id", ""),
            workspace_id=getattr(resolved_principal, "workspace_id", ""),
            project_id=getattr(resolved_principal, "project_id", ""),
            user_id=getattr(resolved_principal, "user_id", ""),
            roles=tuple(getattr(resolved_principal, "roles", ()) or ()),
            session_id=request.session_id,
            task_id=request.task_id,
            tool_call_id=request.tool_call_id,
            turn_id=request.turn_id,
            api_request_id=request.api_request_id,
            arg_keys=tuple(sorted(str(k) for k in request.args.keys())),
            sandbox_id=request.sandbox_id,
        )
    return decision

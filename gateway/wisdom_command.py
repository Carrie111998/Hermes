"""Profile-scoped `/wisdom` command controller for messaging gateways.

The controller owns parsing and presentation-neutral navigation.  It calls the
same :class:`hermes_wisdom.service.WisdomService` used by CLI, Dashboard, and
Desktop; transports only render the returned cards and callbacks.
"""

from __future__ import annotations

import secrets
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from hermes_wisdom.client import (
    WisdomAuthError,
    WisdomConflict,
    WisdomNotFound,
    WisdomValidationError,
)
from hermes_wisdom.package import PackagePolicyError
from hermes_wisdom.service import WisdomService, portal_base_url


PAGE_SIZE = 5
TOKEN_TTL_SECONDS = 600
_ALIASES = {"list": "browse", "suggest": "submit"}
_PRIVATE_COMMANDS = {
    "setup",
    "status",
    "candidates",
    "submit",
    "drafts",
    "review",
    "install",
    "installed",
    "check",
    "update",
    "uninstall",
    "notifications",
}


@dataclass(frozen=True)
class WisdomCommandContext:
    user_id: str
    chat_id: str
    profile: str | None
    organization_id: str | None
    is_group: bool = False


@dataclass
class WisdomAction:
    label: str
    operation: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    url: str | None = None
    primary: bool = False
    destructive: bool = False
    callback_data: str | None = None


@dataclass
class WisdomItem:
    title: str
    detail: str = ""
    actions: list[WisdomAction] = field(default_factory=list)


@dataclass
class WisdomView:
    title: str
    summary: str = ""
    items: list[WisdomItem] = field(default_factory=list)
    actions: list[WisdomAction] = field(default_factory=list)
    notice: str | None = None

    def to_text(self) -> str:
        lines = [self.title]
        if self.summary:
            lines.extend(("", self.summary))
        for item in self.items:
            lines.extend(("", item.title))
            if item.detail:
                lines.append(item.detail)
            lines.extend(
                f"{action.label}: {action.url}"
                for action in item.actions
                if action.url
            )
        if self.notice:
            lines.extend(("", self.notice))
        lines.extend(
            f"{action.label}: {action.url}"
            for action in self.actions
            if action.url
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class _Token:
    operation: str
    arguments: dict[str, Any]
    user_id: str
    chat_id: str
    profile: str | None
    organization_id: str | None
    expires_at: float
    allow_dm_continuation: bool = False


class _CallbackTokens:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, _Token] = {}

    def issue(
        self,
        action: WisdomAction,
        context: WisdomCommandContext,
        *,
        allow_dm_continuation: bool = False,
    ) -> str:
        if not action.operation:
            raise ValueError("callback action has no operation")
        now = time.monotonic()
        token = secrets.token_urlsafe(9)
        value = _Token(
            operation=action.operation,
            arguments=dict(action.arguments),
            user_id=context.user_id,
            chat_id=context.chat_id,
            profile=context.profile,
            organization_id=context.organization_id,
            expires_at=now + TOKEN_TTL_SECONDS,
            allow_dm_continuation=allow_dm_continuation,
        )
        with self._lock:
            self._values = {
                key: item for key, item in self._values.items() if item.expires_at > now
            }
            self._values[token] = value
            while len(self._values) > 512:
                self._values.pop(next(iter(self._values)))
        return token

    def resolve(
        self,
        token: str,
        context: WisdomCommandContext,
        *,
        consume: bool,
    ) -> _Token:
        now = time.monotonic()
        with self._lock:
            value = self._values.get(token)
            if value is None or value.expires_at <= now:
                self._values.pop(token, None)
                raise ValueError(
                    "This Collective Wisdom control expired. Run /wisdom again."
                )
            same_chat = value.chat_id == context.chat_id
            dm_continuation = value.allow_dm_continuation and not context.is_group
            if (
                value.user_id != context.user_id
                or (not same_chat and not dm_continuation)
                or value.profile != context.profile
                or value.organization_id != context.organization_id
            ):
                raise PermissionError(
                    "This Collective Wisdom control belongs to another session."
                )
            if consume:
                self._values.pop(token, None)
            return value


CALLBACK_TOKENS = _CallbackTokens()


def bind_view_callbacks(view: WisdomView, context: WisdomCommandContext) -> WisdomView:
    """Bind every non-URL action to a short, scoped Telegram callback."""
    for action in [*view.actions, *(a for item in view.items for a in item.actions)]:
        if action.operation and not action.url:
            token = CALLBACK_TOKENS.issue(action, context)
            action.callback_data = f"wi:cmd:{token}"
    return view


def issue_continuation(raw_args: str, context: WisdomCommandContext) -> str:
    return CALLBACK_TOKENS.issue(
        WisdomAction("Continue in DM", "command", {"raw_args": raw_args}),
        context,
        allow_dm_continuation=True,
    )


def resolve_continuation(token: str, context: WisdomCommandContext) -> str:
    value = CALLBACK_TOKENS.resolve(token, context, consume=True)
    if value.operation != "command":
        raise ValueError("Invalid Collective Wisdom continuation.")
    return str(value.arguments.get("raw_args") or "")


def command_error_text(exc: Exception) -> str:
    """Return a stable user-safe error without leaking upstream response data."""
    if isinstance(exc, WisdomNotFound):
        return "That Collective Wisdom item was not found."
    if isinstance(exc, WisdomAuthError):
        return (
            "Sign in again or ask your team administrator for Collective Wisdom access."
        )
    if isinstance(exc, WisdomConflict):
        return (
            "The item changed since this control was created. "
            "Open it again to see its current state."
        )
    if isinstance(exc, (WisdomValidationError, PackagePolicyError, ValueError)):
        return str(exc)
    return "Collective Wisdom is temporarily unavailable. Try again shortly."


class WisdomCommandController:
    """Parse `/wisdom` keywords and orchestrate one active-profile service."""

    @staticmethod
    def parse(raw_args: str) -> tuple[str, list[str]]:
        try:
            parts = shlex.split(raw_args or "")
        except ValueError as exc:
            raise ValueError(f"Could not parse /wisdom arguments: {exc}") from exc
        keyword = _ALIASES.get(parts[0].lower(), parts[0].lower()) if parts else "home"
        return keyword, parts[1:]

    def execute(
        self,
        raw_args: str,
        service: WisdomService,
        context: WisdomCommandContext,
    ) -> WisdomView:
        keyword, args = self.parse(raw_args)
        if context.is_group and keyword == "home":
            return WisdomView(
                "Collective Wisdom",
                "Browse skills published by your team. Private skills and device changes continue in a direct message.",
                actions=[
                    WisdomAction("Browse", "browse", primary=True),
                    WisdomAction("Continue in DM", "continue_dm", {"raw_args": ""}),
                    WisdomAction("Help", "help"),
                ],
            )
        if context.is_group and keyword in _PRIVATE_COMMANDS:
            return WisdomView(
                "Collective Wisdom",
                "Private skills, device state, and mutations are available in a direct message.",
                actions=[
                    WisdomAction(
                        "Continue in DM",
                        "continue_dm",
                        {"raw_args": raw_args},
                        primary=True,
                    )
                ],
            )
        handlers = {
            "home": self._home,
            "help": self._help,
            "setup": self._setup,
            "status": self._status,
            "browse": self._browse,
            "show": self._show,
            "versions": self._versions,
            "candidates": self._candidates,
            "submit": self._submit,
            "drafts": self._drafts,
            "review": self._review,
            "install": self._install,
            "installed": self._installed,
            "check": self._check,
            "update": self._update,
            "uninstall": self._uninstall,
            "notifications": self._notifications,
        }
        handler = handlers.get(keyword)
        if handler is None:
            view = self._help(service, [])
            view.notice = f"Unknown /wisdom keyword: {keyword}"
            return view
        if keyword == "show":
            view = self._show(
                service,
                args,
                include_compatibility=not context.is_group,
            )
        elif keyword == "versions":
            view = self._versions(service, args)
        else:
            view = handler(service, args)
        if context.is_group:
            self._make_group_safe(view, raw_args=raw_args)
        return view

    def execute_token(
        self,
        token: str,
        service: WisdomService,
        context: WisdomCommandContext,
    ) -> WisdomView:
        value = CALLBACK_TOKENS.resolve(token, context, consume=False)
        operation, args = value.operation, value.arguments
        navigation = {
            "home": "",
            "browse": "browse " + str(args.get("query") or ""),
            "show": "show " + str(args.get("skill") or ""),
            "versions": "versions " + str(args.get("skill") or ""),
            "candidates": "candidates " + str(args.get("query") or ""),
            "drafts": "drafts",
            "installed": "installed",
            "notifications": "notifications",
            "status": "status",
            "setup": "setup",
            "help": "help",
        }
        if operation == "browse_page":
            view = self._browse(
                service,
                [str(args.get("query") or "")],
                page=max(0, int(args.get("page") or 0)),
            )
            if context.is_group:
                self._make_group_safe(view, raw_args="browse")
            return view
        if operation == "versions_page":
            view = self._versions(
                service,
                [str(args.get("skill") or "")],
                page=max(0, int(args.get("page") or 0)),
            )
            if context.is_group:
                self._make_group_safe(
                    view,
                    raw_args=f"versions {args.get('skill') or ''}".strip(),
                )
            return view
        if operation in navigation:
            return self.execute(navigation[operation], service, context)

        # Navigation remains reusable for ten minutes. Mutation controls are
        # consumed only after the authoritative operation succeeds, so a
        # transient Gateway failure leaves the original card retryable.
        def complete(view: WisdomView) -> WisdomView:
            CALLBACK_TOKENS.resolve(token, context, consume=True)
            return view

        if operation == "setup_confirm":
            result = service.setup(disclosure_accepted=True)
            return complete(
                WisdomView(
                    "Collective Wisdom is ready",
                    f"Team: {result['organization_id']}",
                )
            )
        if operation == "submit":
            result = service.prepare_local_submission(str(args["skill_name"]))
            draft = result["draft"]
            return complete(
                self._draft_view(draft, portal_url=str(result["portal_url"]))
            )
        if operation == "publish":
            draft_id = str(args["draft_id"])
            result = service.approve_owner_draft(draft_id)
            state = str(result.get("publication_state") or "published")
            return complete(
                WisdomView("Skill submitted", self._publication_state_text(state))
            )
        if operation == "decline":
            result = service.decline_owner_draft(str(args["draft_id"]))
            if result.get("state") == "published":
                return complete(
                    WisdomView(
                        "Skill already published",
                        "The Portal completed publication before this action.",
                    )
                )
            return complete(
                WisdomView("Draft declined", "These exact bytes will not be published.")
            )
        if operation == "install_modes":
            return self._install_modes(str(args["reference"]))
        if operation == "install_plan":
            plan = service.install_plan(
                str(args["reference"]), update_mode=args.get("update_mode")
            )
            return complete(self._plan_view(plan, kind="install"))
        if operation == "install_apply":
            result = service.install_apply(str(args["receipt"]), accept_partial=False)
            return complete(
                WisdomView(
                    "Skill installed",
                    f"{result.get('skill_id')} · v{result.get('version')}",
                    actions=[WisdomAction("Installed skills", "installed")],
                )
            )
        if operation == "update_plan":
            plan = service.update_plan(str(args["skill_id"]))
            if not plan.get("receipt"):
                return complete(
                    WisdomView("Skill is current", "No update is available.")
                )
            return complete(self._plan_view(plan, kind="update"))
        if operation == "update_apply":
            result = service.update_apply(str(args["receipt"]))
            return complete(
                WisdomView(
                    "Skill updated",
                    f"{result.get('skill_id')} · v{result.get('version')}",
                    actions=[WisdomAction("Installed skills", "installed")],
                )
            )
        if operation == "check":
            result = service.check(apply_automatic=True)
            return complete(self._check_view(result))
        if operation == "uninstall_confirm":
            skill_id = str(args["skill_id"])
            return WisdomView(
                "Remove managed skill?",
                "The validated managed copy will move to recoverable Wisdom trash.",
                actions=[
                    WisdomAction("Cancel", "installed"),
                    WisdomAction(
                        "Uninstall",
                        "uninstall_apply",
                        {"skill_id": skill_id},
                        destructive=True,
                    ),
                ],
            )
        if operation == "uninstall_apply":
            result = service.uninstall(str(args["skill_id"]))
            return complete(
                WisdomView(
                    "Skill uninstalled",
                    str(result.get("skill_id") or args["skill_id"]),
                )
            )
        if operation == "mark_notifications":
            service.notifications(mark_seen=True)
            return complete(WisdomView("Notifications marked read"))
        raise ValueError("Invalid Collective Wisdom action.")

    @staticmethod
    def _make_group_safe(view: WisdomView, *, raw_args: str) -> None:
        """Remove device/private actions from group cards in-place."""
        safe_operations = {
            "browse",
            "browse_page",
            "show",
            "versions",
            "versions_page",
            "help",
        }
        removed = False

        def keep(action: WisdomAction) -> bool:
            nonlocal removed
            allowed = bool(action.url) or action.operation in safe_operations
            removed = removed or not allowed
            return allowed

        view.actions = [action for action in view.actions if keep(action)]
        for item in view.items:
            item.actions = [action for action in item.actions if keep(action)]
        if removed and not any(
            action.operation == "continue_dm" for action in view.actions
        ):
            view.actions.append(
                WisdomAction(
                    "Continue in DM",
                    "continue_dm",
                    {"raw_args": raw_args},
                    primary=True,
                )
            )

    def _home(self, service: WisdomService, _args: list[str]) -> WisdomView:
        data = service.command_home()
        status = data["status"]
        if not status.get("configured"):
            return WisdomView(
                "Collective Wisdom",
                "Set up this profile to share and install team skills.",
                actions=[
                    WisdomAction("Set up", "setup", primary=True),
                    WisdomAction("Status", "status"),
                    WisdomAction("Help", "help"),
                ],
            )
        if not status.get("gateway_available"):
            authentication = status.get("error_kind") == "authentication"
            return WisdomView(
                "Sign in to use Collective Wisdom"
                if authentication
                else "Collective Wisdom is temporarily unavailable",
                "Reconnect your Nous account and try again."
                if authentication
                else "Your local skills remain unchanged. Try again shortly.",
                actions=[
                    WisdomAction("Status", "status"),
                    WisdomAction("Open Nous Portal ↗", url=portal_base_url()),
                ],
            )
        if (
            not status.get("capability_advertised", True)
            or not status.get("entitled", True)
            or status.get("dogfood_admin_claim") is False
        ):
            return WisdomView(
                "Collective Wisdom access is not enabled",
                "This profile does not currently have access to your team's Collective Wisdom plane.",
                actions=[
                    WisdomAction("Status", "status"),
                    WisdomAction("Open Nous Portal ↗", url=portal_base_url()),
                ],
            )
        counts = data["counts"]
        summary = (
            f"Team: {data.get('organization_id')}\n"
            f"{counts['published']} shared · {counts['suggested']} suggested · "
            f"{counts['drafts']} drafts · {counts['installed']} installed"
        )
        return WisdomView(
            "Collective Wisdom",
            summary,
            actions=[
                WisdomAction("Browse", "browse"),
                WisdomAction("Suggested", "candidates"),
                WisdomAction("Drafts", "drafts"),
                WisdomAction("Installed", "installed"),
                WisdomAction("Updates", "check"),
                WisdomAction("Notifications", "notifications"),
            ],
        )

    def _help(self, _service: WisdomService, _args: list[str]) -> WisdomView:
        return WisdomView(
            "Collective Wisdom commands",
            "Use /wisdom <keyword>. Private state and changes are available in DM.",
            items=[
                WisdomItem(
                    "Discover", "browse [query] · show <skill> · versions <skill>"
                ),
                WisdomItem(
                    "Contribute",
                    "candidates [all|query] · submit <local-skill> · drafts · review <draft>",
                ),
                WisdomItem(
                    "Manage",
                    "install <id|URL|id@vN> · installed · check · update <skill|all> · uninstall <skill>",
                ),
                WisdomItem("Account", "setup · status · notifications · help"),
            ],
        )

    def _setup(self, service: WisdomService, _args: list[str]) -> WisdomView:
        return WisdomView(
            "Set up Collective Wisdom",
            "Candidate qualification stays on this profile. Only owner-approved private draft bytes, author copy, manifest metadata, and managed-install state reach the Gateway.",
            actions=[
                WisdomAction("I understand — set up", "setup_confirm", primary=True)
            ],
        )

    def _status(self, service: WisdomService, _args: list[str]) -> WisdomView:
        data = service.status()
        pending = len(data.get("pending_operations") or [])
        degraded = []
        if not data.get("gateway_available"):
            degraded.append(
                "authentication required"
                if data.get("error_kind") == "authentication"
                else "Gateway unavailable"
            )
        if not data.get("capability_advertised"):
            degraded.append("Wisdom capability unavailable")
        if not data.get("entitled"):
            degraded.append("entitlement unavailable")
        if data.get("setup_required_reason") == "organization_changed":
            degraded.append("organization changed; setup required")
        return WisdomView(
            "Collective Wisdom status",
            "\n".join([
                f"Setup: {'ready' if data.get('configured') else 'required'}",
                f"Gateway: {'available' if data.get('gateway_available') else 'unavailable'}",
                f"Team: {data.get('verified_org_id') or 'not verified'}",
                f"Installation: {data.get('installation_id') or 'not registered'}",
                f"Entitlement: {', '.join(data.get('display_scopes') or []) or 'none'}",
                f"Local store: ready · {pending} pending operation(s)",
                f"State: {', '.join(degraded) if degraded else 'healthy'}",
            ]),
        )

    def _browse(
        self,
        service: WisdomService,
        args: list[str],
        *,
        page: int = 0,
    ) -> WisdomView:
        query = " ".join(args).strip()
        matches = service.search_skills(query)
        start = page * PAGE_SIZE
        skills = matches[start : start + PAGE_SIZE]
        actions: list[WisdomAction] = []
        if page > 0:
            actions.append(
                WisdomAction(
                    "Previous",
                    "browse_page",
                    {"query": query, "page": page - 1},
                )
            )
        if start + PAGE_SIZE < len(matches):
            actions.append(
                WisdomAction(
                    "Next",
                    "browse_page",
                    {"query": query, "page": page + 1},
                )
            )
        return WisdomView(
            "Shared skills",
            (
                f"Results for “{query}” · page {page + 1}"
                if query
                else f"Skills published by your team · page {page + 1}"
            ),
            items=[self._skill_item(item) for item in skills],
            actions=actions,
            notice=None if skills else "No matching shared skills.",
        )

    def _skill_item(self, item: dict[str, Any]) -> WisdomItem:
        skill_id = str(item.get("id") or "")
        version = item.get("latest_version")
        return WisdomItem(
            str(item.get("slug") or skill_id),
            f"v{version or '?'} · {item.get('author_description') or 'No description'}",
            actions=[WisdomAction("View", "show", {"skill": skill_id})],
        )

    def _show(
        self,
        service: WisdomService,
        args: list[str],
        *,
        include_compatibility: bool = True,
    ) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom show <skill>")
        detail = service.resolve_skill(
            args[0],
            include_compatibility=include_compatibility,
        )
        skill = detail.get("skill") or {}
        skill_id = str(skill.get("id") or args[0])
        versions = detail.get("versions") or []
        latest = max(
            (int(v["version"]) for v in versions if isinstance(v.get("version"), int)),
            default=None,
        )
        compatibility = detail.get("local_compatibility") or {}
        latest_detail = detail.get("latest_version_detail") or {}
        latest_version = latest_detail.get("version") or {}
        specification = latest_version.get("system_spec") or {}
        requirements = self._requirements_summary(specification)
        installation = detail.get("local_installation") or {}
        installation_text = (
            f"Installed: v{installation.get('version')} · "
            f"{installation.get('update_mode')}"
            if installation
            else "Installed: no"
        )
        return WisdomView(
            str(skill.get("slug") or skill_id),
            "\n".join([
                str(skill.get("author_description") or "No description"),
                f"Latest: v{latest or '?'} · scan: {skill.get('scan_verdict') or 'not reported'}",
                f"Compatibility: {compatibility.get('outcome') or 'review on install'}",
                f"Requirements: {requirements}",
                installation_text,
            ]),
            actions=[
                WisdomAction(
                    "Install", "install_modes", {"reference": skill_id}, primary=True
                ),
                WisdomAction("Versions", "versions", {"skill": skill_id}),
                WisdomAction(
                    "View in Portal ↗", url=self._portal_skill_url(service, skill_id)
                ),
            ],
        )

    def _versions(
        self,
        service: WisdomService,
        args: list[str],
        *,
        page: int = 0,
    ) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom versions <skill>")
        detail = service.resolve_skill(args[0], include_compatibility=False)
        skill = detail.get("skill") or {}
        skill_id = str(skill.get("id") or args[0])
        all_versions = sorted(
            detail.get("versions") or [],
            key=lambda item: int(item.get("version") or 0),
            reverse=True,
        )
        start = page * PAGE_SIZE
        versions = all_versions[start : start + PAGE_SIZE]
        actions: list[WisdomAction] = []
        if page > 0:
            actions.append(
                WisdomAction(
                    "Previous",
                    "versions_page",
                    {"skill": skill_id, "page": page - 1},
                )
            )
        if start + PAGE_SIZE < len(all_versions):
            actions.append(
                WisdomAction(
                    "Next",
                    "versions_page",
                    {"skill": skill_id, "page": page + 1},
                )
            )
        return WisdomView(
            f"{skill.get('slug') or skill_id} versions",
            items=[
                WisdomItem(
                    f"Version {item.get('version')}",
                    str(
                        item.get("created_at")
                        or item.get("published_at")
                        or "Immutable published version"
                    ),
                    actions=[
                        WisdomAction(
                            "Install",
                            "install_modes",
                            {"reference": f"{skill_id}@v{item.get('version')}"},
                        )
                    ],
                )
                for item in versions
            ],
            actions=actions,
            notice=None if versions else "No published versions.",
        )

    def _candidates(self, service: WisdomService, args: list[str]) -> WisdomView:
        show_all = bool(args and args[0].lower() == "all")
        query = " ".join(args[1:] if show_all else args).strip()
        candidates = service.list_candidates(qualified_only=not show_all, query=query)[
            :PAGE_SIZE
        ]
        return WisdomView(
            "Local skills you can share" if show_all else "Suggested contributions",
            "Nothing leaves this device until you create a private draft.",
            items=[
                WisdomItem(
                    str(item["name"]),
                    str(item.get("qualification") or "manual selection").replace(
                        "_", " "
                    ),
                    actions=[
                        WisdomAction(
                            "Create private draft",
                            "submit",
                            {"skill_name": item["name"]},
                            primary=True,
                        )
                    ],
                )
                for item in candidates
            ],
            actions=[]
            if show_all
            else [
                WisdomAction("View all local skills", "candidates", {"query": "all"})
            ],
            notice=None if candidates else "No local skills currently match.",
        )

    def _submit(self, service: WisdomService, args: list[str]) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom submit <local-skill>")
        # The first tap is explicit preparation; no network mutation occurs while parsing.
        candidates = service.list_candidates(qualified_only=False, query=args[0])
        exact = next((item for item in candidates if item.get("name") == args[0]), None)
        if exact is None:
            raise WisdomNotFound("local skill not found")
        return WisdomView(
            "Create owner-private draft?",
            f"{args[0]} will be prepared and uploaded for your review. Nothing is published yet.",
            actions=[
                WisdomAction(
                    "Create private draft",
                    "submit",
                    {"skill_name": args[0]},
                    primary=True,
                )
            ],
        )

    def _drafts(self, service: WisdomService, _args: list[str]) -> WisdomView:
        drafts = service.list_owner_drafts()[:PAGE_SIZE]
        return WisdomView(
            "Your contribution drafts",
            items=[self._draft_item(service, item) for item in drafts],
            notice=None if drafts else "No owner-private drafts.",
        )

    def _draft_item(self, service: WisdomService, draft: dict[str, Any]) -> WisdomItem:
        draft_id = str(draft["id"])
        state = str(draft.get("state") or "unknown")
        actions = [WisdomAction("Review", "review", {"draft_id": draft_id})]
        if state in {"ready", "owner_approved", "publishing"}:
            actions.append(
                WisdomAction(
                    "Approve & publish", "publish", {"draft_id": draft_id}, primary=True
                )
            )
        if state in {
            "ready",
            "owner_approved",
            "publishing",
            "pending_moderation",
            "changes_requested",
            "invalidated",
        }:
            actions.append(WisdomAction("Decline", "decline", {"draft_id": draft_id}))
        return WisdomItem(
            str(draft.get("slug") or draft_id), state.replace("_", " "), actions
        )

    def _review(self, service: WisdomService, args: list[str]) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom review <draft>")
        review = service.review(args[0], acknowledge=False)
        draft = review["draft"]
        return self._draft_view(
            draft,
            portal_url=service.portal_review_url(args[0]),
            hashes=review.get("hashes"),
            effective_policy=review.get("effective_policy"),
        )

    def _draft_view(
        self,
        draft: dict[str, Any],
        *,
        portal_url: str,
        hashes: dict[str, Any] | None = None,
        effective_policy: dict[str, Any] | None = None,
    ) -> WisdomView:
        draft_id = str(draft["id"])
        state = str(draft.get("state") or "ready")
        detail = [
            str(draft.get("authorDescription") or "No description"),
            f"State: {state.replace('_', ' ')}",
        ]
        if hashes:
            detail.extend([
                f"Content: {hashes.get('content')}",
                f"Description: {hashes.get('author_description')}",
                f"Manifest: {hashes.get('package_manifest')}",
            ])
        scan = draft.get("scan") or {}
        verdict = draft.get("scanVerdict") or scan.get("verdict")
        findings = scan.get("findings") or []
        if verdict:
            detail.append(f"Server scan: {verdict}")
        if findings:
            detail.append(f"Findings: {len(findings)} — inspect in full review")
        if effective_policy:
            mode = (
                effective_policy.get("publication_mode")
                or effective_policy.get("publicationMode")
                or effective_policy.get("mode")
            )
            if mode:
                detail.append(f"Publication policy: {str(mode).replace('_', ' ')}")
        moderation_note = draft.get("moderationNote")
        if moderation_note:
            detail.append(f"Requested changes: {moderation_note}")
        actions = [WisdomAction("Full review ↗", url=portal_url)]
        if state in {"ready", "owner_approved", "publishing"}:
            actions.extend([
                WisdomAction(
                    "Approve & publish", "publish", {"draft_id": draft_id}, primary=True
                ),
            ])
        if state in {
            "ready",
            "owner_approved",
            "publishing",
            "pending_moderation",
            "changes_requested",
            "invalidated",
        }:
            actions.append(WisdomAction("Decline", "decline", {"draft_id": draft_id}))
        return WisdomView(
            str(draft.get("slug") or "Private draft"),
            "\n".join(detail),
            actions=actions,
        )

    def _install(self, _service: WisdomService, args: list[str]) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom install <id|Portal URL|id@vN>")
        return self._install_modes(args[0])

    def _install_modes(self, reference: str) -> WisdomView:
        return WisdomView(
            "Choose how future updates are handled",
            "Your organization policy determines which choices are accepted by Gateway.",
            actions=[
                WisdomAction(
                    "Organization default",
                    "install_plan",
                    {"reference": reference, "update_mode": None},
                    primary=True,
                ),
                WisdomAction(
                    "Manual",
                    "install_plan",
                    {"reference": reference, "update_mode": "MANUAL"},
                ),
                WisdomAction(
                    "Automatic with notice",
                    "install_plan",
                    {"reference": reference, "update_mode": "AUTO_WITH_NOTICE"},
                ),
                WisdomAction(
                    "Required",
                    "install_plan",
                    {"reference": reference, "update_mode": "REQUIRED"},
                ),
            ],
        )

    def _plan_view(self, plan: dict[str, Any], *, kind: str) -> WisdomView:
        compatibility = plan.get("compatibility") or {}
        outcome = str(compatibility.get("outcome") or "unknown")
        blocked = (
            outcome != "compatible"
            or plan.get("allowed") is False
            or bool(plan.get("modified"))
            or bool(plan.get("sensitive_expansion"))
        )
        actions: list[WisdomAction] = []
        if not blocked:
            actions.append(
                WisdomAction(
                    "Confirm install" if kind == "install" else "Confirm update",
                    f"{kind}_apply",
                    {"receipt": plan["receipt"]},
                    primary=True,
                )
            )
        return WisdomView(
            f"Confirm {kind}",
            f"{plan.get('slug') or plan.get('skill_id')} · v{plan.get('version')}\nCompatibility: {outcome}",
            actions=actions,
            notice="Open Collective in Hermes for the full compatibility review."
            if blocked
            else "No files change until you confirm.",
        )

    def _installed(self, service: WisdomService, _args: list[str]) -> WisdomView:
        items = [
            item
            for item in service.list_installations()
            if item.get("state") == "active"
        ][:PAGE_SIZE]
        return WisdomView(
            "Managed installations",
            items=[
                WisdomItem(
                    str(item.get("slug") or item["skill_id"]),
                    self._installation_summary(item),
                    actions=[
                        WisdomAction(
                            "Check update",
                            "update_plan",
                            {"skill_id": item["skill_id"]},
                        ),
                        WisdomAction(
                            "Uninstall",
                            "uninstall_confirm",
                            {"skill_id": item["skill_id"]},
                            destructive=True,
                        ),
                    ],
                )
                for item in items
            ],
            notice=None if items else "No managed skills are installed.",
        )

    def _check(self, service: WisdomService, _args: list[str]) -> WisdomView:
        return self._check_view(service.check(apply_automatic=True))

    def _check_view(self, result: dict[str, Any]) -> WisdomView:
        rows = result.get("installations") or []
        return WisdomView(
            "Collective Wisdom updates",
            items=[
                WisdomItem(
                    str(item.get("slug") or item.get("skill_id")),
                    str(item.get("state") or "checked").replace("_", " "),
                    actions=[
                        WisdomAction(
                            "Review update",
                            "update_plan",
                            {"skill_id": item["skill_id"]},
                        )
                    ]
                    if item.get("state") == "update_available"
                    else [],
                )
                for item in rows[:PAGE_SIZE]
            ],
            notice=None if rows else "No managed installations to check.",
        )

    def _update(self, service: WisdomService, args: list[str]) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom update <skill|all>")
        if args[0].lower() == "all":
            return self._check_view(service.update_all(apply=False))
        matches = [
            item
            for item in service.list_installations()
            if args[0] in {item.get("skill_id"), item.get("slug")}
        ]
        if len(matches) != 1:
            raise WisdomNotFound("managed skill not found")
        plan = service.update_plan(str(matches[0]["skill_id"]))
        return (
            self._plan_view(plan, kind="update")
            if plan.get("receipt")
            else WisdomView("Skill is current")
        )

    def _uninstall(self, service: WisdomService, args: list[str]) -> WisdomView:
        if not args:
            raise ValueError("Usage: /wisdom uninstall <skill>")
        matches = [
            item
            for item in service.list_installations()
            if args[0] in {item.get("skill_id"), item.get("slug")}
        ]
        if len(matches) != 1:
            raise WisdomNotFound("managed skill not found")
        return WisdomView(
            "Remove managed skill?",
            f"{matches[0].get('slug')} will move to recoverable Wisdom trash.",
            actions=[
                WisdomAction(
                    "Uninstall",
                    "uninstall_apply",
                    {"skill_id": matches[0]["skill_id"]},
                    destructive=True,
                )
            ],
        )

    def _notifications(self, service: WisdomService, _args: list[str]) -> WisdomView:
        data = service.notifications(mark_seen=False)
        events = data.get("events") or []
        return WisdomView(
            "Collective Wisdom notifications",
            items=[
                WisdomItem(
                    str(item.get("message") or item.get("kind") or "Update"),
                    str(item.get("occurred_at") or ""),
                )
                for item in events[:PAGE_SIZE]
            ],
            actions=[WisdomAction("Mark all read", "mark_notifications")]
            if events
            else [],
            notice=None if events else "You are all caught up.",
        )

    @staticmethod
    def _publication_state_text(state: str) -> str:
        if state == "pending_moderation":
            return "Sent to your collective administrator for approval."
        if state == "published":
            return "Published to your collective."
        return state.replace("_", " ")

    @staticmethod
    def _portal_skill_url(service: WisdomService, skill_id: str) -> str:
        org_id = service.store.active_org_id() or ""
        org_slug = org_id.split(":", 1)[-1]
        from urllib.parse import quote

        return f"{portal_base_url()}/orgs/{quote(org_slug, safe='')}/wisdom/skills/{quote(skill_id, safe='')}"

    @staticmethod
    def _requirements_summary(specification: dict[str, Any]) -> str:
        requirements = specification.get("requirements")
        requirements = requirements if isinstance(requirements, dict) else specification
        hermes = requirements.get("hermes") or {}
        runtime = requirements.get("runtime") or {}
        parts: list[str] = []
        minimum = hermes.get("minimum_version")
        if minimum:
            parts.append(f"Hermes {minimum}+")
        for field, label in (("platforms", "OS"), ("architectures", "CPU")):
            values = requirements.get(field) or []
            if values:
                parts.append(f"{label}: {', '.join(map(str, values))}")
        enabled_runtime = [name for name, enabled in runtime.items() if enabled is True]
        if enabled_runtime:
            parts.append("runtime: " + ", ".join(enabled_runtime))
        tools = requirements.get("tools") or []
        plugins = requirements.get("plugins") or []
        if tools:
            parts.append(f"{len(tools)} tool requirement(s)")
        if plugins:
            parts.append(f"{len(plugins)} plugin requirement(s)")
        return "; ".join(parts) or "No special requirements declared"

    @staticmethod
    def _installation_summary(item: dict[str, Any]) -> str:
        installed = item.get("version")
        latest = item.get("latest_version")
        mode = item.get("effective_update_mode") or item.get("update_mode")
        state = item.get("skill_state")
        version_text = f"installed v{installed}"
        if isinstance(latest, int):
            version_text += f" · latest v{latest}"
        if state and state != "active":
            version_text += f" · {state}"
        return f"{version_text} · {mode}"

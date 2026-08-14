"""Deterministic Slack thread deletion orchestration.

Deployment assumptions (authoritative — do not defend against their violation
in code): a single gateway process (adapters, ResponseStore, and SessionDB all
run in-process), a single human user in a two-member Slack workspace, and
uuid4-named media cache files, so a cache path is never shared between threads
or sessions.

Accepted property: `!delete` does not scrub local cached attachment copies;
they age out via the standard hourly cache GC within 24 hours.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional


_NOT_FOUND_INVENTORY_ERRORS = {"message_not_found", "thread_not_found"}
_NOT_FOUND_FILE_ERRORS = {"file_deleted", "file_not_found"}
_NOT_FOUND_MESSAGE_ERRORS = {"message_not_found"}
_TRANSIENT_ERRORS = {"ratelimited", "rate_limited", "internal_error", "fatal_error", "service_unavailable"}
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SLACK_TS_RE = re.compile(r"^[0-9]{1,20}\.[0-9]{1,20}$")
_SLACK_ID_RE = re.compile(r"^[A-Z0-9_]{2,40}$")
_REPORT_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


@dataclass
class SlackThreadDeleteResult:
    success: bool
    failures: list[str] = field(default_factory=list)


@dataclass
class SlackThreadInventory:
    root_ts: str = ""
    reply_ts: list[str] = field(default_factory=list)
    file_ids: list[str] = field(default_factory=list)


class SlackThreadDeleteService:
    """Delete one Slack thread without persisting its contents or a work plan."""

    def __init__(
        self,
        client,
        *,
        report_failure: Optional[Callable[[str], object]] = None,
        max_attempts: int = 3,
        max_inventory_pages: int = 100,
        max_inventory_identifiers: int = 20_000,
        max_retry_after_seconds: float = 30.0,
    ) -> None:
        self.client = client
        self.report_failure = report_failure
        self.max_attempts = max(1, max_attempts)
        self.max_inventory_pages = max(1, max_inventory_pages)
        self.max_inventory_identifiers = max(1, max_inventory_identifiers)
        self.max_retry_after_seconds = max(0.0, max_retry_after_seconds)
        self.report_delivery_failed = False

    async def _report(self, text: str) -> None:
        if self.report_failure is None:
            return
        try:
            result = self.report_failure(text)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                if hasattr(result, "get"):
                    if self._error(result):
                        self.report_delivery_failed = True
                elif getattr(result, "success", None) is not True:
                    self.report_delivery_failed = True
        except Exception:
            # Never surface a second response into the thread being scrubbed.
            self.report_delivery_failed = True
            return

    @staticmethod
    def _safe_code(value: object) -> str:
        code = str(value or "unknown_error")
        return code if _SAFE_CODE_RE.fullmatch(code) else "invalid_error_code"

    @staticmethod
    def _error(response) -> str:
        if response is None:
            return "empty_response"
        if hasattr(response, "get"):
            ok = response.get("ok")
            if ok is True:
                return ""
            if ok is False:
                return SlackThreadDeleteService._safe_code(response.get("error"))
            return "invalid_response"
        return "invalid_response"

    @staticmethod
    def _exception_error(exc: Exception) -> tuple[str, float]:
        response = getattr(exc, "response", None)
        error = ""
        retry_after = 0.0
        if response is not None:
            try:
                error = str(response.get("error") or "")
            except Exception:
                pass
            try:
                retry_after = float(getattr(response, "headers", {}).get("Retry-After") or 0)
            except (TypeError, ValueError, AttributeError):
                pass
        return SlackThreadDeleteService._safe_code(error or type(exc).__name__), retry_after

    async def _call(self, operation, *, not_found: set[str]) -> str:
        for attempt in range(self.max_attempts):
            retry_after = 0.0
            try:
                error = self._error(await operation())
            except Exception as exc:
                error, retry_after = self._exception_error(exc)
            if not error or error in not_found:
                return ""
            if error not in _TRANSIENT_ERRORS or attempt + 1 >= self.max_attempts:
                return error
            delay = retry_after if retry_after > 0 else 2**attempt
            await asyncio.sleep(min(delay, self.max_retry_after_seconds))
        return "unknown_error"

    async def _inventory(
        self, channel_id: str, thread_ts: str
    ) -> tuple[SlackThreadInventory, str]:
        inventory = SlackThreadInventory()
        cursor = ""
        seen_cursors: set[str] = set()
        for _page in range(self.max_inventory_pages):
            response = None
            error = "unknown_error"
            for attempt in range(self.max_attempts):
                retry_after = 0.0
                try:
                    response = await self.client.conversations_replies(
                        channel=channel_id,
                        ts=thread_ts,
                        limit=100,
                        cursor=cursor or None,
                    )
                    error = self._error(response)
                except Exception as exc:
                    error, retry_after = self._exception_error(exc)
                if not error or error in _NOT_FOUND_INVENTORY_ERRORS:
                    break
                if error not in _TRANSIENT_ERRORS or attempt + 1 >= self.max_attempts:
                    return SlackThreadInventory(), error
                delay = retry_after if retry_after > 0 else 2**attempt
                await asyncio.sleep(min(delay, self.max_retry_after_seconds))
            if error:
                return SlackThreadInventory(), error
            assert response is not None
            messages = response.get("messages")
            if not isinstance(messages, list):
                return SlackThreadInventory(), "invalid_response"
            metadata = response.get("response_metadata", {})
            if not isinstance(metadata, Mapping):
                return SlackThreadInventory(), "invalid_response"
            next_cursor = metadata.get("next_cursor", "")
            if not isinstance(next_cursor, str):
                return SlackThreadInventory(), "invalid_response"
            for message in messages:
                if not isinstance(message, Mapping):
                    return SlackThreadInventory(), "invalid_response"
                message_ts = str(message.get("ts") or "")
                if not _SLACK_TS_RE.fullmatch(message_ts):
                    return SlackThreadInventory(), "invalid_response"
                if not inventory.root_ts:
                    inventory.root_ts = message_ts
                elif message_ts == inventory.root_ts:
                    # conversations.replies can repeat the parent message on
                    # later pages; a repeat is not an error, and failing here
                    # would permanently brick deletion of threads longer than
                    # one page (inventory runs first on every retry).
                    continue
                else:
                    inventory.reply_ts.append(message_ts)
                files = message.get("files", [])
                if not isinstance(files, list):
                    return SlackThreadInventory(), "invalid_response"
                for file_obj in files:
                    if not isinstance(file_obj, Mapping):
                        return SlackThreadInventory(), "invalid_response"
                    file_id = str(file_obj.get("id") or "")
                    if not _SLACK_ID_RE.fullmatch(file_id):
                        return SlackThreadInventory(), "invalid_response"
                    inventory.file_ids.append(file_id)
                if (
                    bool(inventory.root_ts)
                    + len(inventory.reply_ts)
                    + len(inventory.file_ids)
                    > self.max_inventory_identifiers
                ):
                    return SlackThreadInventory(), "identifier_limit"
            cursor = next_cursor
            if not cursor:
                inventory.reply_ts = list(dict.fromkeys(inventory.reply_ts))
                inventory.file_ids = list(dict.fromkeys(inventory.file_ids))
                return inventory, ""
            if cursor in seen_cursors:
                return SlackThreadInventory(), "cursor_cycle"
            seen_cursors.add(cursor)
        return SlackThreadInventory(), "page_limit"

    @staticmethod
    def _format_report(
        *, workspace_id: str, channel_id: str, thread_ts: str, failures: Iterable[str]
    ) -> str:
        def safe(value: object) -> str:
            text = str(value or "")
            return text if _REPORT_VALUE_RE.fullmatch(text) else "invalid_identifier"

        groups: dict[str, list[str]] = {}
        for item in failures:
            kind, _, detail = item.partition(":")
            groups.setdefault(kind, []).append(safe(detail))
        parts = [
            f"workspace={safe(workspace_id)}",
            f"channel={safe(channel_id)}",
            f"thread={safe(thread_ts)}",
        ]
        for kind in ("auth", "inventory", "files", "messages", "local", "trigger", "root"):
            values = groups.get(kind)
            if values:
                parts.append(f"{kind}={','.join(values)}")
        report = " ".join(parts)
        if len(report.encode("utf-8")) <= 3500:
            return report
        # Preserve every failure category deterministically while remaining
        # below Slack's message limit. One identifier is retained per category;
        # the exact omitted count keeps the aggregate useful for retries.
        bounded = parts[:3]
        for kind in ("auth", "inventory", "files", "messages", "local", "trigger", "root"):
            values = groups.get(kind)
            if not values:
                continue
            value = values[0]
            if len(values) > 1:
                value = f"{value},omitted_{len(values) - 1}"
            bounded.append(f"{kind}={value}")
        bounded.append("report=truncated")
        return " ".join(bounded)

    async def execute(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        trigger_ts: str,
        invoker_user_id: str,
        workspace_id: str,
        quiesce: Optional[Callable[[], Awaitable[None]]] = None,
        local_scrub: Callable[[], Awaitable[list[str]]],
    ) -> SlackThreadDeleteResult:
        failures: list[str] = []
        auth = {}
        auth_error = "unknown_error"
        for attempt in range(self.max_attempts):
            retry_after = 0.0
            try:
                auth = await self.client.auth_test()
                auth_error = self._error(auth)
            except Exception as exc:
                auth_error, retry_after = self._exception_error(exc)
                auth = {}
            if not auth_error:
                break
            if auth_error not in _TRANSIENT_ERRORS or attempt + 1 >= self.max_attempts:
                break
            delay = retry_after if retry_after > 0 else 2**attempt
            await asyncio.sleep(min(delay, self.max_retry_after_seconds))
        if auth_error:
            failures.append(f"auth:{auth_error}")
        elif str(auth.get("user_id") or "") != str(invoker_user_id):
            failures.append("auth:owner_mismatch")
        elif str(auth.get("team_id") or "") != str(workspace_id):
            failures.append("auth:workspace_mismatch")
        if failures:
            await self._report(self._format_report(
                workspace_id=workspace_id, channel_id=channel_id,
                thread_ts=thread_ts, failures=failures,
            ))
            return SlackThreadDeleteResult(False, failures)

        if quiesce is not None:
            try:
                await quiesce()
            except Exception as exc:
                failures.append(f"local:quiesce:{type(exc).__name__}")
        if failures:
            await self._report(self._format_report(
                workspace_id=workspace_id, channel_id=channel_id,
                thread_ts=thread_ts, failures=failures,
            ))
            return SlackThreadDeleteResult(False, failures)

        inventory, inventory_error = await self._inventory(channel_id, thread_ts)
        slack_absent = inventory_error in _NOT_FOUND_INVENTORY_ERRORS
        if inventory_error and not slack_absent:
            failures.append(f"inventory:{inventory_error}")
        if not inventory.root_ts and not inventory_error:
            slack_absent = True
        elif inventory.root_ts and inventory.root_ts != str(thread_ts):
            failures.append("inventory:thread_mismatch")
        if failures:
            await self._report(self._format_report(
                workspace_id=workspace_id, channel_id=channel_id,
                thread_ts=thread_ts, failures=failures,
            ))
            return SlackThreadDeleteResult(False, failures)

        for file_id in inventory.file_ids:
            error = await self._call(
                lambda file_id=file_id: self.client.files_delete(file=file_id),
                not_found=_NOT_FOUND_FILE_ERRORS,
            )
            if error:
                failures.append(f"files:{file_id}:{error}")

        ordinary_replies = [
            message_ts for message_ts in inventory.reply_ts
            if message_ts != str(trigger_ts)
        ]
        for message_ts in ordinary_replies:
            error = await self._call(
                lambda message_ts=message_ts: self.client.chat_delete(
                    channel=channel_id, ts=message_ts
                ),
                not_found=_NOT_FOUND_MESSAGE_ERRORS,
            )
            if error:
                failures.append(f"messages:{message_ts}:{error}")

        try:
            local_failures = await local_scrub()
        except Exception as exc:
            local_failures = [f"session:{type(exc).__name__}"]
        failures.extend(f"local:{item}" for item in (local_failures or []))

        if not failures:
            trigger_error = await self._call(
                lambda: self.client.chat_delete(channel=channel_id, ts=trigger_ts),
                not_found=_NOT_FOUND_MESSAGE_ERRORS,
            )
            if trigger_error:
                failures.append(f"trigger:{trigger_ts}:{trigger_error}")

        if not failures:
            root_error = await self._call(
                lambda: self.client.chat_delete(channel=channel_id, ts=thread_ts),
                not_found=_NOT_FOUND_MESSAGE_ERRORS,
            )
            if root_error:
                failures.append(f"root:{thread_ts}:{root_error}")

        if failures:
            await self._report(self._format_report(
                workspace_id=workspace_id, channel_id=channel_id,
                thread_ts=thread_ts, failures=failures,
            ))
        return SlackThreadDeleteResult(not failures, failures)


class SlackSdkOwnerClient:
    """Small adapter around Slack's AsyncWebClient for the owner credential."""

    def __init__(self, token: str) -> None:
        from slack_sdk.web.async_client import AsyncWebClient

        self._client = AsyncWebClient(token=token)

    async def auth_test(self):
        return await self._client.auth_test()

    async def conversations_replies(self, **kwargs):
        return await self._client.conversations_replies(**kwargs)

    async def files_delete(self, **kwargs):
        return await self._client.files_delete(**kwargs)

    async def chat_delete(self, **kwargs):
        return await self._client.chat_delete(**kwargs)

    async def chat_postMessage(self, **kwargs):
        return await self._client.chat_postMessage(**kwargs)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

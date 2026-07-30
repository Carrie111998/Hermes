"""Owner-token-authenticated loopback client for Grizzly action receipts."""

from __future__ import annotations

import html
import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_BASE_URL = "http://127.0.0.1:8791"
_MAX_RESPONSE_BYTES = 256_000
_CALLBACK_RE = re.compile(r"^od:[A-Za-z0-9_-]{16,60}$")
_CARD_REF_RE = re.compile(r"^TGC-[0-9a-f]{16}$")
_RECEIPT_RE = re.compile(r"^ACT-[0-9a-f]{12}$")
_TELEGRAM_ID_RE = re.compile(r"^-?[0-9]{1,24}$")
_MESSAGE_ID_RE = re.compile(r"^[0-9]{1,24}$")
_ACTION_LABELS = {
    "approve": "Approved",
    "reject": "Rejected",
    "request_changes": "Changes requested",
}


class ActionServiceError(RuntimeError):
    """A content-minimal bridge failure safe to log without response bodies."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _read_owner_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    path_info_before = None
    try:
        # Windows lacks O_NOFOLLOW. Bind the fallback checks to the opened
        # descriptor and compare file identity before/after the open so a
        # reparse-point or path replacement cannot silently redirect the read.
        if os.name == "nt":
            path_info_before = os.lstat(path)
            if stat.S_ISLNK(path_info_before.st_mode):
                raise ActionServiceError(
                    "action bridge token path must not be a symlink"
                )
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ActionServiceError("action bridge token is unavailable") from exc
    try:
        opened_info = os.fstat(descriptor)
        if not stat.S_ISREG(opened_info.st_mode):
            raise ActionServiceError("action bridge token is not a regular file")
        if os.name != "nt":
            expected_uid = getattr(os, "getuid", lambda: 0)()
            if opened_info.st_uid != expected_uid:
                raise ActionServiceError("action bridge token owner mismatch")
            if stat.S_IMODE(opened_info.st_mode) & 0o077:
                raise ActionServiceError("action bridge token is not owner-only")
        if os.name == "nt":
            path_info_after = os.lstat(path)
            opened_identity = (
                int(opened_info.st_dev),
                int(opened_info.st_ino),
            )
            if (
                path_info_before is None
                or stat.S_ISLNK(path_info_after.st_mode)
                or (int(path_info_before.st_dev), int(path_info_before.st_ino))
                != opened_identity
                or (int(path_info_after.st_dev), int(path_info_after.st_ino))
                != opened_identity
            ):
                raise ActionServiceError(
                    "action bridge token changed while it was opened"
                )
        raw_token = os.read(descriptor, 257)
        if len(raw_token) != int(opened_info.st_size):
            raise ActionServiceError("action bridge token changed while it was read")
        token = raw_token.decode("utf-8").strip()
    except UnicodeError as exc:
        raise ActionServiceError("action bridge token is unreadable") from exc
    finally:
        os.close(descriptor)
    if not 43 <= len(token) <= 256 or any(character.isspace() for character in token):
        raise ActionServiceError("action bridge token is malformed")
    return token


def _decode(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ActionServiceError("action service response is too large")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeError, ValueError) as exc:
        raise ActionServiceError("action service response is invalid") from exc
    if not isinstance(payload, dict):
        raise ActionServiceError("action service response is not an object")
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ActionServiceClient:
    """Narrow client for callback resolution and receipt mirror acknowledgement."""

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        timeout: float = 5.0,
        opener: Any | None = None,
    ) -> None:
        self.token_path = token_path or (
            get_hermes_home() / "state" / "action-service" / "bridge-token"
        )
        self.timeout = max(0.5, min(float(timeout), 20.0))
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _read_owner_token(self) -> str:
        """Read the bridge token from one validated file descriptor."""
        return _read_owner_token(self.token_path)

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method not in {"GET", "POST"} or path not in {
            "/api/v1/telegram/resolve",
            "/api/v1/telegram/pending",
            "/api/v1/telegram/mirrored",
        }:
            raise ActionServiceError("action bridge request is outside its allowlist")
        encoded = None
        headers = {
            "Accept": "application/json",
            "Cache-Control": "no-store",
            "X-Action-Bridge-Token": _read_owner_token(self.token_path),
        }
        if body is not None:
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            _BASE_URL + path,
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            if response.geturl() != request.full_url:
                raise ActionServiceError(
                    "action service responded from an unexpected endpoint"
                )
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Preserve only status; response bodies may contain internal detail.
            exc.read(_MAX_RESPONSE_BYTES + 1)
            raise ActionServiceError(
                f"action service rejected the request (HTTP {int(exc.code)})"
            ) from None
        except (OSError, urllib.error.URLError) as exc:
            raise ActionServiceError("action service is unavailable") from exc
        return _decode(raw)

    def resolve_callback(
        self,
        callback: str,
        chat_id: str,
        message_id: str,
        telegram_user_id: str,
        actor_label: str,
    ) -> dict[str, Any]:
        if (
            not _CALLBACK_RE.fullmatch(callback)
            or not _TELEGRAM_ID_RE.fullmatch(chat_id)
            or not _MESSAGE_ID_RE.fullmatch(message_id)
            or not _TELEGRAM_ID_RE.fullmatch(telegram_user_id)
        ):
            raise ActionServiceError("Telegram callback context is malformed")
        payload = self._request(
            "POST",
            "/api/v1/telegram/resolve",
            {
                "callback": callback,
                "chat_id": chat_id,
                "message_id": message_id,
                "telegram_user_id": telegram_user_id,
                "actor_label": " ".join(actor_label.split())[:60],
            },
        )
        if (
            payload.get("mode") != "shadow"
            or not _CARD_REF_RE.fullmatch(str(payload.get("card_ref") or ""))
            or not _RECEIPT_RE.fullmatch(str(payload.get("receipt_id") or ""))
            or payload.get("action") not in _ACTION_LABELS
        ):
            raise ActionServiceError("action service receipt is invalid")
        return payload

    def pending(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/telegram/pending")
        items = payload.get("items")
        if (
            payload.get("mode") != "shadow"
            or not isinstance(items, list)
            or len(items) > 100
        ):
            raise ActionServiceError("pending receipt response is invalid")
        for item in items:
            _validate_pending(item)
        return items

    def pending_card(
        self,
        card_ref: str,
        *,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
        receipt_id: str,
        action: str,
    ) -> dict[str, Any] | None:
        if not _CARD_REF_RE.fullmatch(card_ref):
            raise ActionServiceError("card reference is malformed")
        item = next(
            (row for row in self.pending() if row["card_ref"] == card_ref), None
        )
        if item is None:
            return None
        binding = item["binding"]
        resolution = item["resolution"]
        binding_thread_id = binding["thread_id"]
        thread_matches = (
            binding_thread_id is None
            and thread_id is None
            or binding_thread_id is not None
            and thread_id is not None
            and str(binding_thread_id) == str(thread_id)
        )
        if (
            str(binding["chat_id"]) != str(chat_id)
            or not thread_matches
            or str(binding["message_id"]) != str(message_id)
            or str(resolution["receipt_id"]) != str(receipt_id)
            or resolution["action"] != action
        ):
            raise ActionServiceError(
                "pending receipt does not match its callback route"
            )
        return item

    def mirrored(self, card_ref: str, receipt_id: str) -> dict[str, Any]:
        if not _CARD_REF_RE.fullmatch(card_ref) or not _RECEIPT_RE.fullmatch(
            receipt_id
        ):
            raise ActionServiceError("receipt acknowledgement is malformed")
        payload = self._request(
            "POST",
            "/api/v1/telegram/mirrored",
            {"card_ref": card_ref, "receipt_id": receipt_id},
        )
        if payload.get("mirrored") is not True or payload.get("card_ref") != card_ref:
            raise ActionServiceError("receipt acknowledgement was not confirmed")
        return payload


def _validate_pending(item: Any) -> None:
    binding = item.get("binding") if isinstance(item, dict) else None
    resolution = item.get("resolution") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or not _CARD_REF_RE.fullmatch(str(item.get("card_ref") or ""))
        or not isinstance(binding, dict)
        or not _TELEGRAM_ID_RE.fullmatch(str(binding.get("chat_id") or ""))
        or "thread_id" not in binding
        or (
            binding["thread_id"] is not None
            and not _MESSAGE_ID_RE.fullmatch(str(binding["thread_id"]))
        )
        or not _MESSAGE_ID_RE.fullmatch(str(binding.get("message_id") or ""))
        or not isinstance(binding.get("card_html"), str)
        or not binding["card_html"].strip()
        or len(binding["card_html"]) > 8_000
        or not isinstance(resolution, dict)
        or resolution.get("mode") != "shadow"
        or resolution.get("action") not in _ACTION_LABELS
        or not _RECEIPT_RE.fullmatch(str(resolution.get("receipt_id") or ""))
    ):
        raise ActionServiceError("pending receipt item is invalid")


def render_shadow_card(item: dict[str, Any]) -> str:
    """Render one compact escaped SHADOW receipt into the original card."""

    _validate_pending(item)
    binding = item["binding"]
    resolution = item["resolution"]
    actor = " ".join(str(resolution.get("actor_label") or "Principal").split())[:60]
    rendered = (
        binding["card_html"].rstrip()
        + "\n\n<b>SHADOW RECEIPT</b>\n"
        + html.escape(actor)
        + " selected <b>"
        + html.escape(_ACTION_LABELS[resolution["action"]])
        + "</b>.\nRecorded only — nothing was executed.\n<code>"
        + html.escape(resolution["receipt_id"])
        + "</code>"
    )
    if len(rendered) > 10_000:
        raise ActionServiceError("rendered receipt is too large")
    return rendered

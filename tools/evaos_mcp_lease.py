"""Managed evaOS authentication for Pipedream MCP transports.

The desktop runtime receives only a short-lived MCP lease.  The deployment
broker secret is read from a root-owned file only while minting that lease.
It is never persisted in the manager, included in errors, or exposed to MCP
tool/model surfaces.

The mint request carries ``{action, app_slug}`` plus, when the profile's
managed server entry configures them, the profile's own Pipedream identity —
``external_user_id`` and ``account_id`` straight from Pipedream's developer
docs (``x-pd-external-user-id`` / ``x-pd-account-id``).  Under multiplex every
profile owns its MCP entries, so this identity is per-profile by construction.
No grant handle, catalog, alias, registry or evaOS-invented identity key is
involved; without the configured identity the server rejects the mint (the
legacy grant-header path is server-side back-compat only).
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlparse

import httpx


_APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_ACCOUNT_ID_RE = re.compile(r"^apn_[A-Za-z0-9_-]+$")
_EXTERNAL_USER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
_CREDENTIAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LEASE_ENDPOINT_PATH = "/functions/v1/desktop-runtime-session"
_MCP_ORIGIN = ("https", "remote.mcp.pipedream.net")
_REQUIRED_LEASE_HEADERS = frozenset(
    {
        "Authorization",
        "x-pd-project-id",
        "x-pd-environment",
        "x-pd-external-user-id",
        "x-pd-app-slug",
        "x-pd-account-id",
    }
)


class EvaosLeaseError(RuntimeError):
    """Sanitized failure while resolving or refreshing managed MCP auth."""


@dataclass(frozen=True, repr=False)
class _LeaseSourceMaterial:
    endpoint: str
    broker_secret: str
    app_slug: str
    external_user_id: Optional[str] = None
    account_id: Optional[str] = None


@dataclass(frozen=True, repr=False)
class EvaosMcpLease:
    """Short-lived, memory-only MCP connection material."""

    mcp_url: str
    headers: Mapping[str, str]
    expires_at: datetime

    @property
    def authorization(self) -> str:
        return self.headers["Authorization"]

    def __repr__(self) -> str:
        return (
            "EvaosMcpLease("
            f"mcp_url={self.mcp_url!r}, expires_at={self.expires_at!r}, "
            "headers=<redacted>)"
        )


def _default_secret_reader(name: str) -> Optional[str]:
    from agent.secret_scope import get_secret

    return get_secret(name)


def _default_profile_resolver() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home().expanduser().resolve())


class EvaosLeaseSource:
    """Resolve the current profile's broker secret for a managed app."""

    def __init__(
        self,
        *,
        profile_key: str,
        app_slug: str,
        external_user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        secret_reader: Optional[Callable[[str], Optional[str]]] = None,
        profile_resolver: Optional[Callable[[], str]] = None,
        root_uid: int = 0,
        service_uid: Optional[int] = None,
    ):
        if not isinstance(profile_key, str) or not profile_key:
            raise EvaosLeaseError("managed MCP profile authority is missing")
        if not isinstance(app_slug, str) or not _APP_SLUG_RE.fullmatch(app_slug):
            raise EvaosLeaseError("managed MCP app slug is invalid")
        if (external_user_id is None) != (account_id is None):
            raise EvaosLeaseError(
                "managed MCP external_user_id and account_id must be configured together"
            )
        if external_user_id is not None:
            if not isinstance(external_user_id, str) or not _EXTERNAL_USER_ID_RE.fullmatch(
                external_user_id
            ):
                raise EvaosLeaseError("managed MCP external user id is invalid")
            if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
                raise EvaosLeaseError("managed MCP account id is invalid")
        self._profile_key = profile_key
        self._app_slug = app_slug
        self._external_user_id = external_user_id
        self._account_id = account_id
        self._secret_reader = secret_reader or _default_secret_reader
        self._profile_resolver = profile_resolver or _default_profile_resolver
        self._root_uid = root_uid
        get_effective_uid = getattr(os, "geteuid", None)
        self._service_uid = (
            (
                get_effective_uid()
                if callable(get_effective_uid)
                else root_uid
            )
            if service_uid is None
            else service_uid
        )

    def __repr__(self) -> str:
        return (
            "EvaosLeaseSource("
            f"profile_key={self._profile_key!r}, app_slug={self._app_slug!r})"
        )

    def _setting(self, name: str) -> str:
        try:
            value = self._secret_reader(name)
        except Exception as exc:
            raise EvaosLeaseError(
                "managed MCP profile-scoped configuration is unavailable"
            ) from exc
        if not isinstance(value, str) or not value.strip():
            raise EvaosLeaseError(
                "managed MCP profile-scoped configuration is incomplete"
            )
        return value.strip()

    def _secure_file_text(self, raw_path: str, *, label: str) -> str:
        if raw_path.startswith("%d/"):
            credential_name = raw_path[len("%d/"):]
            credentials_directory = os.environ.get(
                "CREDENTIALS_DIRECTORY", ""
            ).strip()
            systemd_dir = Path(credentials_directory)
            if (
                credential_name in {".", ".."}
                or not _CREDENTIAL_NAME_RE.fullmatch(credential_name)
                or not systemd_dir.is_absolute()
            ):
                raise EvaosLeaseError(
                    f"{label} has an invalid systemd credential pointer"
                )
            path = systemd_dir / credential_name
        else:
            path = Path(raw_path)
        if not path.is_absolute():
            raise EvaosLeaseError(f"{label} must use an absolute path")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise EvaosLeaseError(f"{label} is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise EvaosLeaseError(f"{label} must be a regular file")
            mode = stat.S_IMODE(metadata.st_mode)
            credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
            in_systemd_credentials = False
            if credentials_directory:
                systemd_dir = Path(credentials_directory)
                in_systemd_credentials = (
                    systemd_dir.is_absolute()
                    and path.parent == systemd_dir
                )

            # The Golden source files are root-owned 0600. LoadCredential=
            # copies them into the unit's private, read-only %d directory:
            # systemd creates each copy 0400, grants the service UID read via
            # ACL, and may fall back to chown(service_uid). The ACL mask can
            # surface as 0440 in st_mode even though no ordinary group is
            # being granted access. Accept only those documented copy forms
            # when the exact path is inside CREDENTIALS_DIRECTORY.
            direct_source = (
                metadata.st_uid == self._root_uid and mode == 0o600
            )
            systemd_copy = (
                in_systemd_credentials
                and metadata.st_uid in {self._root_uid, self._service_uid}
                and (
                    mode == 0o400
                    or (
                        metadata.st_uid == self._root_uid
                        and mode == 0o440
                    )
                )
            )
            if not direct_source and not systemd_copy:
                raise EvaosLeaseError(
                    f"{label} does not have secure managed credential "
                    "ownership and mode"
                )
            if metadata.st_size <= 0 or metadata.st_size > 64 * 1024:
                raise EvaosLeaseError(f"{label} has an invalid size")
            chunks = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining:
                raise EvaosLeaseError(f"{label} is unreadable")
            try:
                return b"".join(chunks).decode("utf-8")
            except UnicodeError as exc:
                raise EvaosLeaseError(f"{label} is unreadable") from exc
        except OSError as exc:
            raise EvaosLeaseError(f"{label} is unreadable") from exc
        finally:
            os.close(descriptor)

    def _endpoint(self) -> str:
        endpoint = self._setting("EVAOS_DESKTOP_RUNTIME_SESSION_URL")
        try:
            parsed = urlparse(endpoint)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError as exc:
            raise EvaosLeaseError("managed MCP lease endpoint is invalid") from exc
        if (
            parsed.scheme != "https"
            or not hostname.endswith(".supabase.co")
            or hostname == ".supabase.co"
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != _LEASE_ENDPOINT_PATH
        ):
            raise EvaosLeaseError("managed MCP lease endpoint is invalid")
        return endpoint

    def _broker_secret(self) -> str:
        raw = self._secure_file_text(
            self._setting("PIPEDREAM_AGENT_BROKER_SECRET_FILE"),
            label="managed MCP broker secret file",
        )
        value = raw.strip()
        if not value or "\n" in value or "\r" in value:
            raise EvaosLeaseError("managed MCP broker secret file is malformed")
        return value

    def read(self) -> _LeaseSourceMaterial:
        try:
            current_profile = self._profile_resolver()
        except Exception as exc:
            raise EvaosLeaseError("managed MCP profile authority is unavailable") from exc
        if current_profile != self._profile_key:
            raise EvaosLeaseError("managed MCP profile authority changed")
        return _LeaseSourceMaterial(
            endpoint=self._endpoint(),
            broker_secret=self._broker_secret(),
            app_slug=self._app_slug,
            external_user_id=self._external_user_id,
            account_id=self._account_id,
        )


LeaseTransport = Callable[
    [str, Mapping[str, str], Mapping[str, str]], Awaitable[Any]
]


async def _default_transport(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, str],
):
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(15.0),
    ) as client:
        return await client.post(url, headers=headers, json=payload)


class EvaosLeaseManager:
    """Single-flight, in-memory MCP lease cache."""

    def __init__(
        self,
        *,
        source: EvaosLeaseSource,
        transport: Optional[LeaseTransport] = None,
        now: Optional[Callable[[], datetime]] = None,
        refresh_skew_seconds: float = 60,
        on_mint_failure: Optional[Callable[[Exception], None]] = None,
    ):
        if refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be non-negative")
        self._source = source
        self._transport = transport or _default_transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self._on_mint_failure = on_mint_failure
        self._lock = asyncio.Lock()
        self._lease: Optional[EvaosMcpLease] = None

    def __repr__(self) -> str:
        return "EvaosLeaseManager(lease=<redacted>)"

    def _usable(self, lease: Optional[EvaosMcpLease]) -> bool:
        return bool(
            lease is not None
            and lease.expires_at > self._now() + self._refresh_skew
        )

    async def get_lease(
        self,
        *,
        force_refresh: bool = False,
        rejected_authorization: Optional[str] = None,
    ) -> EvaosMcpLease:
        lease = self._lease
        if not force_refresh and self._usable(lease):
            return lease
        async with self._lock:
            lease = self._lease
            if force_refresh:
                if (
                    rejected_authorization is not None
                    and lease is not None
                    and lease.authorization != rejected_authorization
                    and self._usable(lease)
                ):
                    return lease
            elif self._usable(lease):
                return lease
            try:
                material = self._source.read()
                minted = await self._mint(material)
            except Exception as exc:
                if self._on_mint_failure is not None:
                    self._on_mint_failure(exc)
                raise
            self._lease = minted
            return minted

    async def _mint(self, material: _LeaseSourceMaterial) -> EvaosMcpLease:
        request_headers = {
            "Content-Type": "application/json",
            "X-Evaos-Desktop-Broker-Secret": material.broker_secret,
        }
        request_body = {
            "action": "pipedream_mcp_lease",
            "app_slug": material.app_slug,
        }
        if material.external_user_id is not None:
            request_body["external_user_id"] = material.external_user_id
            request_body["account_id"] = material.account_id
        try:
            response = await self._transport(
                material.endpoint, request_headers, request_body
            )
        except Exception as exc:
            raise EvaosLeaseError("managed MCP lease service is unavailable") from exc
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            if status_code == 401:
                message = "managed MCP broker secret was rejected"
            elif status_code == 403:
                message = "managed MCP profile authority is no longer valid"
            elif status_code == 400:
                message = "managed MCP lease request was rejected"
            elif status_code in (429, 502, 503):
                message = "managed MCP lease service is temporarily unavailable"
            else:
                message = "managed MCP lease service returned an unexpected status"
            raise EvaosLeaseError(message)
        try:
            payload = response.json()
        except Exception as exc:
            raise EvaosLeaseError("managed MCP lease response is malformed") from exc
        return self._parse_lease(
            payload,
            expected_app_slug=material.app_slug,
            expected_external_user_id=material.external_user_id,
            expected_account_id=material.account_id,
        )

    def _parse_lease(
        self,
        payload: Any,
        *,
        expected_app_slug: str,
        expected_external_user_id: Optional[str] = None,
        expected_account_id: Optional[str] = None,
    ) -> EvaosMcpLease:
        if not isinstance(payload, dict) or set(payload) != {
            "mcp_url",
            "headers",
            "expires_at",
        }:
            raise EvaosLeaseError("managed MCP lease response is malformed")
        mcp_url = payload.get("mcp_url")
        try:
            parsed = urlparse(mcp_url) if isinstance(mcp_url, str) else None
            port = parsed.port if parsed is not None else None
        except ValueError as exc:
            raise EvaosLeaseError("managed MCP lease response is malformed") from exc
        if (
            parsed is None
            or (parsed.scheme, parsed.hostname) != _MCP_ORIGIN
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path.rstrip("/") != "/v3"
            or parsed.query
            or parsed.fragment
        ):
            raise EvaosLeaseError("managed MCP lease response is malformed")
        headers = payload.get("headers")
        if (
            not isinstance(headers, dict)
            or set(headers) != _REQUIRED_LEASE_HEADERS
            or not all(
                isinstance(key, str)
                and isinstance(value, str)
                and value.strip()
                and "\r" not in value
                and "\n" not in value
                for key, value in headers.items()
            )
            or not headers.get("Authorization", "").startswith("Bearer ")
            or len(headers["Authorization"]) <= len("Bearer ")
            or headers.get("x-pd-app-slug") != expected_app_slug
            or _ACCOUNT_ID_RE.fullmatch(headers.get("x-pd-account-id", "")) is None
            or (
                expected_external_user_id is not None
                and headers.get("x-pd-external-user-id") != expected_external_user_id
            )
            or (
                expected_account_id is not None
                and headers.get("x-pd-account-id") != expected_account_id
            )
        ):
            raise EvaosLeaseError("managed MCP lease response is malformed")
        raw_expiry = payload.get("expires_at")
        try:
            expires_at = datetime.fromisoformat(
                raw_expiry.replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvaosLeaseError("managed MCP lease response is malformed") from exc
        if expires_at.tzinfo is None:
            raise EvaosLeaseError("managed MCP lease response is malformed")
        expires_at = expires_at.astimezone(timezone.utc)
        if expires_at <= self._now() + self._refresh_skew:
            raise EvaosLeaseError("managed MCP lease response expires too soon")
        return EvaosMcpLease(
            mcp_url=mcp_url,
            headers=dict(headers),
            expires_at=expires_at,
        )


class EvaosLeaseHttpAuth(httpx.Auth):
    """Apply the active lease and retry one HTTP request after a 401."""

    requires_request_body = True

    def __init__(self, manager: EvaosLeaseManager):
        self._manager = manager

    def __repr__(self) -> str:
        return "EvaosLeaseHttpAuth(manager=<redacted>)"

    @staticmethod
    def _apply(request: httpx.Request, lease: EvaosMcpLease) -> None:
        request.url = httpx.URL(lease.mcp_url)
        for name in _REQUIRED_LEASE_HEADERS:
            request.headers.pop(name, None)
            request.headers.pop(name.lower(), None)
        request.headers.update(lease.headers)

    async def async_auth_flow(self, request: httpx.Request):
        lease = await self._manager.get_lease()
        self._apply(request, lease)
        response = yield request
        if response.status_code != 401:
            return
        await response.aread()
        refreshed = await self._manager.get_lease(
            force_refresh=True,
            rejected_authorization=lease.authorization,
        )
        self._apply(request, refreshed)
        yield request

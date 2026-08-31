"""LdapAuthProvider — LDAP / Active Directory dashboard auth (password login).

Plugs into the ``DashboardAuthProvider`` framework as a pure-password
provider (``supports_password = True``): the login page renders a
credential form, ``/auth/password-login`` calls
``complete_password_login``, and everything downstream (session cookies,
verify, refresh, WS tickets, logout, audit) is the shared framework path.

Credentials are verified with an **LDAP bind** — never stored, hashed, or
compared locally. Two mutually exclusive bind modes:

  * **Direct bind** — ``user_dn_template`` like
    ``uid={username},ou=people,dc=example,dc=com``. The username is
    RDN-escaped and substituted, then the provider binds as that DN with
    the supplied password. Simple; no service account.
  * **Search-then-bind** — a service account (``bind_dn`` +
    ``bind_password``; empty ``bind_dn`` = anonymous) searches
    ``user_search_base`` with ``user_search_filter`` (default
    ``(uid={username})``; use ``(sAMAccountName={username})`` for AD) for
    exactly one entry, then re-binds as the found DN. Email / display
    name come from the entry, and ``refresh_session`` re-checks the DN
    still exists so disabled accounts are cut off at the next
    access-token expiry.

Sessions are stateless HMAC-signed tokens minted by this provider (same
scheme as ``plugins/dashboard_auth/basic``): ``verify_session`` — called
on every request — never touches the directory. Only login and (search
mode) refresh do LDAP I/O, and both carry connect/receive timeouts.

Security invariants:
  * An **empty or whitespace password is rejected before any bind** —
    LDAP servers treat an empty password as a successful *anonymous*
    bind, so skipping this check would be a full auth bypass.
  * Usernames are escaped per RFC 4515 (search filters) / RFC 4514
    (DN templates) before interpolation — no LDAP injection.
  * ``ldaps://`` or StartTLS is required unless ``allow_insecure`` is
    set explicitly; certificate validation is on by default.
  * Unknown-user vs wrong-password is never distinguished; on unknown
    user (search mode) a dummy bind equalises timing.

Configuration surfaces (env wins over config.yaml when set non-empty),
mirroring the ``basic`` provider's precedence convention:

  ``config.yaml`` — canonical surface::

      dashboard:
        ldap_auth:
          server_url: ldaps://ldap.example.com        # required
          # EITHER (direct bind):
          user_dn_template: "uid={username},ou=people,dc=example,dc=com"
          # OR (search-then-bind):
          bind_dn: "cn=hermes,ou=svc,dc=example,dc=com"   # empty = anonymous search
          bind_password: "..."
          user_search_base: "ou=people,dc=example,dc=com"
          user_search_filter: "(uid={username})"      # optional (default shown)
          # Optional hardening / shaping:
          require_group: "cn=hermes-users,ou=groups,dc=example,dc=com"
          start_tls: false
          allow_insecure: false                       # permit plain ldap://
          ca_certs_file: /etc/ssl/private-ca.pem
          email_attribute: mail
          display_name_attribute: cn
          display_name: "LDAP"                        # login-form label
          secret: "<32+ random bytes, base64 or hex>" # token-signing key
          session_ttl_seconds: 43200                  # 12h access tokens
          refresh_ttl_seconds: 2592000                # 30d refresh tokens
          timeout_seconds: 5
          verify_user_on_refresh: true                # search mode only

  Environment overrides::

      HERMES_DASHBOARD_LDAP_SERVER_URL
      HERMES_DASHBOARD_LDAP_USER_DN_TEMPLATE
      HERMES_DASHBOARD_LDAP_BIND_DN
      HERMES_DASHBOARD_LDAP_BIND_PASSWORD
      HERMES_DASHBOARD_LDAP_USER_SEARCH_BASE
      HERMES_DASHBOARD_LDAP_USER_SEARCH_FILTER
      HERMES_DASHBOARD_LDAP_REQUIRE_GROUP
      HERMES_DASHBOARD_LDAP_START_TLS          # "1"/"true" to enable
      HERMES_DASHBOARD_LDAP_ALLOW_INSECURE     # "1"/"true" to enable
      HERMES_DASHBOARD_LDAP_CA_CERTS_FILE
      HERMES_DASHBOARD_LDAP_SECRET
      HERMES_DASHBOARD_LDAP_TTL_SECONDS

The ``ldap3`` dependency is lazy-installed via ``tools/lazy_deps.py``
("auth.ldap") only when the plugin is actually configured; this module
never imports ldap3 at import time.

Skip reasons:
  Like the other bundled providers, exposes module-level
  ``LAST_SKIP_REASON`` for the auth gate's fail-closed diagnostics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Callable, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCredentialsError,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)

logger = logging.getLogger(__name__)


_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12h access tokens
_DEFAULT_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30d refresh tokens
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_USER_SEARCH_FILTER = "(uid={username})"

# Fixed-length HMAC-SHA256 suffix on signed tokens (same scheme as the
# ``basic`` provider — binary HMAC bytes can't collide with a delimiter).
_SIG_LEN = hashlib.sha256().digest_size

# Nonexistent DN used to equalise timing when a search finds no user:
# we still attempt one bind so "unknown user" and "wrong password" cost
# roughly the same wall-clock at this endpoint.
_DUMMY_BIND_DN = "uid=hermes-nonexistent-timing-pad,dc=invalid"

LAST_SKIP_REASON: str = ""


# ---------------------------------------------------------------------------
# Token signing (stateless HMAC-signed blobs — same scheme as `basic`)
# ---------------------------------------------------------------------------


def _sign(payload: dict, secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign(token: str, secret: bytes) -> Optional[dict]:
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LdapAuthProvider(DashboardAuthProvider):
    """LDAP-bind password provider with stateless HMAC-signed sessions."""

    name = "ldap"
    display_name = "LDAP"
    supports_password = True

    def __init__(
        self,
        *,
        server_url: str,
        secret: bytes,
        user_dn_template: str = "",
        bind_dn: str = "",
        bind_password: str = "",
        user_search_base: str = "",
        user_search_filter: str = _DEFAULT_USER_SEARCH_FILTER,
        require_group: str = "",
        email_attribute: str = "mail",
        display_name_attribute: str = "cn",
        display_name: str = "LDAP",
        start_tls: bool = False,
        allow_insecure: bool = False,
        ca_certs_file: str = "",
        session_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        verify_user_on_refresh: bool = True,
        connection_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not (
            server_url.startswith("ldap://")
            or server_url.startswith("ldaps://")
        ):
            raise ValueError(
                "server_url must start with ldap:// or ldaps:// "
                f"(got {server_url!r})"
            )
        if (
            server_url.startswith("ldap://")
            and not start_tls
            and not allow_insecure
        ):
            raise ValueError(
                "plain ldap:// without start_tls sends passwords in "
                "cleartext; set start_tls: true, use ldaps://, or set "
                "allow_insecure: true to accept the risk explicitly"
            )
        if len(secret) < 16:
            raise ValueError("secret must be at least 16 bytes")
        if user_dn_template and user_search_base:
            raise ValueError(
                "user_dn_template (direct bind) and user_search_base "
                "(search-then-bind) are mutually exclusive — configure "
                "exactly one"
            )
        if not user_dn_template and not user_search_base:
            raise ValueError(
                "no bind mode configured: set user_dn_template (direct "
                "bind) or user_search_base (search-then-bind)"
            )
        if user_dn_template and "{username}" not in user_dn_template:
            raise ValueError(
                "user_dn_template must contain the {username} placeholder"
            )
        if user_search_base and "{username}" not in user_search_filter:
            raise ValueError(
                "user_search_filter must contain the {username} placeholder"
            )

        self._server_url = server_url
        self._secret = secret
        self._user_dn_template = user_dn_template
        self._bind_dn = bind_dn
        self._bind_password = bind_password
        self._user_search_base = user_search_base
        self._user_search_filter = user_search_filter
        self._require_group = require_group
        self._email_attr = email_attribute
        self._display_attr = display_name_attribute
        self.display_name = display_name or "LDAP"
        self._start_tls = start_tls
        self._ca_certs_file = ca_certs_file
        self._ttl = max(60, int(session_ttl_seconds))
        self._refresh_ttl = max(300, int(refresh_ttl_seconds))
        self._timeout = float(timeout_seconds)
        self._verify_user_on_refresh = bool(verify_user_on_refresh)
        self._factory = connection_factory or self._default_factory

    # ---- OAuth methods: not used (pure-password provider) ------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(
            "LdapAuthProvider is password-only; there is no OAuth redirect "
            "flow. The login page POSTs to /auth/password-login instead."
        )

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError(
            "LdapAuthProvider is password-only; use complete_password_login."
        )

    # ---- password login ------------------------------------------------------

    def complete_password_login(
        self, *, username: str, password: str
    ) -> Session:
        username = (username or "").strip()
        # SECURITY: an empty password is an ANONYMOUS bind on LDAP servers
        # (RFC 4513 §5.1.2) and would "succeed" — reject before any bind.
        if not username or not password or not password.strip():
            raise InvalidCredentialsError("invalid username or password")

        if self._user_dn_template:
            from ldap3.utils.dn import escape_rdn

            user_dn = self._user_dn_template.format(
                username=escape_rdn(username)
            )
            attrs: dict = {}
        else:
            user_dn, attrs = self._search_user(username)
            if user_dn is None:
                # Timing pad: unknown-user should cost about the same as
                # wrong-password, so attempt one bind against a fixed
                # nonexistent DN before rejecting.
                pad = self._bind(user=_DUMMY_BIND_DN, password=password)
                if pad is not None:  # pragma: no cover — defensive
                    pad.unbind()
                raise InvalidCredentialsError("invalid username or password")

        conn = self._bind(user=user_dn, password=password)
        if conn is None:
            raise InvalidCredentialsError("invalid username or password")
        try:
            if self._require_group and not self._user_in_group(
                conn, user_dn, username
            ):
                raise InvalidCredentialsError(
                    "invalid username or password"
                )
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001 — teardown must not mask errors
                pass
        return self._mint_session(username, user_dn, attrs)

    # ---- internals: LDAP I/O -------------------------------------------------

    def _default_factory(
        self, *, user: Optional[str], password: Optional[str]
    ):
        """Build a real ldap3 Connection (unbound) with TLS + timeouts.

        Imported lazily — ldap3 is only installed once the plugin is
        configured (tools/lazy_deps.py "auth.ldap").
        """
        import ssl

        import ldap3

        tls = None
        if self._server_url.startswith("ldaps://") or self._start_tls:
            tls = ldap3.Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=self._ca_certs_file or None,
            )
        server = ldap3.Server(
            self._server_url,
            connect_timeout=self._timeout,
            tls=tls,
            get_info=ldap3.NONE,
        )
        conn = ldap3.Connection(
            server,
            user=user or None,
            password=password or None,
            client_strategy=ldap3.SYNC,
            receive_timeout=self._timeout,
            raise_exceptions=False,
            auto_bind=False,
        )
        if self._start_tls:
            # open() connects the socket; if the StartTLS upgrade then
            # fails, ldap3 leaves that socket open (strategy/base.py
            # raises without closing). Close it ourselves or every failed
            # login against a TLS-broken directory leaks an fd.
            try:
                conn.open()
                conn.start_tls()
            except Exception:
                try:
                    conn.unbind()
                except Exception:  # noqa: BLE001 — cleanup must not mask
                    pass
                raise
        return conn

    def _bind(
        self, *, user: Optional[str], password: Optional[str]
    ):
        """Create a connection and bind. Bound connection, or None if the
        server rejected the credentials. ProviderError on transport
        failure (unreachable, TLS failure, timeout)."""
        from ldap3.core.exceptions import LDAPException

        conn = None
        try:
            conn = self._factory(user=user, password=password)
            ok = conn.bind()
        except LDAPException as exc:
            # The connection may already own a connected (or half-open)
            # socket: ldap3 2.9.1 raises LDAPSocketOpenError from a failed
            # connect() / TLS wrap without closing it. Close it here, or a
            # down directory leaks one fd per hit on this unauthenticated
            # endpoint.
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:  # noqa: BLE001 — cleanup must not mask
                    pass
            raise ProviderError(f"LDAP server unreachable: {exc}") from exc
        if not ok:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass
            return None
        return conn

    # ---- session lifecycle (stateless tokens — no LDAP I/O) ----------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        payload = _unsign(access_token, self._secret)
        if (
            payload is None
            or payload.get("kind") != "access"
            or payload.get("exp", 0) <= int(time.time())
        ):
            return None
        return self._session_from_payload(access_token, "", payload)

    def refresh_session(self, *, refresh_token: str) -> Session:
        if not refresh_token:
            raise RefreshExpiredError("no refresh token present in session")
        payload = _unsign(refresh_token, self._secret)
        if (
            payload is None
            or payload.get("kind") != "refresh"
            or payload.get("exp", 0) <= int(time.time())
        ):
            raise RefreshExpiredError("refresh token expired or invalid")
        # Search mode: re-check the account still exists in the directory
        # so a disabled/deleted user is cut off at access-token expiry
        # instead of riding the 30-day refresh horizon. Direct mode has no
        # service credentials to search with, so refresh is token-only
        # there (documented tradeoff).
        if self._verify_user_on_refresh and self._user_search_base:
            if not self._user_still_present(str(payload.get("dn", ""))):
                raise RefreshExpiredError(
                    "user no longer present in directory"
                )
        return self._mint_session(
            str(payload.get("sub", "")),
            str(payload.get("dn", "")),
            {
                "email": str(payload.get("em", "")),
                "display": str(payload.get("nm", "")),
            },
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        # Stateless tokens — nothing to revoke server-side. Best-effort
        # no-op, must not raise.
        _ = refresh_token
        return None

    # ---- internals: token minting ------------------------------------------

    def _mint_session(
        self, username: str, user_dn: str, attrs: dict
    ) -> Session:
        now = int(time.time())
        exp = now + self._ttl
        email = str(attrs.get("email", "") or "")
        display = str(attrs.get("display", "") or "") or username
        access_token = _sign(
            {
                "sub": username, "dn": user_dn, "em": email, "nm": display,
                "kind": "access", "exp": exp,
            },
            self._secret,
        )
        refresh_token = _sign(
            {
                "sub": username, "dn": user_dn, "em": email, "nm": display,
                "kind": "refresh", "exp": now + self._refresh_ttl,
            },
            self._secret,
        )
        return Session(
            user_id=username,
            email=email,
            display_name=display,
            org_id="",
            provider=self.name,
            expires_at=exp,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    def _session_from_payload(
        self, access_token: str, refresh_token: str, payload: dict
    ) -> Session:
        username = str(payload.get("sub", ""))
        return Session(
            user_id=username,
            email=str(payload.get("em", "")),
            display_name=str(payload.get("nm", "")) or username,
            org_id="",
            provider=self.name,
            expires_at=int(payload["exp"]),
            access_token=access_token,
            refresh_token=refresh_token,
        )

    # ---- internals: LDAP I/O (implemented in later tasks) -------------------

    def _search_user(self, username: str):
        """Search-then-bind leg 1: find the user's DN + profile attributes.

        Returns ``(user_dn, {"email": ..., "display": ...})`` on a unique
        match, ``(None, {})`` when no entry matches. Zero matches and
        multiple matches are both treated as "not found" — a filter that
        matches several entries must never let a bind against ANY of them
        succeed. Raises ``ProviderError`` when the service bind is
        rejected or the directory is unreachable — both mean *we* cannot
        verify anyone, which is an operator problem (503), not a
        credentials problem (401).
        """
        import ldap3
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars

        conn = self._bind(
            user=self._bind_dn or None,
            password=self._bind_password or None,
        )
        if conn is None:
            raise ProviderError(
                "LDAP service-account bind was rejected — check "
                "dashboard.ldap_auth.bind_dn / bind_password"
            )
        try:
            flt = self._user_search_filter.format(
                username=escape_filter_chars(username)
            )
            ok = conn.search(
                search_base=self._user_search_base,
                search_filter=flt,
                search_scope=ldap3.SUBTREE,
                attributes=[self._email_attr, self._display_attr],
            )
            entries = list(conn.entries) if ok else []
        except LDAPException as exc:
            raise ProviderError(f"LDAP search failed: {exc}") from exc
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass

        if len(entries) != 1:
            if len(entries) > 1:
                logger.warning(
                    "dashboard-auth-ldap: user_search_filter matched %d "
                    "entries for a single username — rejecting login. "
                    "Tighten dashboard.ldap_auth.user_search_filter.",
                    len(entries),
                )
            return None, {}

        entry = entries[0]

        def _first(attr_name: str) -> str:
            try:
                val = entry[attr_name].value
            except Exception:  # noqa: BLE001 — attribute absent
                return ""
            if isinstance(val, (list, tuple)):
                val = val[0] if val else ""
            return str(val or "")

        return entry.entry_dn, {
            "email": _first(self._email_attr),
            "display": _first(self._display_attr),
        }

    def _user_in_group(self, conn, user_dn: str, username: str) -> bool:
        """BASE-scope membership probe on the require_group entry.

        Covers the three common group schemas in one filter:
        groupOfNames (member), groupOfUniqueNames (uniqueMember), and
        posixGroup (memberUid — matched by username, not DN). Runs on the
        user's own freshly-bound connection in both modes, so the
        directory ACLs must let authenticated users read the group entry
        (the default on OpenLDAP and AD).
        """
        import ldap3
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars

        dn_esc = escape_filter_chars(user_dn)
        uid_esc = escape_filter_chars(username)
        flt = (
            f"(|(member={dn_esc})"
            f"(uniqueMember={dn_esc})"
            f"(memberUid={uid_esc}))"
        )
        try:
            ok = conn.search(
                search_base=self._require_group,
                search_filter=flt,
                search_scope=ldap3.BASE,
                attributes=[],
            )
        except LDAPException as exc:
            raise ProviderError(
                f"LDAP group check failed: {exc}"
            ) from exc
        return bool(ok and conn.entries)

    def _user_still_present(self, user_dn: str) -> bool:
        """Refresh-time existence probe (search mode only).

        BASE-scope search on the user's DN with the service account.
        False → the account was deleted/moved (caller raises
        RefreshExpiredError). ProviderError propagates when the directory
        is unreachable — per the framework contract the middleware then
        503s without clearing cookies.
        """
        if not user_dn:
            return False
        import ldap3
        from ldap3.core.exceptions import LDAPException

        conn = self._bind(
            user=self._bind_dn or None,
            password=self._bind_password or None,
        )
        if conn is None:
            raise ProviderError(
                "LDAP service-account bind was rejected during refresh"
            )
        try:
            ok = conn.search(
                search_base=user_dn,
                search_filter="(objectClass=*)",
                search_scope=ldap3.BASE,
                attributes=[],
            )
            return bool(ok and conn.entries)
        except LDAPException:
            # A BASE search on a nonexistent DN raises noSuchObject on
            # many servers rather than returning an empty result — that
            # is "user gone", not an outage (the bind above already
            # proved the directory reachable).
            return False
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass

"""Account SID + Auth Token resolution and Basic Auth header, shared by
every core-Twilio (Messages API) channel. Email uses its own SendGrid
credentials instead — see channels/email.py."""

import base64
import os

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"


def get_scoped_secret(name, default=None):
    """Scope-aware read: under multiplex, a secondary profile's secrets
    live only in its secret scope, not os.environ."""
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def basic_auth_header(account_sid: str, auth_token: str) -> str:
    creds = f"{account_sid}:{auth_token}".encode("ascii")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


def get_account_credentials(pconfig=None) -> tuple[str, str]:
    """(account_sid, auth_token). pconfig.api_key wins for auth_token when
    present, mirroring other Hermes standalone-send call sites."""
    account_sid = get_scoped_secret("TWILIO_ACCOUNT_SID", "")
    auth_token = (
        getattr(pconfig, "api_key", None) if pconfig is not None else None
    ) or get_scoped_secret("TWILIO_AUTH_TOKEN", "")
    return account_sid, auth_token

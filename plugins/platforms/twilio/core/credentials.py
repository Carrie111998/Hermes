"""Shared Twilio credential helpers.

Every channel in this plugin (RCS today; SMS/MMS/WhatsApp/Voice/Email
later) authenticates to Twilio the same way — Account SID + Auth Token,
HTTP Basic Auth. Keeping that logic here means channel modules never
need to duplicate or diverge on how credentials are resolved.
"""

import base64
import os

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"


def get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Under multiplex, a secondary profile's secrets live only in its secret
    scope, not os.environ — a bare os.getenv would find nothing there.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def basic_auth_header(account_sid: str, auth_token: str) -> str:
    creds = f"{account_sid}:{auth_token}".encode("ascii")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


def get_account_credentials(pconfig=None) -> tuple[str, str]:
    """Return (account_sid, auth_token).

    pconfig.api_key (when present) wins for auth_token — mirrors the
    standalone-send call sites elsewhere in Hermes (e.g. the built-in sms
    plugin), which read the platform config's api_key before falling back
    to the env/secret-scope lookup.
    """
    account_sid = get_scoped_secret("TWILIO_ACCOUNT_SID", "")
    auth_token = (
        getattr(pconfig, "api_key", None) if pconfig is not None else None
    ) or get_scoped_secret("TWILIO_AUTH_TOKEN", "")
    return account_sid, auth_token

"""Identity-scoped Google credential registry, shared by google_api.py and setup.py.

Each identity maps to its own credential directory (a token JSON + a client-secret
JSON) and its own OAuth scope set. Adding a new identity's Google access later is
additive: add an entry to IDENTITIES below, then run
``setup.py --identity <name> --client-secret <path>`` to provision it — no other
code changes required, and no scope, path, or fallback is shared between entries.

FAIL-CLOSED by design, mirroring ``agent/secret_scope.py``'s philosophy elsewhere
in this codebase: callers must pass an explicit, already-resolved identity (the
same resolution already used for Family Rules/Actions routing — this module does
no identity resolution of its own). There is no default identity and no fallback:
an unresolved or unrecognized identity is a caller bug to fix at the call site,
never something to paper over by reaching for JID's (or anyone else's) paths.
"""
from __future__ import annotations

from pathlib import Path

from _hermes_home import get_hermes_home

HERMES_HOME = get_hermes_home()

# JID's own router account — unchanged scope, unchanged file locations
# (HERMES_HOME root). This is a purely additive migration: his existing
# google_token.json / google_client_secret.json need no changes at all.
_JID_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts",  # read + write (upgraded from contacts.readonly)
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

# Zee's (Zarkash's) old personal Gmail account — deliberately narrower than
# JID's router account: Gmail is read + draft-only (no send, no modify beyond
# drafting — same drafting-only discipline already used for JID's own personal
# Proton mailbox), Calendar and Drive are full access, and Contacts is full
# read/write (Zee is rebuilding this account's contact list from scratch, so
# write access supports Jarvis adding contacts on his behalf).
_ZARKASH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts only — never send
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts",  # read + write
]

IDENTITIES: dict[str, dict] = {
    "jid": {
        "credentials_dir": HERMES_HOME,
        "scopes": _JID_SCOPES,
    },
    "zarkash": {
        "credentials_dir": HERMES_HOME / "family_credentials" / "zarkash",
        "scopes": _ZARKASH_SCOPES,
    },
}


class UnknownGoogleIdentityError(RuntimeError):
    """Raised when a Google credential lookup has no, or an unregistered, identity.

    This is the fail-closed signal: it means a credential read reached
    ``get_google_credentials`` without a valid, registered identity, which would
    otherwise risk defaulting to (or guessing) someone else's credentials. The
    fix is to resolve the identity correctly at the call site and pass it
    explicitly — never to add a fallback here.
    """


def get_google_credentials(identity: str | None) -> tuple[Path, Path, list[str]]:
    """Resolve (token_path, client_secret_path, scopes) for a resolved identity.

    FAIL-CLOSED: raises ``UnknownGoogleIdentityError`` for a missing or
    unregistered identity. There is no default and no fallback to any other
    identity's credentials.
    """
    if not identity:
        raise UnknownGoogleIdentityError(
            "get_google_credentials() called with no identity. The caller must "
            "resolve which person this turn is for (same resolution already "
            "used for Family Rules/Actions routing) and pass it explicitly — "
            "there is no default identity."
        )
    entry = IDENTITIES.get(identity)
    if entry is None:
        raise UnknownGoogleIdentityError(
            f"get_google_credentials(identity={identity!r}): no Google "
            f"credentials registered for this identity. Known identities: "
            f"{sorted(IDENTITIES)}. To add a new one, add an entry to "
            f"IDENTITIES in _google_identities.py, then run "
            f"setup.py --identity {identity!r} --client-secret <path>."
        )
    cred_dir = entry["credentials_dir"]
    return (
        cred_dir / "google_token.json",
        cred_dir / "google_client_secret.json",
        list(entry["scopes"]),
    )

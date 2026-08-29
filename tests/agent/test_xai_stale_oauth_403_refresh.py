"""Regression tests: xAI signals an expired OAuth token with 403, not 401.

xAI answers an access token that has simply aged out with HTTP 403 and a
body carrying either ``[WKE=unauthenticated:...]`` or ``OAuth2 access token
could not be validated`` -- never a 401.  #29344 taught the credential-pool
recovery path to tell those bodies apart from the entitlement 403s that a
refresh can never fix, but the singleton ``codex_responses`` refresh branch
in ``run_conversation`` kept gating on ``status_code == 401`` alone.

Consequence in the field: a long-running gateway on ``xai-oauth`` held a
token that died ~6h earlier and had no path back.  Neither recovery arm
fired -- the pool arm returns early whenever the agent carries no
credential pool, and the singleton arm could not see a 403 -- so Grok
stayed dead until the process was restarted.  ``hermes update`` restarts
the stack and re-mints, which is why updating appeared to "fix Grok" for
exactly one token lifetime at a time.

These lock in the shared predicate and the widened gate.
"""

import re
from pathlib import Path

import pytest

from agent.agent_runtime_helpers import is_xai_stale_oauth_error


# The exact body observed in production gateway logs.
REAL_EXPIRED_BODY = (
    'HTTP 403: {"code":"unauthenticated:bad-credentials",'
    '"error":"The OAuth2 access token could not be validated."}'
)

ENTITLEMENT_BODY = (
    "You have either run out of available resources or do not have an "
    "active Grok subscription"
)


@pytest.mark.parametrize(
    "error_context,status_code,expected",
    [
        # -- recoverable: the token is merely stale -------------------------
        ({"message": REAL_EXPIRED_BODY}, 403, True),
        ({"message": "boom [WKE=unauthenticated:bad-credentials]"}, 403, True),
        ({"error": "The OAuth2 access token could not be validated."}, 403, True),
        # xAI has shipped both casings; the match must be case-insensitive
        ({"message": "OAUTH2 ACCESS TOKEN COULD NOT BE VALIDATED"}, 403, True),
        # -- NOT recoverable: refreshing cannot fix these -------------------
        ({"message": ENTITLEMENT_BODY}, 403, False),
        (
            {
                "message": "oauth authentication is currently not allowed "
                "for this organization"
            },
            403,
            False,
        ),
        # -- wrong status: the predicate is 403-only ------------------------
        ({"message": REAL_EXPIRED_BODY}, 401, False),
        ({"message": REAL_EXPIRED_BODY}, 429, False),
        ({"message": REAL_EXPIRED_BODY}, None, False),
        # -- degenerate input never raises ----------------------------------
        (None, 403, False),
        ({}, 403, False),
        ("not-a-dict", 403, False),
        ({"message": None, "code": None}, 403, False),
    ],
)
def test_stale_oauth_predicate(error_context, status_code, expected):
    assert is_xai_stale_oauth_error(error_context, status_code) is expected


def test_singleton_refresh_gate_accepts_the_403():
    """The codex_responses refresh branch must not be 401-only anymore.

    Asserted against source text because exercising the branch for real
    means standing up the whole streaming conversation loop; the gate is a
    four-line boolean whose regression mode is silent.
    """
    src = Path(__file__).resolve().parents[2] / "agent" / "conversation_loop.py"
    body = src.read_text(encoding="utf-8")

    gate = re.search(
        r"if \(\s*\n\s*agent\.api_mode == .codex_responses.\s*\n"
        r"\s*and agent\.provider in \{.openai-codex., .xai-oauth.\}\s*\n"
        r"\s*and ([^\n]+)\n",
        body,
    )
    assert gate, "codex_responses auth-refresh gate not found"
    condition = gate.group(1)
    assert "401" in condition, "401 handling must be preserved"
    assert "_xai_stale_oauth" in condition, (
        "the gate still ignores xAI's 403 expiry signal -- a stale "
        "xai-oauth token has no recovery path"
    )


def test_pool_path_reuses_the_shared_predicate():
    """The pool arm must call the helper, not keep a second copy of it."""
    src = Path(__file__).resolve().parents[2] / "agent" / "agent_runtime_helpers.py"
    body = src.read_text(encoding="utf-8")

    recover = body[body.index("def recover_with_credential_pool(") :]
    assert "is_xai_stale_oauth_error(error_context, status_code)" in recover
    assert "_is_xai_auth_failure" not in recover, (
        "inline duplicate of the predicate is back; the two arms will drift"
    )

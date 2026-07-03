"""Regression guard: the Railway entrypoint must not force ``--insecure``.

``docker/entrypoint.sh`` is this fork's bespoke Railway boot path ("Strategy
B" — invoked directly as Railway's ``startCommand``, bypassing s6). It is the
sibling of ``docker/s6-rc.d/dashboard/run`` (guarded by
``tests/test_docker_home_override_scripts.py``), and it used to carry the same
bug: auto-adding ``--insecure`` for any non-loopback bind, which silently
disabled the dashboard's OAuth auth gate — the gate the Hermes Desktop app
relies on to authenticate "Remote gateway" connections.

These are pure static-text checks (no Docker build) so they run in unit CI and
catch a regression if a future upstream merge clobbers the bespoke entrypoint.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"


def test_entrypoint_does_not_derive_insecure_from_bind_host() -> None:
    """The legacy "non-loopback bind implies ``--insecure``" auto-flip must be
    gone. ``--insecure`` is now opt-in via ``HERMES_DASHBOARD_INSECURE``; the
    OAuth auth gate is the authority on whether a public bind is safe.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")

    # The explicit opt-in env var must drive the escape hatch.
    assert "HERMES_DASHBOARD_INSECURE" in text, (
        "Explicit HERMES_DASHBOARD_INSECURE opt-in is missing from the "
        "Railway entrypoint."
    )

    # ``--insecure`` may only be appended inside the HERMES_DASHBOARD_INSECURE
    # branch — never unconditionally under a bare host check.
    insecure_case_idx = text.find('case "${HERMES_DASHBOARD_INSECURE:-}" in')
    assert insecure_case_idx != -1, (
        "Expected an explicit `case \"${HERMES_DASHBOARD_INSECURE:-}\" in` "
        "block gating the --insecure escape hatch."
    )
    assert "dash_args+=(--insecure)" in text, (
        "The --insecure escape hatch should still be reachable (opt-in)."
    )
    assert text.index("dash_args+=(--insecure)") > insecure_case_idx, (
        "--insecure is added outside the HERMES_DASHBOARD_INSECURE opt-in "
        "branch — the host-derived auto-flip regression is back."
    )

    # Truthy values aligned with the rest of the boot scripts.
    for truthy in ("1", "true", "TRUE", "True", "yes", "YES", "Yes"):
        assert truthy in text, (
            f"HERMES_DASHBOARD_INSECURE should accept truthy value {truthy!r}"
        )


def test_entrypoint_engages_oauth_gate_from_client_id() -> None:
    """The Nous Portal OAuth gate must be wired: the entrypoint reads the
    provisioned client id and derives the public redirect URL behind Railway's
    TLS-terminating proxy.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")

    assert "HERMES_DASHBOARD_OAUTH_CLIENT_ID" in text, (
        "Entrypoint must engage the OAuth gate from "
        "HERMES_DASHBOARD_OAUTH_CLIENT_ID."
    )
    # Behind Railway's proxy the OAuth redirect_uri must be the public https
    # URL — derived from Railway's public domain when not set explicitly.
    assert "RAILWAY_PUBLIC_DOMAIN" in text and "HERMES_DASHBOARD_PUBLIC_URL" in text, (
        "Entrypoint must derive HERMES_DASHBOARD_PUBLIC_URL from "
        "RAILWAY_PUBLIC_DOMAIN so the OAuth redirect_uri is correct."
    )


def test_entrypoint_fails_safe_without_an_auth_gate() -> None:
    """A non-loopback bind with no gate configured must NOT start the
    dashboard (no unauthenticated public exposure, no crash-loop).
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")

    assert "dash_start=0" in text, (
        "Entrypoint must fail safe (skip starting the dashboard) when a "
        "non-loopback bind has no OAuth/basic/insecure posture configured."
    )

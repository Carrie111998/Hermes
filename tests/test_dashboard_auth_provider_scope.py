"""Dashboard-auth providers must stay visible under every home scope.

One dashboard process serves every profile: it is launched with
``--open-profile`` and handles ``?profile=<name>`` requests, each of which runs
under ``set_hermes_home_override``. The auth gate reads the provider registry
inside that override.

Registering the provider per-home therefore broke the gate — it looked the
provider up under a home it was never registered against, found nothing, and
failed closed: ``/api/auth/providers`` 503, ``/login`` "Sign-in unavailable",
and valid sessions rejected as ``invalid_or_expired_session``.
"""

from hermes_cli.dashboard_auth import registry
from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)


class _StubProvider:
    """Minimal provider satisfying ``assert_protocol_compliance``."""

    name = "stub"
    display_name = "Stub"
    supports_password = True
    supports_session = True

    # Never called here — this test is about registry visibility, not auth.
    def start_login(self, redirect_uri):  # pragma: no cover
        raise NotImplementedError

    def complete_login(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def verify_session(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def refresh_session(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    def revoke_session(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


def test_provider_visible_under_a_different_home_override(tmp_path):
    launch_home = tmp_path / "launch"
    other_profile = tmp_path / "profiles" / "other"
    launch_home.mkdir(parents=True)
    other_profile.mkdir(parents=True)

    provider = _StubProvider()
    # scope=None is what PluginContext.register_dashboard_auth_provider uses:
    # the gate is process-global, so the registration must be too.
    registry.register_provider(provider, scope=None)
    try:
        assert provider in registry.list_providers()

        # A profile-scoped request: the gate now runs under another home.
        token = set_hermes_home_override(str(other_profile))
        try:
            assert provider in registry.list_providers(), (
                "provider vanished under a profile-scoped home override"
            )
            assert registry.get_provider("stub") is provider
            assert provider in registry.list_session_providers()
        finally:
            reset_hermes_home_override(token)
    finally:
        registry.clear_providers()


def test_per_home_registration_is_invisible_to_other_scopes(tmp_path):
    """Guards the regression itself: scoping this registry by home is what broke."""
    other_profile = tmp_path / "profiles" / "other"
    other_profile.mkdir(parents=True)

    provider = _StubProvider()
    registry.register_provider(provider, scope=str(tmp_path / "launch"))
    try:
        token = set_hermes_home_override(str(other_profile))
        try:
            assert provider not in registry.list_providers()
        finally:
            reset_hermes_home_override(token)
    finally:
        registry.clear_providers()

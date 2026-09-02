"""Tests for cua-driver child-environment policy.

cua-driver ships anonymous PostHog telemetry ENABLED by default upstream.
Hermes disables it unless the user opts in via
``computer_use.cua_telemetry: true``. The policy is applied by injecting
``CUA_DRIVER_RS_TELEMETRY_ENABLED=0`` into every cua-driver child env.

Linux children also retain cua-driver's desktop-aware accessibility policy
when a system gateway lacks graphical-session environment variables.

These assert the behavior contract (default disables, opt-in leaves the var
untouched, config failure fails safe toward disabled), not specific config
snapshots.
"""

from unittest.mock import patch

from tools.computer_use import cua_backend


_VAR = "CUA_DRIVER_RS_TELEMETRY_ENABLED"
_A11Y_VAR = "CUA_DRIVER_RS_A11Y_ADVERTISE_MODE"


class TestTelemetryDisabledFlag:

    def test_explicit_false_disables(self):
        with patch("hermes_cli.config.load_config",
                   return_value={"computer_use": {"cua_telemetry": False}}):
            assert cua_backend._cua_telemetry_disabled() is True


    def test_config_load_failure_fails_safe(self):
        # Unreadable config => default to disabling telemetry (privacy-safe).
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            assert cua_backend._cua_telemetry_disabled() is True



class TestChildEnv:
    def test_disabled_injects_var_zero(self):
        with patch.object(cua_backend, "_cua_telemetry_disabled", return_value=True):
            env = cua_backend.cua_driver_child_env({"PATH": "/usr/bin"})
            assert env[_VAR] == "0"
            # base env is preserved
            assert env["PATH"] == "/usr/bin"



    def test_disabled_overrides_inherited_enabled(self):
        # Even if the parent process had telemetry enabled, the default policy
        # forces it off in the child.
        with patch.object(cua_backend, "_cua_telemetry_disabled", return_value=True):
            env = cua_backend.cua_driver_child_env({_VAR: "1"})
            assert env[_VAR] == "0"

    def test_gnome_and_cosmic_force_non_activating_a11y_advertisement(self):
        # GNOME treats ScreenReaderEnabled=true as a request to persist the
        # setting and launch Orca. Hermes must retain AT-SPI support without
        # changing that user preference, even if the parent inherited an
        # unsafe advertisement mode.
        for desktop in ("ubuntu:GNOME", "COSMIC", "pop:COSMIC"):
            with patch.object(cua_backend.sys, "platform", "linux"):
                env = cua_backend.cua_driver_child_env({
                    "XDG_CURRENT_DESKTOP": desktop,
                    _A11Y_VAR: "all",
                })
            assert env[_A11Y_VAR] == "is_enabled_only", desktop

    def test_gnome_preserves_stricter_inherited_none_mode(self):
        with patch.object(cua_backend.sys, "platform", "linux"):
            env = cua_backend.cua_driver_child_env({
                "XDG_CURRENT_DESKTOP": "GNOME",
                _A11Y_VAR: "none",
            })
        assert env[_A11Y_VAR] == "none"

    def test_cinnamon_disables_a11y_advertisement(self):
        for desktop in ("Cinnamon", "X-Cinnamon", "GNOME:X-Cinnamon"):
            with patch.object(cua_backend.sys, "platform", "linux"):
                env = cua_backend.cua_driver_child_env({
                    "XDG_CURRENT_DESKTOP": desktop,
                    _A11Y_VAR: "all",
                })
            assert env[_A11Y_VAR] == "none", desktop

    def test_kde_and_unknown_linux_preserve_driver_policy(self):
        for desktop in ("KDE", "sway", "unknown"):
            with patch.object(cua_backend.sys, "platform", "linux"):
                inherited = cua_backend.cua_driver_child_env({
                    "XDG_CURRENT_DESKTOP": desktop, _A11Y_VAR: "all"
                })
                absent = cua_backend.cua_driver_child_env({
                    "XDG_CURRENT_DESKTOP": desktop
                })
            assert inherited[_A11Y_VAR] == "all", desktop
            assert _A11Y_VAR not in absent, desktop

    def test_desktop_identity_uses_order_and_skips_empty_values(self):
        assert cua_backend._desktop_identity_from({
            "XDG_CURRENT_DESKTOP": "",
            "XDG_SESSION_DESKTOP": "GNOME",
            "DESKTOP_SESSION": "fallback",
        }) == "GNOME"
        assert cua_backend._desktop_identity_from({
            "XDG_SESSION_DESKTOP": "",
            "DESKTOP_SESSION": "cinnamon",
        }) == "cinnamon"

    def test_missing_desktop_recovers_user_manager_identity(self):
        service_env = {"SYSTEMD_EXEC_PID": str(cua_backend.os.getpid())}
        with patch.dict(cua_backend.os.environ, service_env, clear=True), \
             patch.object(cua_backend.sys, "platform", "linux"), \
             patch.object(cua_backend, "_cua_telemetry_disabled", return_value=True), \
             patch.object(
                 cua_backend,
                 "_linux_desktop_from_user_manager",
                 return_value="ubuntu:GNOME",
             ):
            env = cua_backend.cua_driver_child_env()
        assert env["XDG_CURRENT_DESKTOP"] == "ubuntu:GNOME"
        assert env[_A11Y_VAR] == "is_enabled_only"

    def test_user_manager_probe_uses_minimal_fixed_bus_environment(self):
        result = cua_backend.subprocess.CompletedProcess(
            args=["systemctl"],
            returncode=0,
            stdout="PATH=/usr/bin\nXDG_CURRENT_DESKTOP=ubuntu:GNOME\n",
            stderr="",
        )
        inherited = {
            "XDG_RUNTIME_DIR": "/run/user/999",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/999/bus",
            "OPENAI_API_KEY": "must-not-leak",
        }
        with patch.dict(cua_backend.os.environ, inherited, clear=True), \
             patch.object(cua_backend.os, "getuid", return_value=1000, create=True), \
             patch.object(
                 cua_backend,
                 "_trusted_systemctl_path",
                 return_value="/usr/bin/systemctl",
             ), \
             patch.object(cua_backend.subprocess, "run", return_value=result) as run:
            desktop = cua_backend._probe_linux_desktop_from_user_manager()

        assert desktop == "ubuntu:GNOME"
        assert run.call_args.args[0] == [
            "/usr/bin/systemctl", "--user", "show-environment"
        ]
        probe_env = run.call_args.kwargs["env"]
        assert set(probe_env) == {
            "PATH", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"
        }
        assert probe_env["XDG_RUNTIME_DIR"] == "/run/user/1000"
        assert probe_env["DBUS_SESSION_BUS_ADDRESS"] == (
            "unix:path=/run/user/1000/bus"
        )
        assert "OPENAI_API_KEY" not in probe_env

    def test_user_manager_probe_failures_return_none(self):
        for error in (
            OSError("missing"),
            cua_backend.subprocess.TimeoutExpired("systemctl", 2),
        ):
            with patch.object(
                cua_backend, "_trusted_systemctl_path", return_value="/usr/bin/systemctl"
            ), patch.object(cua_backend.subprocess, "run", side_effect=error):
                assert cua_backend._probe_linux_desktop_from_user_manager() is None

        for result in (
            cua_backend.subprocess.CompletedProcess(
                args=["systemctl"], returncode=1, stdout="", stderr="failed"
            ),
            cua_backend.subprocess.CompletedProcess(
                args=["systemctl"], returncode=0, stdout="PATH=/usr/bin\n", stderr=""
            ),
        ):
            with patch.object(
                cua_backend, "_trusted_systemctl_path", return_value="/usr/bin/systemctl"
            ), patch.object(cua_backend.subprocess, "run", return_value=result):
                assert cua_backend._probe_linux_desktop_from_user_manager() is None

    def test_user_manager_desktop_probe_is_briefly_cached(self):
        original = cua_backend._cua_linux_desktop_cache
        cua_backend._cua_linux_desktop_cache = (0.0, None)
        try:
            with patch.object(
                cua_backend,
                "_probe_linux_desktop_from_user_manager",
                return_value="ubuntu:GNOME",
            ) as probe:
                assert cua_backend._linux_desktop_from_user_manager() == "ubuntu:GNOME"
                assert cua_backend._linux_desktop_from_user_manager() == "ubuntu:GNOME"
            probe.assert_called_once()
        finally:
            cua_backend._cua_linux_desktop_cache = original

    def test_child_of_systemd_service_does_not_probe_user_manager(self):
        inherited = {"SYSTEMD_EXEC_PID": str(cua_backend.os.getpid() - 1)}
        with patch.dict(cua_backend.os.environ, inherited, clear=True), \
             patch.object(cua_backend.sys, "platform", "linux"), \
             patch.object(cua_backend, "_cua_telemetry_disabled", return_value=True), \
             patch.object(cua_backend, "_linux_desktop_from_user_manager") as probe:
            env = cua_backend.cua_driver_child_env()
        probe.assert_not_called()
        assert _A11Y_VAR not in env

    def test_non_linux_preserves_inherited_a11y_advertisement(self):
        with patch.object(cua_backend.sys, "platform", "darwin"):
            env = cua_backend.cua_driver_child_env({_A11Y_VAR: "all"})
        assert env[_A11Y_VAR] == "all"


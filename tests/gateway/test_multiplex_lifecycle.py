"""Phase 4: lifecycle guard + per-profile observability."""
import pytest

from gateway.config import GatewayConfig
from gateway.restart import GATEWAY_FATAL_CONFIG_EXIT_CODE


class TestServedProfilesStatus:
    def test_write_and_read_served_profiles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import importlib
        import gateway.status as status
        importlib.reload(status)
        try:
            status.write_runtime_status(
                gateway_state="running", served_profiles=["default", "coder"]
            )
            rec = status.read_runtime_status()
            assert rec.get("served_profiles") == ["default", "coder"]
        finally:
            importlib.reload(status)


def test_cron_profile_homes_follow_allowlist(tmp_path, monkeypatch):
    """The helper wired into in-process cron returns only selected profiles."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    for name in ("worker", "guest"):
        (default_home / "profiles" / name).mkdir(parents=True)

    import gateway.run as gateway_run

    homes = gateway_run._multiplex_profile_homes(
        GatewayConfig(
            multiplex_profiles=True,
            multiplex_profile_allowlist=["worker"],
        )
    )

    assert [name for name, _home in homes] == ["default", "worker"]


def test_dedicated_gateway_cron_start_kwargs_bind_one_profile(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    from cron.scheduler_provider import InProcessCronScheduler

    brand_home = tmp_path / ".hermes" / "profiles" / "brand"
    runner = type(
        "Runner",
        (),
        {
            "config": GatewayConfig(multiplex_profiles=False),
            "adapters": {"slack": "brand-adapter"},
            "_profile_adapters": {},
            "_draining": False,
            "_external_drain_active": False,
            "_active_profile_name": lambda self: "brand",
        },
    )()
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: brand_home)

    kwargs = gateway_run._cron_scheduler_start_kwargs(
        runner,
        InProcessCronScheduler(),
        loop="loop",
    )

    assert kwargs["profile_homes"] == [("brand", brand_home)]
    assert kwargs["profile_adapters"] == {"brand": {"slack": "brand-adapter"}}
    assert kwargs["owner_kind"] == "gateway-dedicated"
    assert kwargs["runtime_id"].startswith("gateway:brand:")
    assert kwargs["loop"] == "loop"


def test_intentional_multiplex_gateway_keeps_configured_profile_ownership(
    tmp_path,
    monkeypatch,
):
    import gateway.run as gateway_run
    from cron.scheduler_provider import InProcessCronScheduler

    default_home = tmp_path / ".hermes"
    brand_home = default_home / "profiles" / "brand"
    homes = [("default", default_home), ("brand", brand_home)]
    runner = type(
        "Runner",
        (),
        {
            "config": GatewayConfig(multiplex_profiles=True),
            "adapters": {"slack": "aya-adapter"},
            "_profile_adapters": {"brand": {"slack": "brand-adapter"}},
            "_draining": False,
            "_external_drain_active": False,
            "_active_profile_name": lambda self: "default",
        },
    )()
    monkeypatch.setattr(gateway_run, "_multiplex_profile_homes", lambda _cfg: homes)

    kwargs = gateway_run._cron_scheduler_start_kwargs(
        runner,
        InProcessCronScheduler(),
        loop="loop",
    )

    assert kwargs["profile_homes"] == homes
    assert kwargs["profile_adapters"] == {
        "default": {"slack": "aya-adapter"},
        "brand": {"slack": "brand-adapter"},
    }
    assert kwargs["owner_kind"] == "gateway-multiplex"
    assert kwargs["runtime_id"].startswith("gateway:default:")


class TestNamedProfileMultiplexerGuard:
    """_guard_named_profile_under_multiplexer is inert unless all conditions hold."""


    def test_force_bypasses(self, monkeypatch):
        from hermes_cli import gateway as gw
        # Even if it looks like a named profile, force returns immediately.
        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        gw._guard_named_profile_under_multiplexer(force=True)

    def test_inert_when_no_default_gateway_running(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw
        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: tmp_path
        )
        # No gateway.pid in tmp_path => no running default gateway => no raise.
        gw._guard_named_profile_under_multiplexer(force=False)

    def _fake_running_default_gateway(self, monkeypatch, tmp_path):
        """Make the guard believe a live default gateway exists at tmp_path."""
        from hermes_cli import gateway as gw
        import gateway.status as status

        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: tmp_path
        )
        (tmp_path / "gateway.pid").write_text("12345", encoding="utf-8")
        monkeypatch.setattr(status, "_read_pid_record", lambda p: {"pid": 12345})
        monkeypatch.setattr(status, "_pid_from_record", lambda rec: 12345)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)

    def test_unset_allowlist_preserves_historical_guard(self, monkeypatch, tmp_path):
        self._fake_running_default_gateway(monkeypatch, tmp_path)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n",
            encoding="utf-8",
        )

        from hermes_cli import gateway as gw

        with pytest.raises(SystemExit) as excinfo:
            gw._guard_named_profile_under_multiplexer(force=False)
        assert excinfo.value.code == GATEWAY_FATAL_CONFIG_EXIT_CODE

    def test_served_profile_is_still_guarded(self, monkeypatch, tmp_path):
        self._fake_running_default_gateway(monkeypatch, tmp_path)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n"
            "  multiplex_profiles: true\n"
            "  multiplex_profile_allowlist:\n"
            "    - Coder\n",
            encoding="utf-8",
        )

        from hermes_cli import gateway as gw

        with pytest.raises(SystemExit) as excinfo:
            gw._guard_named_profile_under_multiplexer(force=False)
        assert excinfo.value.code == GATEWAY_FATAL_CONFIG_EXIT_CODE

    @pytest.mark.parametrize(
        "allowlist_yaml",
        ["[]", "[worker]", "coder"],
    )
    def test_unserved_profile_may_run_standalone(
        self, monkeypatch, tmp_path, allowlist_yaml
    ):
        self._fake_running_default_gateway(monkeypatch, tmp_path)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n"
            "  multiplex_profiles: true\n"
            f"  multiplex_profile_allowlist: {allowlist_yaml}\n",
            encoding="utf-8",
        )

        from hermes_cli import gateway as gw

        gw._guard_named_profile_under_multiplexer(force=False)



from types import SimpleNamespace

from hermes_cli.status import show_status


def test_show_status_all_does_not_print_tavily_key_value(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sentinel = "NONSECRET_SENTINEL_VALUE_DO_NOT_PRINT_123456"
    monkeypatch.setenv("TAVILY_API_KEY", sentinel)

    show_status(SimpleNamespace(all=True, deep=False))

    output = capsys.readouterr().out
    assert "Tavily" in output
    assert sentinel not in output


def test_show_status_termux_gateway_section_skips_systemctl(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod
    import hermes_cli.auth as auth_mod
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(auth_mod, "get_nous_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)

    def _unexpected_systemctl(*args, **kwargs):
        raise AssertionError("systemctl should not be called in the Termux status view")

    monkeypatch.setattr(status_mod.subprocess, "run", _unexpected_systemctl)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Manager:      Termux / manual process" in output
    assert "Start with:   hermes gateway" in output
    assert "systemd (user)" not in output


def test_show_status_reports_nous_auth_error(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod
    import hermes_cli.auth as auth_mod
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(
        auth_mod,
        "get_nous_auth_status",
        lambda: {
            "logged_in": False,
            "portal_base_url": "https://portal.nousresearch.com",
            "access_expires_at": "2026-04-20T01:00:51+00:00",
            "agent_key_expires_at": "2026-04-20T04:54:24+00:00",
            "has_refresh_token": True,
            "error": "Refresh session has been revoked",
        },
        raising=False,
    )
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_qwen_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Nous Portal   ✗ not logged in (run: hermes portal)" in output
    assert "Error:      Refresh session has been revoked" in output
    assert "Access exp:" in output
    assert "Key exp:" in output


def test_show_status_reports_nous_inference_key_without_portal_login(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod
    from hermes_cli.nous_account import NousPortalAccountInfo
    import hermes_cli.auth as auth_mod
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(
        auth_mod,
        "get_nous_auth_status",
        lambda: {
            "logged_in": False,
            "inference_credential_present": True,
            "credential_source": "pool:manual opaque key",
            "inference_base_url": "https://inference.example.com/v1",
            "agent_key_expires_at": "2099-01-01T00:00:00+00:00",
        },
        raising=False,
    )
    monkeypatch.setattr(
        status_mod,
        "get_nous_portal_account_info",
        lambda: NousPortalAccountInfo(
            logged_in=False,
            source="inference_key",
            fresh=False,
            inference_credential_present=True,
            inference_base_url="https://inference.example.com/v1",
        ),
        raising=False,
    )
    monkeypatch.setattr(status_mod, "managed_nous_tools_enabled", lambda: False, raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_qwen_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Nous Portal   ✗ not logged in (Nous inference key configured)" in output
    assert "Inference:  https://inference.example.com/v1" in output
    assert "Nous inference credentials are configured" in output


# ---------------------------------------------------------------------------
# Helpers shared by xAI OAuth status tests
# ---------------------------------------------------------------------------

def _base_xai_mocks(monkeypatch, tmp_path):
    """Set up the minimal environment for show_status, returning status_mod."""
    from hermes_cli import status as status_mod
    import hermes_cli.auth as auth_mod
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(auth_mod, "get_nous_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_qwen_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_minimax_oauth_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)
    return status_mod


class TestShowStatusXaiOAuth:
    """xAI OAuth row in hermes status."""

    # ------------------------------------------------------------------
    # Logged-in branch
    # ------------------------------------------------------------------

    def test_logged_in_shows_check_mark_and_label(self, monkeypatch, capsys, tmp_path):
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True, "auth_store": "/a/auth.json"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "xAI OAuth" in out
        # The logged-in label must appear; the "not logged in" label must not
        assert "✓" in out or "logged in" in out
        assert "not logged in" not in out.split("xAI OAuth", 1)[1].split("\n")[0]

    def test_logged_in_shows_auth_store(self, monkeypatch, capsys, tmp_path):
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True, "auth_store": "/home/u/.hermes/auth.json"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Auth file:  /home/u/.hermes/auth.json" in out

    def test_logged_in_shows_last_refresh(self, monkeypatch, capsys, tmp_path):
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {
                                "logged_in": True,
                                "auth_store": "/a/auth.json",
                                "last_refresh": "2026-05-17T10:00:00+00:00",
                            },
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Refreshed:" in out

    def test_logged_in_does_not_show_error_line(self, monkeypatch, capsys, tmp_path):
        """Error field must be suppressed when logged_in is True."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {
                                "logged_in": True,
                                "auth_store": "/a/auth.json",
                                "error": "stale-error-must-not-appear",
                            },
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1]
        assert "stale-error-must-not-appear" not in xai_section

    def test_no_auth_store_line_when_field_absent(self, monkeypatch, capsys, tmp_path):
        """Auth file line must not appear when auth_store is missing."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1].split("◆", 1)[0]
        assert "Auth file:" not in xai_section

    def test_no_refreshed_line_when_last_refresh_absent(self, monkeypatch, capsys, tmp_path):
        """Refreshed line must not appear when last_refresh is not present."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True, "auth_store": "/a/auth.json"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1].split("◆", 1)[0]
        assert "Refreshed:" not in xai_section

    # ------------------------------------------------------------------
    # Not-logged-in branch
    # ------------------------------------------------------------------

    def test_not_logged_in_shows_login_command(self, monkeypatch, capsys, tmp_path):
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": False, "error": "no credentials"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "not logged in (run: hermes auth add xai-oauth)" in out

    def test_not_logged_in_shows_error(self, monkeypatch, capsys, tmp_path):
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": False, "error": "Token has expired"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Error:      Token has expired" in out

    def test_not_logged_in_omits_error_line_when_error_absent(self, monkeypatch, capsys, tmp_path):
        """No Error: line when not logged in but error key is missing."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": False},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1].split("◆", 1)[0]
        assert "Error:" not in xai_section

    # ------------------------------------------------------------------
    # Resilience: import failure and runtime exception
    # ------------------------------------------------------------------

    def test_import_failure_does_not_crash_show_status(self, monkeypatch, capsys, tmp_path):
        """show_status must complete even when get_xai_oauth_auth_status cannot be imported."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.delattr(auth_mod, "get_xai_oauth_auth_status", raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "◆ Auth Providers" in out

    def test_import_failure_does_not_break_other_oauth_providers(self, monkeypatch, capsys, tmp_path):
        """Nous/Codex/MiniMax rows must still appear when xAI import fails."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_nous_auth_status",
                            lambda: {"logged_in": True}, raising=False)
        monkeypatch.delattr(auth_mod, "get_xai_oauth_auth_status", raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Nous Portal" in out
        assert "MiniMax OAuth" in out

    def test_status_function_exception_does_not_crash(self, monkeypatch, capsys, tmp_path):
        """show_status must not propagate an exception raised by get_xai_oauth_auth_status."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)

        def _raises():
            raise RuntimeError("backend unreachable")

        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status", _raises, raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "◆ Auth Providers" in out

    def test_status_function_returns_none_does_not_crash(self, monkeypatch, capsys, tmp_path):
        """get_xai_oauth_auth_status returning None must be handled gracefully."""
        import hermes_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: None, raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "xAI OAuth" in out
        assert "not logged in (run: hermes auth add xai-oauth)" in out


class TestPluginPlatformDepsProbe:
    """Read-only "is this platform available?" callers must use the CHEAP probe.

    For adapter plugins that defer a heavy SDK, ``check_fn`` is also the
    *loader* — calling it imports the SDK. Feishu's
    ``check_feishu_requirements()`` imports ``lark_oapi``, whose top-level
    ``__init__`` eagerly pulls the whole ~10k-module package: measured on this
    box at **238.2s and 404.9s on two consecutive runs** (the second warm, so
    this is steady state, not a cold-compile artifact). Every submodule after
    it is free, which is why the cost is invisible in a profile of the
    submodule imports.

    ``PlatformEntry.deps_available_fn`` exists for exactly this and answers in
    ~0ms via ``PathFinder.find_spec``. ``gateway/config.py::_apply_env_overrides``
    was already converted; the display/probe callers in status, gateway and
    web_server were missed, so ``hermes status`` paid a multi-minute SDK import
    to print one line. It also made
    ``test_jobs_json_utf8_bom.py::test_status_scheduled_jobs_accepts_utf8_bom``
    blow any per-test timeout, and because the suite runs
    ``--timeout-method=thread`` that ``os._exit``s the WHOLE pytest process —
    a single slow import aborting an entire tests/hermes_cli sweep.

    The loader path (``platform_registry.py``'s adapter construction) must keep
    using ``check_fn``: there, loading is the point.
    """

    def _entry(self, calls):
        from gateway.platform_registry import PlatformEntry

        def _expensive():
            calls.append("check_fn")
            return True

        def _cheap():
            calls.append("deps_available_fn")
            return True

        return PlatformEntry(
            name="feishu-probe",
            label="FeishuProbe",
            adapter_factory=lambda cfg: None,
            check_fn=_expensive,
            deps_available_fn=_cheap,
        )

    def test_show_status_prefers_the_cheap_probe(self, monkeypatch, capsys):
        from hermes_cli import status as status_mod
        from gateway.platform_registry import platform_registry

        calls = []
        monkeypatch.setattr(
            platform_registry, "plugin_entries",
            lambda: [self._entry(calls)], raising=False,
        )
        try:
            status_mod.show_status(SimpleNamespace(all=False, deep=False))
        except Exception:
            pass  # unrelated sections may fail in a bare env; the probe is the contract
        capsys.readouterr()

        assert "deps_available_fn" in calls, (
            "show_status never consulted deps_available_fn — a plugin whose "
            "check_fn is also its SDK loader makes `hermes status` pay a "
            "multi-minute import to print one line"
        )
        assert "check_fn" not in calls, (
            f"show_status called the expensive loader anyway: {calls}"
        )

    def test_falls_back_to_check_fn_when_no_cheap_probe(self, monkeypatch, capsys):
        """Plugins without a cheap probe must keep working unchanged."""
        from hermes_cli import status as status_mod
        from gateway.platform_registry import platform_registry

        calls = []
        entry = self._entry(calls)
        object.__setattr__(entry, "deps_available_fn", None)
        monkeypatch.setattr(
            platform_registry, "plugin_entries", lambda: [entry], raising=False,
        )
        try:
            status_mod.show_status(SimpleNamespace(all=False, deep=False))
        except Exception:
            pass
        capsys.readouterr()

        assert "check_fn" in calls, (
            "no deps_available_fn supplied, so check_fn must still be used"
        )

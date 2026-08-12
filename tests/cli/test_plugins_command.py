from types import SimpleNamespace

from hermes_cli.plugin_activation import PluginActivationState


def _make_cli():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    return cli


def _without_loaded_plugins(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: SimpleNamespace(list_plugins=lambda: []),
    )


def test_plugins_quick_view_uses_resolved_group_deny(monkeypatch, capsys):
    from hermes_cli import plugins_cmd

    candidates = [
        ("legacy-copy", "1.0", "Bundled", "bundled", None, "shared", "backend"),
        ("new-copy", "2.0", "User", "user", None, "shared", "standalone"),
    ]
    activation = PluginActivationState(
        enabled=frozenset({"new-copy"}),
        disabled=frozenset({"legacy-copy"}),
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_plugin_activation_state",
        lambda: activation,
    )
    _without_loaded_plugins(monkeypatch)

    assert _make_cli().process_command("/plugins") is True

    out = capsys.readouterr().out
    assert "new-copy v2.0 [disabled]" in out


def test_plugins_quick_view_does_not_enable_inactive_user_shadow(
    monkeypatch,
    capsys,
):
    from hermes_cli import plugins_cmd

    candidates = [
        ("bundled", "1.0", "Bundled", "bundled", None, "shared", "backend"),
        ("user-shadow", "2.0", "User", "user", None, "shared", "standalone"),
    ]
    monkeypatch.setattr(
        plugins_cmd,
        "_discover_plugin_candidates",
        lambda: candidates,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_plugin_activation_state",
        PluginActivationState,
    )
    _without_loaded_plugins(monkeypatch)

    assert _make_cli().process_command("/plugins") is True

    out = capsys.readouterr().out
    assert "user-shadow v2.0 [not enabled]" in out

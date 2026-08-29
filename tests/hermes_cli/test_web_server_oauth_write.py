from hermes_cli.web_server import _save_anthropic_oauth_creds


def test_dashboard_anthropic_oauth_owns_each_login_in_pool(_isolate_hermes_home, monkeypatch, tmp_path):
    """Dashboard PKCE must not duplicate its newest login via hermes_pkce."""
    from agent.credential_pool import load_pool

    oauth_file = tmp_path / ".anthropic_oauth.json"
    monkeypatch.setattr("agent.anthropic_adapter._get_hermes_oauth_file", lambda: oauth_file)

    _save_anthropic_oauth_creds("access-a", "refresh-a", 123456, "Personal")
    _save_anthropic_oauth_creds("access-b", "refresh-b", 123457, "Work")

    assert not oauth_file.exists()
    entries = [
        entry
        for entry in load_pool("anthropic").entries()
        if entry.source == "manual:dashboard_pkce"
    ]
    assert [(entry.label, entry.access_token, entry.refresh_token) for entry in entries] == [
        ("Personal", "access-a", "refresh-a"),
        ("Work", "access-b", "refresh-b"),
    ]

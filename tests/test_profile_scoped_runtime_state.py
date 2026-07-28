def test_skills_directory_follows_active_profile(monkeypatch, tmp_path):
    import tools.skills_tool as skills_tool

    first = tmp_path / "profile-a"
    second = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert skills_tool._skills_dir() == first / "skills"
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert skills_tool._skills_dir() == second / "skills"


def test_plugin_managers_are_cached_per_profile(monkeypatch, tmp_path):
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "_plugin_manager", None)
    plugins._plugin_managers.clear()
    first = tmp_path / "profile-a"
    second = tmp_path / "profile-b"

    monkeypatch.setenv("HERMES_HOME", str(first))
    first_manager = plugins.get_plugin_manager()
    monkeypatch.setenv("HERMES_HOME", str(second))
    second_manager = plugins.get_plugin_manager()
    monkeypatch.setenv("HERMES_HOME", str(first))

    assert first_manager is not second_manager
    assert plugins.get_plugin_manager() is first_manager


def test_file_tool_config_state_is_profile_scoped(monkeypatch, tmp_path):
    import hermes_cli.config as config
    import tools.file_tools as file_tools

    first = tmp_path / "profile-a" / "config.yaml"
    second = tmp_path / "profile-b" / "config.yaml"
    current = {"path": first, "limit": 111}
    monkeypatch.setattr(config, "get_config_path", lambda: current["path"])
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"file_read_max_chars": current["limit"]},
    )
    monkeypatch.setattr(file_tools, "_max_read_chars_cached", None)
    file_tools._max_read_chars_by_config.clear()

    assert file_tools._get_hermes_config_resolved() == str(first.resolve())
    assert file_tools._get_max_read_chars() == 111

    current.update(path=second, limit=222)
    assert file_tools._get_hermes_config_resolved() == str(second.resolve())
    assert file_tools._get_max_read_chars() == 222

    current.update(path=first, limit=999)
    assert file_tools._get_max_read_chars() == 111

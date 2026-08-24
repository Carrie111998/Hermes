from hermes_cli.windows_ssh_runtime import dispatch


def test_list_profiles_operation_uses_canonical_profile_inventory(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles" / "deals").mkdir(parents=True)
    (root / "profiles" / ".ignored").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))

    assert dispatch(["list-profiles"]) == ["default", "deals"]

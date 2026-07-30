from hermes_cli.main import _print_runtime_capabilities


def test_version_probe_reports_provider_auth_home_contract(capsys):
    _print_runtime_capabilities()

    assert capsys.readouterr().out == (
        "Runtime capabilities: provider-auth-home-v1\n"
    )

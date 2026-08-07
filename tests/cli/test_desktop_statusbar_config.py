from hermes_cli.config import DEFAULT_CONFIG


def test_desktop_statusbar_defaults_off_for_a_quiet_workspace():
    assert DEFAULT_CONFIG["display"]["desktop_statusbar"] == "off"

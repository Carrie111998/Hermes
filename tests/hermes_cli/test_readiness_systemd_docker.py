from unittest.mock import patch

from hermes_cli.readiness import (
    _collect_docker_information,
    _collect_systemd_information,
)


@patch("subprocess.run")
def test_systemd_information(mock_run):
    mock_run.return_value.stdout = """\
Id=agakoc-hermes-api.service
ActiveState=active
SubState=running
ExecMainStatus=0
NRestarts=0
UnitFileState=enabled
"""

    result = _collect_systemd_information()

    assert result["healthy"] is True
    assert len(result["services"]) >= 1


@patch("subprocess.run")
def test_docker_information(mock_run):
    mock_run.return_value.stdout = (
        '{"Names":"agakoc-postgres","State":"running"}\n'
        '{"Names":"agakoc-qdrant","State":"running"}\n'
    )

    result = _collect_docker_information()

    assert result["running"] == 2
    assert len(result["containers"]) == 2

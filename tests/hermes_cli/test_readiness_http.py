from pathlib import Path
from unittest.mock import Mock, patch

from hermes_cli.readiness import _collect_http_health_information


@patch("hermes_cli.readiness.requests.get")
def test_http_health_ok(mock_get):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "data": {
            "service": "hermes-api",
        },
    }

    mock_get.return_value = response

    result = _collect_http_health_information(Path("."))

    assert result["configured"] is True
    assert result["http_ok"] is True
    assert result["health_ok"] is True
    assert result["service"] == "hermes-api"

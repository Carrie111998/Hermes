"""Regression tests for the Hermes Docker compose dashboard contract."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def test_gateway_service_enables_dashboard_on_loopback() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "container_name: hermes" in text
    assert "restart: unless-stopped" in text
    assert "network_mode: host" in text
    assert "volumes:\n      - ~/.hermes:/opt/data" in text
    assert 'command: ["gateway", "run"]' in text
    assert "HERMES_DASHBOARD=true" in text
    assert "HERMES_DASHBOARD_HOST=127.0.0.1" in text
    assert "HERMES_DASHBOARD_PORT=9119" in text
    assert "HERMES_DASHBOARD_HOST=0.0.0.0" not in text

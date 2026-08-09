from fastapi.testclient import TestClient

from hermes_cli.web_server import app


def test_api_and_service_worker_responses_are_not_cacheable():
    with TestClient(app) as client:
        api_response = client.get("/api/status")
        assert api_response.status_code == 200
        assert "no-store" in api_response.headers["cache-control"]
        assert api_response.headers["pragma"] == "no-cache"

        service_worker = client.get("/sw.js")
        assert service_worker.status_code == 200
        assert "no-cache" in service_worker.headers["cache-control"]

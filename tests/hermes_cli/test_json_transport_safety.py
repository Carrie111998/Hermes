import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.json_safety import dumps_utf8_safe
from hermes_cli.web_server import UTF8SafeJSONResponse


def test_dumps_utf8_safe_repairs_keys_and_values_without_mutating_input():
    payload = {"key\udfff": ["value\ud800", "中文"]}

    serialized = dumps_utf8_safe(payload, ensure_ascii=False)

    serialized.encode("utf-8")
    assert json.loads(serialized) == {"key�": ["value�", "中文"]}
    assert next(iter(payload)) == "key\udfff"


def test_dumps_utf8_safe_leaves_valid_unicode_serialization_unchanged():
    payload = {"message": "Hello, 世界 👋"}

    assert dumps_utf8_safe(payload, ensure_ascii=False) == json.dumps(
        payload, ensure_ascii=False
    )


def test_fastapi_default_response_survives_lone_surrogate():
    app = FastAPI(default_response_class=UTF8SafeJSONResponse)

    @app.get("/payload")
    def payload():
        return {"message": "Hello \ud83d 世界"}

    response = TestClient(app).get("/payload")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello � 世界"}
    assert "世界".encode() in response.content

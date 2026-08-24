import json

import pytest

from hermes_wisdom.client import WisdomClient, WisdomNotFound, WisdomValidationError


class Response:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""

    def json(self):
        return self._body


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def client(response):
    value = WisdomClient.__new__(WisdomClient)
    value.base = "https://gateway.example"
    value.timeout = 7
    value.session = Session(response)
    return value


def test_submit_body_has_no_local_candidate_or_activity_signals():
    body = {
        "draft": {
            "id": "d1",
            "orgId": "o1",
            "ownerUserId": "u1",
            "slug": "my-skill",
            "draftCommit": "sha256:" + "a" * 64,
            "contentHash": "sha256:" + "b" * 64,
            "authorDescription": "Does a task.",
            "authorDescriptionHash": "sha256:" + "c" * 64,
            "state": "ready",
            "packageManifestHash": "sha256:" + "d" * 64,
            "packageManifestSchemaVersion": 1,
            "systemSpec": None,
            "scan": None,
            "scanVerdict": "pass",
            "explanation": None,
            "updatedAt": "now",
        }
    }
    value = client(Response(201, body))
    value.submit_draft(
        slug="my-skill",
        commit="sha256:" + "a" * 64,
        content_hash="sha256:" + "b" * 64,
        description="Does a task.",
    )
    payload = value.session.calls[0][2]["json"]
    assert payload == {
        "slug": "my-skill",
        "draft_commit": "sha256:" + "a" * 64,
        "content_hash": "sha256:" + "b" * 64,
        "author_description": "Does a task.",
    }
    assert not (
        {"usage", "refinement", "candidate", "ranking", "stability", "dismissal"}
        & payload.keys()
    )


def test_not_found_is_opaque():
    value = client(Response(404, {"error": "not_found"}))
    with pytest.raises(WisdomNotFound, match="item not found"):
        value._request("GET", "skills/secret")


def test_approve_exact_three_hash_body():
    value = client(Response(200, {"draft": {"id": "invalid"}}))
    with pytest.raises(Exception):
        value.approve("d1", content_hash="c", description_hash="d", manifest_hash="m")
    payload = value.session.calls[0][2]["json"]
    assert payload == {
        "content_hash": "c",
        "author_description_hash": "d",
        "package_manifest_hash": "m",
    }

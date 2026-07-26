from sigil.desktop_bridge.runner import backend_status, handle_request


def proposal_request() -> dict[str, object]:
    return {
        "command": "explain_proposal",
        "payload": {
            "proposal_id": "PRP-20260725-0042",
            "symbol": "MSFT",
            "side": "BUY",
            "estimated_notional": 25.0,
            "strategy": "Quality momentum v2",
            "evidence_references": [
                {
                    "id": "EV-0042",
                    "label": "Proposal evidence",
                    "source": "sigil",
                }
            ],
        },
    }


def test_backend_status_is_read_only_and_paper_only() -> None:
    status = backend_status()

    assert status["status"] == "ok"
    assert status["mode"] == "local-read-only"
    assert status["environment"] == "paper"
    assert status["simulation"] is True
    assert status["execution_authorized"] is False
    assert status["broker_submission_available"] is False
    assert status["supported_commands"] == ["health", "explain_proposal"]


def test_health_request_returns_status() -> None:
    response = handle_request({"command": "health"})

    assert response["ok"] is True
    assert response["result"]["mode"] == "local-read-only"


def test_explain_proposal_returns_governed_result() -> None:
    response = handle_request(proposal_request())

    assert response["ok"] is True

    result = response["result"]
    assert result["kind"] == "proposal-explanation"
    assert result["model_route"] == "python-bridge-v1"
    assert result["source"] == "local"
    assert result["execution_authorized"] is False
    assert result["broker_submission_available"] is False
    assert result["evidence_references"][0]["id"] == "EV-0042"
    assert "$25.00" in result["explanation"]


def test_explain_proposal_rejects_invalid_side() -> None:
    request = proposal_request()
    payload = request["payload"]
    assert isinstance(payload, dict)
    payload["side"] = "SHORT"

    response = handle_request(request)

    assert response == {
        "ok": False,
        "error": "invalid_payload",
        "message": "side must be BUY or SELL.",
    }


def test_explain_proposal_requires_evidence_list() -> None:
    request = proposal_request()
    payload = request["payload"]
    assert isinstance(payload, dict)
    payload["evidence_references"] = "EV-0042"

    response = handle_request(request)

    assert response["ok"] is False
    assert response["error"] == "invalid_payload"


def test_unknown_command_fails_closed() -> None:
    response = handle_request({"command": "execute"})

    assert response == {
        "ok": False,
        "error": "unsupported_command",
        "message": "Only allow-listed read-only commands are available.",
    }


def test_non_object_request_fails_closed() -> None:
    response = handle_request(["health"])

    assert response["ok"] is False
    assert response["error"] == "invalid_request"

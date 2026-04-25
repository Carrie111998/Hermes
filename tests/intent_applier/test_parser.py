import json
from pathlib import Path

import pytest

from intent_applier.parser import (
    IntentMessage,
    IntentParseError,
    parse_intent_file,
)


def write_intent(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.fixture
def valid_intent_dict():
    return {
        "message_id": "msg-1",
        "idempotency_key": "tracker-intent:approval_intent:linkedin-1:approved",
        "protocol_version": "2.0-draft",
        "type": "APPROVAL_INTENT",
        "from": "main",
        "to": "tracker",
        "job_id": "linkedin-1",
        "timestamp": "2026-04-25T10:00:00Z",
        "correlation_id": "corr-1",
        "attempt": 1,
        "max_attempts": 3,
        "lease_timeout_seconds": 300,
        "reply_expected": False,
        "intent_only": True,
        "payload": {
            "requested_stage": "approved",
            "actor_id": "diego",
            "source": "legacy_dashboard",
            "notes": "VP role matches",
            "metadata": {"original_source": "legacy_dashboard"},
        },
    }


class TestParseHappyPath:
    def test_parses_valid_approval_intent(self, tmp_path, valid_intent_dict):
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        msg = parse_intent_file(path)
        assert isinstance(msg, IntentMessage)
        assert msg.message_id == "msg-1"
        assert msg.idempotency_key == "tracker-intent:approval_intent:linkedin-1:approved"
        assert msg.intent_type == "APPROVAL_INTENT"
        assert msg.job_id == "linkedin-1"
        assert msg.requested_stage == "approved"
        assert msg.actor_id == "diego"
        assert msg.source == "legacy_dashboard"
        assert msg.notes == "VP role matches"
        assert msg.metadata == {"original_source": "legacy_dashboard"}
        assert msg.path == path

    def test_parses_state_transition_intent(self, tmp_path, valid_intent_dict):
        valid_intent_dict["type"] = "STATE_TRANSITION_INTENT"
        valid_intent_dict["payload"]["requested_stage"] = "review"
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        msg = parse_intent_file(path)
        assert msg.intent_type == "STATE_TRANSITION_INTENT"
        assert msg.requested_stage == "review"

    def test_metadata_defaults_to_empty_dict_when_missing(self, tmp_path, valid_intent_dict):
        del valid_intent_dict["payload"]["metadata"]
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        msg = parse_intent_file(path)
        assert msg.metadata == {}

    def test_notes_defaults_to_empty_string_when_null(self, tmp_path, valid_intent_dict):
        valid_intent_dict["payload"]["notes"] = None
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        msg = parse_intent_file(path)
        assert msg.notes == ""


class TestParseErrors:
    def test_missing_message_id_raises(self, tmp_path, valid_intent_dict):
        del valid_intent_dict["message_id"]
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        with pytest.raises(IntentParseError, match="message_id"):
            parse_intent_file(path)

    def test_missing_idempotency_key_raises(self, tmp_path, valid_intent_dict):
        del valid_intent_dict["idempotency_key"]
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        with pytest.raises(IntentParseError, match="idempotency_key"):
            parse_intent_file(path)

    def test_unknown_intent_type_raises(self, tmp_path, valid_intent_dict):
        valid_intent_dict["type"] = "SOMETHING_ELSE"
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        with pytest.raises(IntentParseError, match="intent_type"):
            parse_intent_file(path)

    def test_missing_payload_raises(self, tmp_path, valid_intent_dict):
        del valid_intent_dict["payload"]
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        with pytest.raises(IntentParseError, match="payload"):
            parse_intent_file(path)

    def test_missing_requested_stage_raises(self, tmp_path, valid_intent_dict):
        del valid_intent_dict["payload"]["requested_stage"]
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        with pytest.raises(IntentParseError, match="requested_stage"):
            parse_intent_file(path)

    def test_missing_job_id_raises(self, tmp_path, valid_intent_dict):
        del valid_intent_dict["job_id"]
        path = write_intent(tmp_path, "intent.json", valid_intent_dict)
        with pytest.raises(IntentParseError, match="job_id"):
            parse_intent_file(path)

    def test_malformed_json_raises(self, tmp_path):
        p = tmp_path / "intent.json"
        p.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(IntentParseError, match="JSON"):
            parse_intent_file(p)

    def test_top_level_non_dict_raises(self, tmp_path):
        for value, name in [("42", "int"), ("null", "NoneType"), ("true", "bool"), ("3.14", "float")]:
            p = tmp_path / f"intent_{name}.json"
            p.write_text(value, encoding="utf-8")
            with pytest.raises(IntentParseError, match="must be a dict"):
                parse_intent_file(p)

import io
import json
import sys
from types import SimpleNamespace

from cron.scripts import classify_items


def test_main_sends_classifier_contract_as_system_prompt(monkeypatch, capsys):
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(
            content='[{"index": 0, "score": 9, "reason": "production incident"}]'
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                [
                    {
                        "id": "incident-1",
                        "subject": "Production unavailable",
                        "summary": "HTTP 500; respond today",
                    }
                ]
            )
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "classify_items.py",
            "--criteria",
            "Surface production incidents",
            "--format",
            "json",
        ],
    )

    assert classify_items.main() == 0
    assert captured["task"] == "monitor"
    assert captured["messages"][0] == {
        "role": "system",
        "content": classify_items._CLASSIFY_INSTRUCTIONS,
    }
    assert captured["messages"][1]["role"] == "user"
    assert "Production unavailable" in captured["messages"][1]["content"]

    output = json.loads(capsys.readouterr().out)
    assert output[0]["id"] == "incident-1"
    assert output[0]["score"] == 9

import json


def test_record_served_model_writes_per_job_artifact(tmp_path, monkeypatch):
    from cron.served_model_observability import record_served_model

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert record_served_model(
        "ebc4cde7b260",
        requested_model="openai/gpt-5.6-luna",
        served_model="openai/gpt-5.6-luna",
        observed_at="2026-08-23T20:02:37Z",
    ) is True
    path = tmp_path / "cron" / "output" / "ebc4cde7b260" / "served-model.jsonl"
    assert json.loads(path.read_text()) == {
        "job_id": "ebc4cde7b260",
        "observed_at": "2026-08-23T20:02:37Z",
        "output_file": None,
        "requested_model": "openai/gpt-5.6-luna",
        "served_model": "openai/gpt-5.6-luna",
    }


def test_record_served_model_is_fail_open_without_models(tmp_path, monkeypatch):
    from cron.served_model_observability import record_served_model

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert record_served_model("job", requested_model=None, served_model=None) is False
    assert not list(tmp_path.rglob("served-model.jsonl"))

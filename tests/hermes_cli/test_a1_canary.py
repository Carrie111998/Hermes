import json

from hermes_cli.a1_canary import GREEN_CANARY_CASE_IDS, main, run_green_canary_harness


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_green_canary_harness_writes_reusable_digest_only_summary(tmp_path):
    output = tmp_path / "a1-green-summary.jsonl"

    results = run_green_canary_harness(output_path=output)

    assert [result["case_id"] for result in results] == list(GREEN_CANARY_CASE_IDS)
    records = _read_jsonl(output)
    assert records == results
    assert all(record["raw_payload_stored"] is False for record in records)
    assert "do not leave local" not in output.read_text()
    assert "streaming hello" not in output.read_text()
    assert "fallback hello" not in output.read_text()


def test_green_canary_harness_proves_denial_streaming_and_fallback_paths(tmp_path):
    output = tmp_path / "a1-green-summary.jsonl"

    records = run_green_canary_harness(output_path=output)
    by_id = {record["case_id"]: record for record in records}

    denied = by_id["A1.3-NEG-001"]
    assert denied["provider_call_count"] == 0
    assert denied["dispatch_attempted"] == [False]
    assert "a1.c2.frontier-deny" in denied["rule_ids"]

    streaming = by_id["A1.3-CANARY-005"]
    assert streaming["surface"] == "streaming"
    assert streaming["provider_call_count"] == 1
    assert streaming["event_types"] == ["resolver_decision", "payload_capture", "dispatch_result"]
    assert streaming["dispatch_completed"] == [True]

    fallback = by_id["A1.3-CANARY-004"]
    assert fallback["provider_call_count"] == 2
    assert fallback["event_types"] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert fallback["resolver_providers"] == [
        "custom:headroom-openrouter-litellm",
        "local-ollama",
    ]
    assert fallback["resolver_hosts"] == ["localhost:8787", "localhost:11434"]


def test_green_canary_cli_returns_nonzero_when_output_missing(tmp_path, capsys):
    output = tmp_path / "cli-summary.jsonl"

    exit_code = main(["--output", str(output)])

    assert exit_code == 0
    records = _read_jsonl(output)
    assert len(records) == 4
    stdout = capsys.readouterr().out
    assert "A1.3 GREEN canaries passed" in stdout
    assert str(output) in stdout

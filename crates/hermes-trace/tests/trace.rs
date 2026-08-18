use hermes_trace::{project_reader, snapshot_reader, write_project_file, EventWriter, TraceEvent};
use serde_json::json;
use std::io::Cursor;
use tempfile::NamedTempFile;

fn event(seq: u64, event_type: &str, data: serde_json::Value) -> TraceEvent {
    TraceEvent {
        schema_version: 1,
        seq,
        time: 1_700_000_000.0 + seq as f64,
        session_id: "session-1".into(),
        event_type: event_type.into(),
        turn: Some(1),
        step: Some(1),
        data,
    }
}

#[test]
fn writer_rejects_non_monotonic_sequence_numbers() {
    let file = NamedTempFile::new().expect("temp trace");
    let mut writer = EventWriter::open(file.path()).expect("open writer");

    writer
        .append(&event(2, "turn/start", json!({})))
        .expect("first event");
    let error = writer
        .append(&event(2, "step/start", json!({})))
        .expect_err("duplicate sequence must fail");

    assert!(error.to_string().contains("monotonic"));
}

#[test]
fn projection_streams_turn_step_tool_and_usage_summary() {
    let input = [
        event(1, "turn/start", json!({})),
        event(2, "step/start", json!({})),
        event(
            3,
            "request/header",
            json!({"provider":"local","model":"gemma","input_tokens":120}),
        ),
        event(
            4,
            "tool/call",
            json!({"call_id":"call-1","name":"read_file","arguments":{"path":"README.md"}}),
        ),
        event(
            5,
            "tool/result",
            json!({"call_id":"call-1","is_error":false,"duration_ms":8,"preview":"ok"}),
        ),
        event(
            6,
            "assistant/message",
            json!({"output_tokens":24,"text":"Done"}),
        ),
        event(7, "step/end", json!({})),
        event(8, "turn/end", json!({})),
    ];
    let jsonl = input
        .iter()
        .map(|value| serde_json::to_string(value).expect("serialize"))
        .collect::<Vec<_>>()
        .join("\n");

    let projection = project_reader(Cursor::new(jsonl)).expect("project trace");

    assert_eq!(projection.summary.turns, 1);
    assert_eq!(projection.summary.steps, 1);
    assert_eq!(projection.summary.tool_calls, 1);
    assert_eq!(projection.summary.input_tokens, 120);
    assert_eq!(projection.summary.output_tokens, 24);
    assert_eq!(projection.records.len(), 8);
}

#[test]
fn verifier_rejects_tool_result_without_call_id() {
    let value = event(1, "tool/result", json!({"preview":"missing id"}));

    let error = value.validate().expect_err("tool result needs call id");

    assert!(error.to_string().contains("call_id"));
}

#[test]
fn normalized_snapshot_ignores_wall_clock_time() {
    let first = serde_json::to_string(&event(1, "turn/start", json!({"message":"hi"})))
        .expect("serialize first");
    let mut second_event = event(1, "turn/start", json!({"message":"hi"}));
    second_event.time = 1_900_000_000.0;
    let second = serde_json::to_string(&second_event).expect("serialize second");

    let first_snapshot = snapshot_reader(Cursor::new(first)).expect("first snapshot");
    let second_snapshot = snapshot_reader(Cursor::new(second)).expect("second snapshot");

    assert_eq!(first_snapshot, second_snapshot);
}

#[test]
fn streaming_file_projection_matches_the_projection_contract() {
    let file = NamedTempFile::new().expect("temp trace");
    let input = [
        event(1, "turn/start", json!({})),
        event(
            2,
            "assistant/message",
            json!({"output_tokens": 4, "content": "done"}),
        ),
        event(3, "turn/end", json!({"completed": true})),
    ];
    std::fs::write(
        file.path(),
        input
            .iter()
            .map(|value| serde_json::to_string(value).expect("serialize"))
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .expect("write trace");

    let mut output = Vec::new();
    write_project_file(file.path(), &mut output, false).expect("stream projection");
    let value: serde_json::Value = serde_json::from_slice(&output).expect("projection JSON");

    assert_eq!(value["summary"]["turns"], 1);
    assert_eq!(value["summary"]["output_tokens"], 4);
    assert_eq!(value["records"].as_array().map(Vec::len), Some(3));
    assert_eq!(value["records"][0]["time"], 1_700_000_001.0);
}

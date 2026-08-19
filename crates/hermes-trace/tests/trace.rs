use hermes_trace::{
    project_reader, snapshot_reader, verify_reader, write_project_file, EventWriter, TraceEvent,
};
use serde_json::json;
use std::io::Cursor;
use std::process::Command;
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

#[test]
fn verify_reader_returns_summary_without_projection_records() {
    let input = [
        event(1, "turn/start", json!({"input_tokens": 3})),
        event(2, "step/start", json!({})),
        event(3, "tool/call", json!({"call_id":"call-1"})),
        event(
            4,
            "tool/result",
            json!({"call_id":"call-1","is_error":true}),
        ),
        event(5, "assistant/message", json!({"output_tokens": 7})),
    ];
    let jsonl = input
        .iter()
        .map(|value| serde_json::to_string(value).expect("serialize"))
        .collect::<Vec<_>>()
        .join("\n");

    let summary = verify_reader(Cursor::new(jsonl)).expect("verify trace");

    assert_eq!(summary.turns, 1);
    assert_eq!(summary.steps, 1);
    assert_eq!(summary.tool_calls, 1);
    assert_eq!(summary.tool_errors, 1);
    assert_eq!(summary.input_tokens, 3);
    assert_eq!(summary.output_tokens, 7);
}

#[test]
fn cli_verify_accepts_valid_trace() {
    let file = NamedTempFile::new().expect("temp trace");
    let input = [
        event(1, "turn/start", json!({"input_tokens": 3})),
        event(2, "tool/call", json!({"call_id":"call-1"})),
        event(3, "tool/result", json!({"call_id":"call-1"})),
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

    let output = Command::new(env!("CARGO_BIN_EXE_hermes-trace"))
        .arg("verify")
        .arg(file.path())
        .output()
        .expect("run hermes-trace verify");

    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "ok");
}

#[test]
fn cli_verify_rejects_non_monotonic_trace() {
    let file = NamedTempFile::new().expect("temp trace");
    let input = [
        event(1, "turn/start", json!({})),
        event(1, "step/start", json!({})),
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

    let output = Command::new(env!("CARGO_BIN_EXE_hermes-trace"))
        .arg("verify")
        .arg(file.path())
        .output()
        .expect("run hermes-trace verify");

    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("sequence numbers must be monotonic"));
}

#[test]
fn cli_digest_is_normalized_and_changes_with_event_content() {
    let first = NamedTempFile::new().expect("first trace");
    let second = NamedTempFile::new().expect("second trace");
    let changed = NamedTempFile::new().expect("changed trace");

    let base = [
        event(1, "turn/start", json!({"input_tokens": 3})),
        event(2, "assistant/message", json!({"output_tokens": 5})),
    ];
    let mut different_times = base.clone();
    different_times[0].time += 100.0;
    different_times[1].time += 200.0;
    let changed_content = [
        event(1, "turn/start", json!({"input_tokens": 3})),
        event(2, "assistant/message", json!({"output_tokens": 6})),
    ];

    for (path, events) in [
        (first.path(), base.as_slice()),
        (second.path(), different_times.as_slice()),
        (changed.path(), changed_content.as_slice()),
    ] {
        std::fs::write(
            path,
            events
                .iter()
                .map(|value| serde_json::to_string(value).expect("serialize"))
                .collect::<Vec<_>>()
                .join("\n"),
        )
        .expect("write trace");
    }

    let digest = |path: &std::path::Path| -> String {
        let output = Command::new(env!("CARGO_BIN_EXE_hermes-trace"))
            .arg("digest")
            .arg(path)
            .output()
            .expect("run hermes-trace digest");
        assert!(
            output.status.success(),
            "stderr: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8_lossy(&output.stdout).trim().to_owned()
    };

    let first_digest = digest(first.path());
    assert_eq!(first_digest.len(), 64);
    assert!(first_digest.chars().all(|value| value.is_ascii_hexdigit()));
    assert_eq!(first_digest, digest(second.path()));
    assert_ne!(first_digest, digest(changed.path()));
}

#[test]
fn cli_summary_outputs_streamed_json_summary() {
    let file = NamedTempFile::new().expect("temp trace");
    let input = [
        event(1, "turn/start", json!({"input_tokens": 3})),
        event(2, "step/start", json!({})),
        event(3, "tool/call", json!({"call_id":"call-1"})),
        event(
            4,
            "tool/result",
            json!({"call_id":"call-1","is_error":true}),
        ),
        event(5, "assistant/message", json!({"output_tokens": 7})),
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

    let output = Command::new(env!("CARGO_BIN_EXE_hermes-trace"))
        .arg("summary")
        .arg(file.path())
        .output()
        .expect("run hermes-trace summary");

    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let value: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("summary stdout is JSON");
    assert_eq!(value["turns"], 1);
    assert_eq!(value["steps"], 1);
    assert_eq!(value["tool_calls"], 1);
    assert_eq!(value["tool_errors"], 1);
    assert_eq!(value["input_tokens"], 3);
    assert_eq!(value["output_tokens"], 7);
}

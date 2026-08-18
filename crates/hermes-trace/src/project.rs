use crate::{TraceError, TraceEvent};
use serde::Serialize;
use serde_json::Value;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct ProjectionSummary {
    pub turns: u64,
    pub steps: u64,
    pub tool_calls: u64,
    pub tool_errors: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct ProjectionRecord {
    pub seq: u64,
    pub time: f64,
    #[serde(rename = "type")]
    pub event_type: String,
    pub turn: Option<u64>,
    pub step: Option<u64>,
    pub data: Value,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Projection {
    pub summary: ProjectionSummary,
    pub records: Vec<ProjectionRecord>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Snapshot {
    pub summary: ProjectionSummary,
    pub records: Vec<SnapshotRecord>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct SnapshotRecord {
    pub seq: u64,
    #[serde(rename = "type")]
    pub event_type: String,
    pub turn: Option<u64>,
    pub step: Option<u64>,
    pub data: Value,
}

fn usage(data: &Value, field: &'static str) -> Result<u64, TraceError> {
    match data.get(field) {
        None => Ok(0),
        Some(value) => value.as_u64().ok_or(TraceError::InvalidUsage { field }),
    }
}

fn scan_reader(
    reader: impl BufRead,
    mut visit: impl FnMut(TraceEvent) -> Result<(), TraceError>,
) -> Result<ProjectionSummary, TraceError> {
    let mut summary = ProjectionSummary::default();
    let mut previous_seq = None;
    for (index, line) in reader.lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let event: TraceEvent =
            serde_json::from_str(&line).map_err(|error| TraceError::InvalidLine {
                line: index + 1,
                source: Box::new(TraceError::Json(error)),
            })?;
        event.validate().map_err(|error| TraceError::InvalidLine {
            line: index + 1,
            source: Box::new(error),
        })?;
        if let Some(previous) = previous_seq {
            if event.seq <= previous {
                return Err(TraceError::InvalidLine {
                    line: index + 1,
                    source: Box::new(TraceError::NonMonotonic {
                        previous,
                        next: event.seq,
                    }),
                });
            }
        }
        previous_seq = Some(event.seq);
        match event.event_type.as_str() {
            "turn/start" => summary.turns += 1,
            "step/start" => summary.steps += 1,
            "tool/call" => summary.tool_calls += 1,
            "tool/result" if event.data.get("is_error").and_then(Value::as_bool) == Some(true) => {
                summary.tool_errors += 1;
            }
            _ => {}
        }
        summary.input_tokens += usage(&event.data, "input_tokens")?;
        summary.output_tokens += usage(&event.data, "output_tokens")?;
        visit(event)?;
    }
    Ok(summary)
}

pub fn project_reader(reader: impl BufRead) -> Result<Projection, TraceError> {
    let mut records = Vec::new();
    let summary = scan_reader(reader, |event| {
        records.push(ProjectionRecord {
            seq: event.seq,
            time: event.time,
            event_type: event.event_type,
            turn: event.turn,
            step: event.step,
            data: event.data,
        });
        Ok(())
    })?;
    Ok(Projection { summary, records })
}

pub fn snapshot_reader(reader: impl BufRead) -> Result<Snapshot, TraceError> {
    let projection = project_reader(reader)?;
    Ok(Snapshot {
        summary: projection.summary,
        records: projection
            .records
            .into_iter()
            .map(|record| SnapshotRecord {
                seq: record.seq,
                event_type: record.event_type,
                turn: record.turn,
                step: record.step,
                data: record.data,
            })
            .collect(),
    })
}

pub fn write_project_file(
    path: impl AsRef<Path>,
    mut writer: impl Write,
    normalized: bool,
) -> Result<(), TraceError> {
    let path = path.as_ref();
    let summary = scan_reader(BufReader::new(File::open(path)?), |_| Ok(()))?;

    writer.write_all(b"{\"summary\":")?;
    serde_json::to_writer(&mut writer, &summary)?;
    writer.write_all(b",\"records\":[")?;
    let mut first = true;
    scan_reader(BufReader::new(File::open(path)?), |event| {
        if first {
            first = false;
        } else {
            writer.write_all(b",")?;
        }
        if normalized {
            serde_json::to_writer(
                &mut writer,
                &SnapshotRecord {
                    seq: event.seq,
                    event_type: event.event_type,
                    turn: event.turn,
                    step: event.step,
                    data: event.data,
                },
            )?;
        } else {
            serde_json::to_writer(
                &mut writer,
                &ProjectionRecord {
                    seq: event.seq,
                    time: event.time,
                    event_type: event.event_type,
                    turn: event.turn,
                    step: event.step,
                    data: event.data,
                },
            )?;
        }
        Ok(())
    })?;
    writer.write_all(b"]}")?;
    Ok(())
}

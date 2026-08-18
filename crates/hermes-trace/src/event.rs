use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum TraceError {
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("event type must not be empty")]
    EmptyEventType,
    #[error("session_id must not be empty")]
    EmptySessionId,
    #[error("sequence numbers must be monotonic: previous {previous}, next {next}")]
    NonMonotonic { previous: u64, next: u64 },
    #[error("{event_type} requires data.call_id")]
    MissingCallId { event_type: String },
    #[error("invalid trace at line {line}: {source}")]
    InvalidLine {
        line: usize,
        #[source]
        source: Box<TraceError>,
    },
    #[error("usage value {field} must be a non-negative integer")]
    InvalidUsage { field: &'static str },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct TraceEvent {
    pub schema_version: u16,
    pub seq: u64,
    pub time: f64,
    pub session_id: String,
    #[serde(rename = "type")]
    pub event_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub step: Option<u64>,
    #[serde(default)]
    pub data: Value,
}

impl TraceEvent {
    pub fn validate(&self) -> Result<(), TraceError> {
        if self.event_type.trim().is_empty() {
            return Err(TraceError::EmptyEventType);
        }
        if self.session_id.trim().is_empty() {
            return Err(TraceError::EmptySessionId);
        }
        if matches!(self.event_type.as_str(), "tool/call" | "tool/result")
            && self
                .data
                .get("call_id")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
        {
            return Err(TraceError::MissingCallId {
                event_type: self.event_type.clone(),
            });
        }
        Ok(())
    }
}

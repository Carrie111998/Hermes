use crate::{TraceError, TraceEvent};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;

pub struct EventWriter {
    writer: BufWriter<File>,
    last_seq: Option<u64>,
}

impl EventWriter {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, TraceError> {
        let path = path.as_ref();
        let last_seq = if path.exists() {
            let reader = BufReader::new(File::open(path)?);
            let mut last = None;
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
                if let Some(previous) = last {
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
                last = Some(event.seq);
            }
            last
        } else {
            None
        };
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Self {
            writer: BufWriter::new(file),
            last_seq,
        })
    }

    pub fn append(&mut self, event: &TraceEvent) -> Result<(), TraceError> {
        event.validate()?;
        if let Some(previous) = self.last_seq {
            if event.seq <= previous {
                return Err(TraceError::NonMonotonic {
                    previous,
                    next: event.seq,
                });
            }
        }
        serde_json::to_writer(&mut self.writer, event)?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()?;
        self.last_seq = Some(event.seq);
        Ok(())
    }
}

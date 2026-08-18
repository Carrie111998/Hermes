use hermes_trace::{project_reader, write_project_file, EventWriter, TraceEvent};
use std::env;
use std::fs::File;
use std::io::{self, BufReader, BufWriter};
use std::process::ExitCode;

fn usage() -> &'static str {
    "usage: hermes-trace <verify|project|snapshot|append> <trace.jsonl>\nappend reads one JSON event from stdin"
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let command = args.next().ok_or_else(|| usage().to_string())?;
    let path = args.next().ok_or_else(|| usage().to_string())?;
    if args.next().is_some() {
        return Err(usage().into());
    }
    match command.as_str() {
        "verify" => {
            project_reader(BufReader::new(File::open(path)?))?;
            println!("ok");
        }
        "project" => {
            write_project_file(path, BufWriter::new(io::stdout().lock()), false)?;
            println!();
        }
        "snapshot" => {
            write_project_file(path, BufWriter::new(io::stdout().lock()), true)?;
            println!();
        }
        "append" => {
            let event: TraceEvent = serde_json::from_reader(io::stdin().lock())?;
            EventWriter::open(path)?.append(&event)?;
        }
        _ => return Err(usage().into()),
    }
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("hermes-trace: {error}");
            ExitCode::FAILURE
        }
    }
}

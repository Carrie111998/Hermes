use hermes_trace::{digest_reader, verify_reader, write_project_file, EventWriter, TraceEvent};
use std::env;
use std::fs::File;
use std::io::{self, BufReader, BufWriter};
use std::process::ExitCode;

fn usage() -> &'static str {
    "usage: hermes-trace <verify|summary|project|snapshot|digest|append> <trace.jsonl>\nappend reads one JSON event from stdin"
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
            verify_reader(BufReader::new(File::open(path)?))?;
            println!("ok");
        }
        "summary" => {
            let summary = verify_reader(BufReader::new(File::open(path)?))?;
            serde_json::to_writer(io::stdout().lock(), &summary)?;
            println!();
        }
        "project" => {
            write_project_file(path, BufWriter::new(io::stdout().lock()), false)?;
            println!();
        }
        "snapshot" => {
            write_project_file(path, BufWriter::new(io::stdout().lock()), true)?;
            println!();
        }
        "digest" => {
            println!("{}", digest_reader(BufReader::new(File::open(path)?))?);
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

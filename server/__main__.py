"""CLI harness for product agent runs and local API startup."""
from __future__ import annotations

import argparse
import json
import stat
import sys
import time
from pathlib import Path

from .agent_service import AgentRunService, HermesProcessExecutor, StubRunExecutor
from .api_cli import main as api_main
from .config import Settings
from .db import json_dump, new_id, now
from .markets import no_research_markets
from .postgres import create_database
from .provisioning import provision_demo_account
from .run_types import REGISTRY
from .lead_research.candidates import CandidateRepository


def _market_gate(company: str, payload: dict) -> None:
    """Compatibility gate for the scrubbed demo-pack CLI path."""
    countries = [str(code).upper() for code in payload.get("countries", [])]
    if len(countries) > 5:
        raise SystemExit(f"lead-map rule: max 5 countries per scan, got {len(countries)}")
    blocked = sorted(set(countries) & no_research_markets(company))
    if blocked:
        raise SystemExit(f"market preferences: {blocked} marked no-research for {company}")


def _payload(args) -> dict:
    if args.payload:
        return json.loads(Path(args.payload).read_text(encoding="utf-8"))
    return json.loads(args.payload_json) if args.payload_json else {}


def _company_id(db, value: str) -> str:
    row = db.one("SELECT id FROM companies WHERE id=? OR lower(name)=lower(?) LIMIT 1", (value, value))
    if row:
        return row["id"]
    company_id, stamp = new_id("cmp"), now()
    db.execute("INSERT INTO companies VALUES(?,?,?,?,?,?,?)",
               (company_id, value, None, "active", json_dump({}), stamp, stamp))
    db.execute("INSERT INTO onboarding(company_id,updated_at) VALUES(?,?)", (company_id, stamp))
    return company_id


def _print(run: dict) -> None:
    print(json.dumps(run, ensure_ascii=False, indent=2))


def _read_password_file(path: Path) -> str:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("password file must be a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("password file permissions must restrict access to its owner")
    password = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError("password file is empty")
    return password


def _load_provisioning_profile(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("profile must be a JSON object")
    company_profile = data.get("company_profile")
    onboarding_sources = data.get("onboarding_sources")
    if not isinstance(company_profile, dict) or not isinstance(onboarding_sources, list):
        raise ValueError("profile must contain company_profile and onboarding_sources")
    return company_profile, onboarding_sources


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--company", default="silverline")
    run.add_argument("--type", required=True, choices=sorted(REGISTRY))
    run.add_argument("--payload")
    run.add_argument("--payload-json")
    run.add_argument("--idem")
    run.add_argument("--stub", action="store_true")
    run.add_argument("--timeout", type=int, default=900)
    for name in ("list", "show", "cancel", "retry"):
        command = sub.add_parser(name)
        command.add_argument("--company", default="silverline")
        if name != "list":
            command.add_argument("run_id")
    sub.add_parser("types")
    provision = sub.add_parser("provision-demo", help="Provision a clean, completed demo account")
    provision.add_argument("--email", required=True)
    provision.add_argument("--password-file", type=Path, required=True)
    provision.add_argument("--profile", type=Path, required=True)
    candidates = sub.add_parser("import-candidates", help="Import a private service-only candidate corpus")
    candidates.add_argument("--dataset-id", required=True)
    candidates.add_argument("--version", required=True)
    candidates.add_argument("--file", type=Path, required=True)
    backfill = sub.add_parser(
        "backfill-candidate-search",
        help="Fill search_text for corpora imported before that column existed",
    )
    backfill.add_argument(
        "--batch", type=int, default=2000,
        help="Rows per transaction; a corpus can be large and one transaction "
             "over all of it holds a write lock for as long as it takes",
    )
    args = parser.parse_args(argv)

    if args.command == "serve":
        values = ["--host", args.host, "--port", str(args.port)]
        if args.reload:
            values.append("--reload")
        api_main(values)
        return
    if args.command == "types":
        for run_type, (skill, _) in REGISTRY.items():
            print(f"{run_type:26} -> {skill or '(deterministic aggregation)'}")
        return

    if args.command == "provision-demo":
        settings = Settings.load()
        if settings.auth_mode != "local":
            raise SystemExit("provision-demo is intentionally limited to auth_mode: local")
        db = create_database(settings)
        try:
            company_profile, onboarding_sources = _load_provisioning_profile(args.profile)
            _print(provision_demo_account(
                db,
                email=args.email,
                password=_read_password_file(args.password_file),
                company_profile=company_profile,
                onboarding_sources=onboarding_sources,
            ))
        finally:
            close = getattr(db, "close", None)
            if close:
                close()
        return

    if args.command == "backfill-candidate-search":
        settings = Settings.load()
        db = create_database(settings)
        try:
            if args.batch < 1:
                raise SystemExit("--batch must be at least 1")
            filled = CandidateRepository(db).backfill_search_text(batch=args.batch)
            remaining = db.one(
                "SELECT COUNT(*) AS n FROM candidate_records WHERE search_text IS NULL"
            )["n"]
            # Counts only. Candidate rows are never echoed, here or anywhere
            # else in this CLI.
            print(json.dumps({"filled": filled, "remaining": remaining}))
            # Safe to re-run and safe to skip: selection computes the value for a
            # row that lacks it, so this only ever buys speed. Saying so on
            # stderr because a bare `{"filled": 0}` reads like a failure.
            if not filled:
                print(
                    "note: nothing to backfill; every corpus row already has "
                    "search text", file=sys.stderr,
                )
        finally:
            close = getattr(db, "close", None)
            if close:
                close()
        return

    if args.command == "import-candidates":
        settings = Settings.load()
        db = create_database(settings)
        try:
            report = CandidateRepository(db).import_file(
                args.dataset_id, args.version, args.file.name, args.file.read_bytes(),
            )
            # Candidate rows are intentionally never echoed to stdout.
            print(json.dumps({
                "dataset_id": report.dataset_id,
                "version": report.version,
                "count": report.record_count,
                "raw_hash": report.raw_hash,
                # Sector ids come from our own catalog, so naming them
                # discloses nothing. The unknown categories are corpus values
                # and are counted, never echoed — the report object still
                # carries them for a caller in-process.
                "sector_categories": list(report.sector_categories),
                "unknown_category_count": len(report.unknown_categories),
                "findable_by_sector": report.findable_by_sector,
                "warnings": report.warnings(),
            }, ensure_ascii=False))
            # On stderr as well as in the payload: a corpus that no sector search
            # can reach imports perfectly and is then invisible, and the machine
            # -readable field alone is easy to miss in a terminal. A corpus is
            # immutable, so the cheapest moment to notice is now — the fix is
            # re-importing as a new version, not editing this one.
            for warning in report.warnings():
                print(f"warning: {warning}", file=sys.stderr)
        finally:
            close = getattr(db, "close", None)
            if close:
                close()
        return

    settings = Settings.load()
    db = create_database(settings)
    executor = (StubRunExecutor() if getattr(args, "stub", False)
                else HermesProcessExecutor(getattr(args, "timeout", 900)))
    service = AgentRunService(db, executor)
    company_id = _company_id(db, args.company)

    if args.command == "run":
        payload = _payload(args)
        if args.type == "lead_scan":
            _market_gate(args.company, payload)
        run = service.create(company_id, args.type, payload, args.idem)
        run = service.start(company_id, run["id"])
        while run["status"] not in {"succeeded", "failed", "cancelled"}:
            time.sleep(0.1)
            run = service.get(company_id, run["id"])
        _print(run)
    elif args.command == "list":
        _print({"runs": service.list(company_id)})
    elif args.command == "show":
        _print(service.get(company_id, args.run_id))
    elif args.command == "cancel":
        _print(service.cancel(company_id, args.run_id))
    elif args.command == "retry":
        _print(service.retry(company_id, args.run_id))


if __name__ == "__main__":
    main()

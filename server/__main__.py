"""CLI harness for product agent runs and local API startup."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .agent_service import AgentRunService, HermesProcessExecutor, StubRunExecutor
from .api_cli import main as api_main
from .config import Settings
from .db import json_dump, new_id, now
from .demo_seed import seed_silverline
from .markets import no_research_markets
from .postgres import create_database
from .run_types import REGISTRY


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
    seed = sub.add_parser("seed-demo", help="Create/reset the tenant-backed Silverine test client")
    seed.add_argument("--email", default="client@silverline.test")
    seed.add_argument("--password", default="silverline-test-123")
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

    if args.command == "seed-demo":
        settings = Settings.load()
        if settings.auth_mode != "local":
            raise SystemExit("seed-demo is intentionally limited to auth_mode: local")
        db = create_database(settings)
        try:
            _print(seed_silverline(db, email=args.email, password=args.password))
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

"""Durable agent-run orchestration used by both FastAPI and the CLI.

Runs are tenant-scoped, payload-validated, evented, cancellable, and executed
outside request threads. Model stdout is treated as untrusted and must contain
a JSON object matching the selected run type before the run can succeed.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from . import run_types
from .agent_evidence import evidence_from_log, evidence_from_output
from .db import Database, json_dump, json_load, new_id, now
from .quality import (canonical_linkedin_url, content_hash, normalize_name,
                      preflight_message, validate_contact_record)
from .lead_research.models import AgenticResearchRequest, AgenticResearchResult
from .lead_research.contacts import verify_contact as verify_contact_evidence


TERMINAL = {"succeeded", "failed", "cancelled"}
OUTPUT_KEYS: dict[str, set[str]] = {
    "document_processing": {"records", "rejects"},
    "product_extraction": {"products"},
    "company_brain_build": {
        "product_understanding", "ideal_customer_profile", "buyer_roles",
        "market_assumptions", "sales_arguments", "business_rules_digest", "missing_data",
    },
    "company_profile_research": {
        "identity", "seller_countries", "products", "market_preferences", "source_spans",
    },
    "lead_scan": {"leads"},
    "lead_research": {"profile", "fit", "signals", "approach_angle", "score_inputs"},
    "lead_research_gap": {
        "pages", "facts", "unresolved_fields", "requests_started", "tokens_used", "stop_reason",
    },
    "lead_research_refresh": {
        "pages", "facts", "unresolved_fields", "requests_started", "tokens_used", "stop_reason",
    },
    "contact_discovery": {"contacts"},
    "outreach_generation": {"body", "language", "to", "cc", "qa_verdict"},
    "email_send": {"provider_message_id", "status"},
    "whatsapp_send": {"provider_message_id", "status"},
    "linkedin_note_generation": {"profile_url", "note"},
    "analytics_refresh": {"metrics"},
}


def _run_dict(row) -> dict:
    return {
        "id": row["id"], "company_id": row["company_id"], "run_type": row["run_type"],
        "status": row["status"], "payload": json_load(row["payload"], {}),
        "output": json_load(row["output"], None), "output_ref": row["output_ref"],
        "error": row["error"], "cost": row["cost"],
        "cancellation_requested": bool(row["cancellation_requested"]),
        "created_at": row["created_at"], "started_at": row["started_at"],
        "completed_at": row["completed_at"], "updated_at": row["updated_at"],
    }


def validate_payload(run_type: str, payload: dict, db: Database, company_id: str) -> None:
    if run_type not in run_types.REGISTRY:
        raise HTTPException(422, f"Unknown run type: {run_type}")
    if run_type in {"email_send", "whatsapp_send"}:
        raise HTTPException(
            422,
            f"{run_type} runs are created only by the approval-gated delivery service",
        )
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload must be an object")
    if run_type == "lead_research_gap":
        try:
            AgenticResearchRequest.model_validate(payload)
        except Exception as exc:
            raise HTTPException(422, f"invalid lead research gap payload: {exc}") from exc
    if run_type == "lead_research_refresh":
        required = {"fact_id", "field", "dedupe_key", "organization_id", "budget"}
        missing_refresh = sorted(required - set(payload))
        if missing_refresh:
            raise HTTPException(422, {
                "message": "Missing research refresh payload fields", "fields": missing_refresh,
            })
        fact_id = str(payload.get("fact_id") or "")
        if fact_id.startswith("sf_"):
            allowed = db.one(
                "SELECT 1 FROM research_fact_consumers WHERE company_id=? AND shared_fact_id=?",
                (company_id, fact_id),
            )
        else:
            allowed = db.one(
                "SELECT 1 FROM tenant_facts WHERE company_id=? AND id=?",
                (company_id, fact_id),
            )
        if not allowed:
            raise HTTPException(422, "research refresh fact is outside the tenant")
        budget = payload.get("budget") or {}
        if int(budget.get("page_limit", 0)) > 2 or int(budget.get("request_limit", 0)) > 3 \
                or int(budget.get("time_limit_seconds", 0)) > 45 \
                or int(budget.get("token_limit", 0)) > 2_000:
            raise HTTPException(422, "research refresh exceeds the background budget")
    if run_type == "company_profile_research":
        website = payload.get("official_website")
        parsed = urlsplit(str(website or ""))
        if parsed.scheme != "https" or not parsed.hostname:
            raise HTTPException(422, "company_profile_research requires an official https website")
        pages = payload.get("max_pages")
        seconds = payload.get("max_seconds")
        if not isinstance(pages, int) or not 1 <= pages <= 8:
            raise HTTPException(422, "company profile research is limited to 8 pages")
        if not isinstance(seconds, int) or not 10 <= seconds <= 120:
            raise HTTPException(422, "company profile research is limited to 120 seconds")
    if run_type == "lead_scan":
        countries = payload.get("countries") or []
        if not 1 <= len(countries) <= 5:
            raise HTTPException(422, "lead_scan requires between 1 and 5 countries")
        invalid = [code for code in countries if not isinstance(code, str) or len(code) != 2]
        if invalid:
            raise HTTPException(422, {"message": "Invalid ISO country codes", "countries": invalid})
        section = db.one(
            "SELECT data FROM company_sections WHERE company_id=? AND section='market_preferences'",
            (company_id,),
        )
        preferences = json_load(section["data"], {}) if section else {}
        blocked = {str(code).upper() for code in preferences.get("no_research_markets", [])}
        requested = {str(code).upper() for code in countries}
        if requested & blocked:
            raise HTTPException(409, {"message": "Markets blocked from research",
                                      "countries": sorted(requested & blocked)})
    required_by_type = {
        "document_processing": ("document_id",),
        "product_extraction": ("document_ids",),
        "lead_research": ("lead_id",),
        "contact_discovery": ("lead_ids",),
        "linkedin_note_generation": ("contact_id",),
    }
    missing = [key for key in required_by_type.get(run_type, ()) if key not in payload]
    if missing:
        raise HTTPException(422, {"message": "Missing run payload fields", "fields": missing})


def _last_lines(transcript: str, limit: int = 3) -> str:
    """The tail of an agent transcript, for an error a human has to act on."""
    lines = [line.strip() for line in transcript.strip().splitlines() if line.strip()]
    return " | ".join(lines[-limit:]) if lines else "(no output)"


def extract_json(text: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("agent returned no output")
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    # The final answer is the LAST top-level JSON object on stdout; earlier
    # ones are echoed payloads or tool logs. Parse forward, skipping past each
    # complete object so nested dicts are never mistaken for the answer.
    decoder = json.JSONDecoder()
    last: dict | None = None
    index = 0
    while (index := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            last = value
            index += end
        else:
            index += 1
    if last is not None:
        return last
    raise ValueError("agent output did not contain a JSON object")


class BaseRunExecutor:
    def execute(self, service: "AgentRunService", run: dict) -> dict:
        raise NotImplementedError

    def cancel(self, run_id: str) -> None:
        return None


class StubRunExecutor(BaseRunExecutor):
    """Credential-free executor used by tests and local UI development."""
    def execute(self, service: "AgentRunService", run: dict) -> dict:
        run_type = run["run_type"]
        if run_type == "analytics_refresh":
            return {"metrics": service.analytics(run["company_id"])}
        skill, prompt = run_types.build(
            run_type, service.company_name(run["company_id"]), run["payload"],
            context=service.company_context(run["company_id"]),
        )
        values = {key: _stub_value(key) for key in OUTPUT_KEYS[run_type]}
        if run_type == "company_profile_research":
            values.update({
                "identity": {
                    "name": service.company_name(run["company_id"]),
                    "website": run["payload"]["official_website"],
                },
                "seller_countries": [],
                "products": [],
                "market_preferences": {},
                "source_spans": [],
            })
        if run_type == "outreach_generation":
            draft = run["payload"].get("draft_content") or {}
            values.update({
                "subject": draft.get("subject", "Partnership opportunity"),
                "body": draft.get("body", "Hello, we would like to explore a potential business partnership."),
                "language": draft.get("language", "en"),
                "to": draft.get("to") or run["payload"].get("to", "buyer@example.com"),
                "cc": draft.get("cc", []),
                "qa_verdict": {"pass": True, "failures": []},
            })
        return {"stub": True, "skill": skill, "prompt_chars": len(prompt or ""), **values}


def _stub_value(key: str):
    if key in {"records", "rejects", "products", "leads", "contacts", "cc", "signals", "missing_data", "source_spans", "pages", "facts", "unresolved_fields"}:
        return []
    if key == "qa_verdict":
        return {"pass": True, "failures": []}
    if key in {"score_inputs", "profile", "fit", "product_understanding", "ideal_customer_profile",
               "buyer_roles", "market_assumptions", "sales_arguments", "business_rules_digest"}:
        return {}
    if key in {"requests_started", "tokens_used"}:
        return 0
    if key == "stop_reason":
        return "source_exhausted"
    return ""


class HermesProcessExecutor(BaseRunExecutor):
    def __init__(self, timeout: int = 900):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen] = {}

    def execute(self, service: "AgentRunService", run: dict) -> dict:
        company_name = service.company_name(run["company_id"])
        skill, prompt = run_types.build(
            run["run_type"], company_name, run["payload"],
            context=service.company_context(run["company_id"]),
        )
        if skill is None:
            return {"metrics": service.analytics(run["company_id"])}
        command = ["hermes", "-z", prompt, "--skills", skill, "--yolo"]
        if run["run_type"] == "lead_research_gap":
            command.extend(["--model", run["payload"]["decision_model"]])
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ},
        )
        with self._lock:
            self._processes[run["id"]] = process
        lines: list[str] = []
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_queue.put(line)
            output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True,
                                  name=f"interfaze-output-{run['id']}")
        reader.start()
        deadline = time.monotonic() + self.timeout
        try:
            output_closed = False
            while process.poll() is None or not output_closed:
                try:
                    line = output_queue.get(timeout=0.2)
                    if line is None:
                        output_closed = True
                    else:
                        lines.append(line)
                        service.event(run["id"], run["company_id"], "log", line.rstrip()[:4000])
                        # Sources are captured as they scroll past: run_events
                        # is trimmed and read live, this outlives the run.
                        service.record_evidence(
                            run["company_id"], run["id"], evidence_from_log(line)
                        )
                except queue.Empty:
                    pass
                if service.cancellation_requested(run["id"]):
                    process.terminate()
                    raise InterruptedError("run cancelled")
                if time.monotonic() >= deadline:
                    process.kill()
                    raise TimeoutError(f"run exceeded {self.timeout} seconds")
                if output_closed and process.poll() is not None:
                    break
            returncode = process.wait(timeout=5)
            transcript = "".join(lines)
            if returncode != 0:
                raise RuntimeError(
                    f"Hermes exited with status {returncode}: {_last_lines(transcript)}"
                )
            try:
                return extract_json(transcript)
            except ValueError as exc:
                # `hermes -z` reports its own failures on stdout and still
                # exits 0 — an unset provider, an expired key, a model the
                # account cannot reach. Without the transcript those all
                # surface as "agent output did not contain a JSON object",
                # which is true and tells an operator nothing.
                raise RuntimeError(f"{exc}: {_last_lines(transcript)}") from exc
        finally:
            with self._lock:
                self._processes.pop(run["id"], None)

    def cancel(self, run_id: str) -> None:
        with self._lock:
            process = self._processes.get(run_id)
        if process and process.poll() is None:
            process.terminate()


class AgentRunService:
    def __init__(self, db: Database, executor: BaseRunExecutor | None = None, workers: int = 4):
        self.db = db
        self.executor = executor or HermesProcessExecutor()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="interfaze-run")

    def company_name(self, company_id: str) -> str:
        row = self.db.one("SELECT name FROM companies WHERE id=?", (company_id,))
        if not row:
            raise ValueError("company not found")
        return row["name"]

    def company_context(self, company_id: str) -> dict:
        sections = {
            row["section"]: json_load(row["data"], {})
            for row in self.db.all("SELECT section,data FROM company_sections WHERE company_id=?", (company_id,))
        }
        products = [json_load(row["data"], {}) for row in self.db.all(
            "SELECT data FROM products WHERE company_id=? ORDER BY name", (company_id,)
        )]
        brain = self.db.one(
            "SELECT content FROM company_brain_snapshots WHERE company_id=? AND status='approved' "
            "ORDER BY version DESC LIMIT 1", (company_id,),
        )
        return {
            "sections": sections,
            "products": products,
            "approved_company_brain": json_load(brain["content"], None) if brain else None,
        }

    def create(self, company_id: str, run_type: str, payload: dict,
               idempotency_key: str | None = None) -> dict:
        validate_payload(run_type, payload, self.db, company_id)
        if idempotency_key:
            existing = self.db.one(
                "SELECT * FROM agent_runs WHERE company_id=? AND idempotency_key=?",
                (company_id, idempotency_key),
            )
            if existing:
                return _run_dict(existing)
        run_id, stamp = new_id("run"), now()
        self.db.execute(
            "INSERT INTO agent_runs(id,company_id,run_type,status,payload,idempotency_key,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (run_id, company_id, run_type, "queued", json_dump(payload), idempotency_key, stamp, stamp),
        )
        self.event(run_id, company_id, "created", f"{run_type} queued")
        return self.get(company_id, run_id)

    def get(self, company_id: str, run_id: str) -> dict:
        row = self.db.one("SELECT * FROM agent_runs WHERE id=? AND company_id=?", (run_id, company_id))
        if not row:
            raise HTTPException(404, "Agent run not found")
        return _run_dict(row)

    def list(self, company_id: str) -> list[dict]:
        return [_run_dict(row) for row in self.db.all(
            "SELECT * FROM agent_runs WHERE company_id=? ORDER BY created_at DESC", (company_id,)
        )]

    def start(self, company_id: str, run_id: str) -> dict:
        run = self.get(company_id, run_id)
        if run["status"] != "queued":
            raise HTTPException(409, f"Run cannot start from {run['status']}")
        changed = self.db.execute(
            "UPDATE agent_runs SET status='running',started_at=?,updated_at=? "
            "WHERE id=? AND company_id=? AND status='queued'",
            (now(), now(), run_id, company_id),
        )
        if not changed:
            raise HTTPException(409, "Run was already started")
        self.event(run_id, company_id, "started")
        self.pool.submit(self._execute, company_id, run_id)
        return self.get(company_id, run_id)

    def _execute(self, company_id: str, run_id: str) -> None:
        run = self.get(company_id, run_id)
        try:
            output = self.executor.execute(self, run)
            self._validate_output(run["run_type"], output)
            # From the validated output, before persistence: the sources the
            # agent explicitly cited are the ones worth keeping.
            self.record_evidence(company_id, run_id, evidence_from_output(output))
            self.apply_output(run, output)
            stamp = now()
            self.db.execute(
                "UPDATE agent_runs SET status='succeeded',output=?,completed_at=?,updated_at=? WHERE id=?",
                (json_dump(output), stamp, stamp, run_id),
            )
            self._sync_parent_status(run, "succeeded")
            self.event(run_id, company_id, "succeeded")
        except InterruptedError:
            stamp = now()
            self.db.execute("UPDATE agent_runs SET status='cancelled',completed_at=?,updated_at=? WHERE id=?",
                            (stamp, stamp, run_id))
            self._sync_parent_status(run, "cancelled")
            self.event(run_id, company_id, "cancelled")
        except Exception as exc:
            stamp = now()
            self.db.execute(
                "UPDATE agent_runs SET status='failed',error=?,completed_at=?,updated_at=? WHERE id=?",
                (str(exc)[:4000], stamp, stamp, run_id),
            )
            self._sync_parent_status(run, "failed")
            self.event(run_id, company_id, "failed", str(exc)[:1000])

    def _validate_output(self, run_type: str, output: dict) -> None:
        # No stub escape hatch: model stdout is untrusted, and StubRunExecutor
        # output carries every required key anyway.
        if not isinstance(output, dict):
            raise ValueError("run output must be a JSON object")
        missing = OUTPUT_KEYS[run_type] - set(output)
        if missing:
            raise ValueError(f"run output missing fields: {sorted(missing)}")
        if run_type in {"lead_research_gap", "lead_research_refresh"}:
            AgenticResearchResult.model_validate(output)
        if run_type == "company_profile_research":
            spans = output.get("source_spans")
            products = output.get("products")
            if not isinstance(spans, list) or not isinstance(products, list):
                raise ValueError("company profile products and source spans must be lists")
            span_ids: set[str] = set()
            for span in spans:
                if not isinstance(span, dict):
                    raise ValueError("source span must be an object")
                span_id = str(span.get("id") or "").strip()
                exact_text = str(span.get("exact_text") or "").strip()
                source_url = str(span.get("source_url") or "")
                if not span_id or len(exact_text) < 3 or not source_url.startswith("https://"):
                    raise ValueError("source span requires id, https source_url, and exact text")
                span_ids.add(span_id)
            for product in products:
                references = product.get("source_span_ids") if isinstance(product, dict) else None
                if not isinstance(references, list) or not references or not set(references) <= span_ids:
                    raise ValueError("each derived product requires a valid exact source span")
        if run_type == "contact_discovery":
            contacts = output.get("contacts")
            if not isinstance(contacts, list):
                raise ValueError("contact discovery contacts must be a list")
            for contact in contacts:
                if not isinstance(contact, dict):
                    raise ValueError("discovered contact must be an object")
                evidence = contact.get("evidence", [])
                if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
                    raise ValueError("discovered contact evidence must be a list of objects")
                for item in evidence:
                    url = item.get("provenance_url")
                    if url is not None and not str(url).startswith("https://"):
                        raise ValueError("contact evidence provenance must use https")

    def _sync_parent_status(self, run: dict, status: str) -> None:
        payload = run["payload"]
        if run["run_type"] == "lead_scan" and payload.get("scan_id"):
            self.db.execute("UPDATE lead_scans SET status=?,updated_at=? WHERE id=? AND company_id=?",
                            (status, now(), payload["scan_id"], run["company_id"]))
        # `documents.status` is technical readiness of the file itself, owned by
        # DocumentProcessingService. A semantic extraction run that fails says
        # nothing about whether the document is still usable, so it must not
        # push the document out of `ready` — the run's own status carries that.

    def cancel(self, company_id: str, run_id: str) -> dict:
        run = self.get(company_id, run_id)
        if run["status"] in TERMINAL:
            raise HTTPException(409, f"Run is already {run['status']}")
        self.db.execute("UPDATE agent_runs SET cancellation_requested=1,updated_at=? WHERE id=?",
                        (now(), run_id))
        self.executor.cancel(run_id)
        if run["status"] == "queued":
            stamp = now()
            self.db.execute("UPDATE agent_runs SET status='cancelled',completed_at=?,updated_at=? WHERE id=?",
                            (stamp, stamp, run_id))
        self.event(run_id, company_id, "cancellation_requested")
        return self.get(company_id, run_id)

    def retry(self, company_id: str, run_id: str) -> dict:
        old = self.get(company_id, run_id)
        if old["status"] not in TERMINAL:
            raise HTTPException(409, "Only terminal runs can be retried")
        return self.create(company_id, old["run_type"], old["payload"])

    def cancellation_requested(self, run_id: str) -> bool:
        row = self.db.one("SELECT cancellation_requested FROM agent_runs WHERE id=?", (run_id,))
        return bool(row and row["cancellation_requested"])

    def event(self, run_id: str, company_id: str, kind: str,
              message: str = "", data: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO run_events(run_id,company_id,ts,kind,message,data) VALUES(?,?,?,?,?,?)",
            (run_id, company_id, now(), kind, message, json_dump(data or {})),
        )

    def record_evidence(
        self,
        company_id: str,
        run_id: str,
        items,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> int:
        """Persist what a run looked at. Redacted, deduplicated, never fatal.

        Called from the log-reading loop while a run is still going, so it must
        not raise: losing an evidence row is a gap in the audit trail; raising
        would abort the run itself.
        """
        from .agent_evidence import serialize_evidence

        stored = 0
        for item in items or []:
            try:
                self.db.execute(
                    "INSERT INTO agent_run_evidence(id,company_id,run_id,entity_type,entity_id,"
                    "source_type,source_url,file_reference,title,retrieved_at,metadata,result,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    # Empty string, never NULL: SQLite treats NULLs as distinct
                    # in a UNIQUE index, so a NULL source_url would let the
                    # same file-based source insert without limit.
                    (new_id("ev"), company_id, run_id, entity_type, entity_id,
                     item.source_type, item.source_url or "",
                     item.file_reference or "", item.title or None, now(),
                     serialize_evidence(item.metadata), serialize_evidence(item.result),
                     now()),
                )
                stored += 1
            except Exception:
                # Almost always the dedupe index: the same source seen twice.
                continue
        return stored

    def evidence(self, company_id: str, run_id: str) -> list[dict]:
        return [
            {
                "id": row["id"], "source_type": row["source_type"],
                "source_url": row["source_url"], "file_reference": row["file_reference"],
                "title": row["title"], "retrieved_at": row["retrieved_at"],
                "entity_type": row["entity_type"], "entity_id": row["entity_id"],
                "metadata": json_load(row["metadata"], {}),
                "result": json_load(row["result"], None),
            }
            for row in self.db.all(
                "SELECT * FROM agent_run_evidence WHERE company_id=? AND run_id=?"
                " ORDER BY created_at, id",
                (company_id, run_id),
            )
        ]

    def detail(self, company_id: str, run_id: str) -> dict:
        """Everything an administrator needs to audit one run.

        Tenant-scoped like every other read here: callers that reach this from
        the cross-company admin API must resolve the run's own company first.
        """
        run = self.get(company_id, run_id)
        payload = run.get("payload") or {}
        related = {
            key: payload[key]
            for key in ("document_id", "source_document_id", "lead_id", "contact_id",
                        "scan_id", "campaign_id", "message_id", "product_id")
            if payload.get(key)
        }
        return {
            **run,
            "events": self.events(company_id, run_id),
            "evidence": self.evidence(company_id, run_id),
            "related": related,
        }

    def events(self, company_id: str, run_id: str) -> list[dict]:
        self.get(company_id, run_id)
        return [{"id": row["id"], "ts": row["ts"], "kind": row["kind"],
                 "message": row["message"], "data": json_load(row["data"], {})}
                for row in self.db.all(
                    "SELECT * FROM run_events WHERE run_id=? AND company_id=? ORDER BY id",
                    (run_id, company_id),
                )]

    def analytics(self, company_id: str) -> dict:
        def count(table: str, extra: str = "", params: tuple = ()) -> int:
            row = self.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE company_id=? {extra}",
                              (company_id, *params))
            return int(row["n"])
        return {
            "leads": count("leads"), "contacts": count("contacts"),
            "messages": count("outreach_messages"),
            "sent": count("outreach_messages", "AND status='sent'"),
            "replied": count("outreach_messages", "AND replied_at IS NOT NULL"),
            "agent_runs": count("agent_runs"),
            "failed_runs": count("agent_runs", "AND status='failed'"),
        }

    def _clear_document_records(self, company_id: str, document_id: str) -> None:
        """Retire the records a previous run derived from this same document.

        Re-running extraction is meant to replace, never accumulate. Products
        already collapse on their unique name, but contacts and the free-form
        `processed_records` section appended, so a second run doubled them.

        Matching happens in Python rather than SQL because `data` is a JSON
        string on both backends and the extraction operators differ
        (json_extract vs ->>); the row counts here are per-document, not
        per-tenant.
        """
        for table in ("products", "contacts"):
            for row in self.db.all(
                f"SELECT id, data FROM {table} WHERE company_id=?", (company_id,)
            ):
                if json_load(row["data"], {}).get("source_document_id") == document_id:
                    self.db.execute(
                        f"DELETE FROM {table} WHERE id=? AND company_id=?",
                        (row["id"], company_id),
                    )

        section = self.db.one(
            "SELECT data FROM company_sections WHERE company_id=? AND section='processed_records'",
            (company_id,),
        )
        if not section:
            return
        records = json_load(section["data"], {}).get("records", [])
        kept = [r for r in records if r.get("source_document_id") != document_id]
        if len(kept) != len(records):
            self.db.execute(
                "UPDATE company_sections SET data=?,updated_at=? "
                "WHERE company_id=? AND section='processed_records'",
                (json_dump({"records": kept}), now(), company_id),
            )

    def apply_output(self, run: dict, output: dict) -> None:
        # Domain-specific persistence is intentionally deterministic. The model
        # proposes records; this code owns IDs, tenant keys, and state changes.
        run_type, company_id, payload = run["run_type"], run["company_id"], run["payload"]
        stamp = now()
        if run_type == "document_processing":
            document_id = payload.get("document_id")
            if document_id:
                # The document's own status is not touched here: it stays
                # `ready` because the file is still perfectly usable. Only the
                # semantic result and the run pointer are recorded.
                self.db.execute(
                    "UPDATE documents SET processing_run_id=?,data=?,updated_at=? "
                    "WHERE id=? AND company_id=?",
                    (run["id"], json_dump({"records": output.get("records", []),
                                            "rejects": output.get("rejects", [])}),
                     stamp, document_id, company_id),
                )
                self._clear_document_records(company_id, document_id)
            for record in output.get("records", []):
                record_type = record.get("record_type") or payload.get("document_type")
                if record_type in {"product", "product_catalog", "technical_sheet"}:
                    name = str(record.get("product_name") or record.get("name") or "").strip()
                    if name:
                        self.db.execute(
                            "INSERT INTO products(id,company_id,name,normalized_name,data,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,normalized_name) DO UPDATE SET "
                            "data=excluded.data,updated_at=excluded.updated_at",
                            (new_id("prd"), company_id, name, normalize_name(name),
                             json_dump({**record, "source_document_id": document_id}), stamp, stamp),
                        )
                elif record_type in {"contact", "current_contacts"}:
                    failures = validate_contact_record(record)
                    contact_id = new_id("con")
                    tenant_evidence = {
                        "evidence_id": f"document-contact:{document_id}:{contact_id}",
                        "source_class": "customer", "tenant_supplied": True,
                    }
                    verification = verify_contact_evidence(record, [tenant_evidence])
                    self.db.execute(
                        "INSERT INTO contacts(id,company_id,lead_id,email,phone,linkedin_url,status,data,"
                        "verification_tier,contact_kind,verification_method,verification_evidence_ids,"
                        "verification_checked_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (contact_id, company_id, record.get("lead_id"), record.get("email"),
                         record.get("phone"), canonical_linkedin_url(record.get("linkedin_url")),
                         "invalid" if failures else "verified" if verification.tier == "green" else "active",
                         json_dump({**record, "source_document_id": document_id,
                                    "validation_failures": failures, "evidence": [tenant_evidence]}),
                         verification.tier, verification.contact_kind, verification.method,
                         json_dump(verification.evidence_ids), verification.checked_at, stamp, stamp),
                    )
                else:
                    section = self.db.one(
                        "SELECT data FROM company_sections WHERE company_id=? AND section='processed_records'",
                        (company_id,),
                    )
                    records = json_load(section["data"], {}).get("records", []) if section else []
                    records.append({**record, "source_document_id": document_id,
                                    "document_type": payload.get("document_type")})
                    self.db.execute(
                        "INSERT INTO company_sections(company_id,section,data,updated_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(company_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                        (company_id, "processed_records", json_dump({"records": records}), stamp),
                    )
        elif run_type == "product_extraction":
            for product in output.get("products", []):
                name = str(product.get("product_name") or product.get("name") or "").strip()
                if not name:
                    continue
                normalized = normalize_name(name)
                self.db.execute(
                    "INSERT INTO products(id,company_id,name,normalized_name,data,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,normalized_name) DO UPDATE SET "
                    "data=excluded.data,updated_at=excluded.updated_at",
                    (new_id("prd"), company_id, name, normalized, json_dump(product), stamp, stamp),
                )
        elif run_type == "company_brain_build":
            row = self.db.one("SELECT COALESCE(MAX(version),0)+1 AS version FROM company_brain_snapshots "
                              "WHERE company_id=?", (company_id,))
            snapshot_id = new_id("brain")
            self.db.execute(
                "INSERT INTO company_brain_snapshots(id,company_id,version,status,content,sources,run_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (snapshot_id, company_id, row["version"], "draft", json_dump(output),
                 json_dump(payload.get("sources", [])), run["id"], stamp),
            )
            self.db.execute("UPDATE agent_runs SET output_ref=? WHERE id=?", (snapshot_id, run["id"]))
        elif run_type == "lead_scan":
            preferences = self.db.one(
                "SELECT data FROM company_sections WHERE company_id=? AND section='sales_preferences'",
                (company_id,),
            )
            excluded = [str(value).casefold() for value in
                        (json_load(preferences["data"], {}).get("excluded_industries", []) if preferences else [])]
            if isinstance(json_load(preferences["data"], {}).get("excluded_industries") if preferences else None, str):
                excluded = [value.strip().casefold() for value in
                            json_load(preferences["data"], {})["excluded_industries"].split(",")]
            requested = {str(code).upper() for code in payload.get("countries", [])}
            per_country: dict[str, int] = {}
            cap = int(payload.get("max_leads_per_country", 50))
            for lead in output.get("leads", []):
                if not lead.get("company_name"):
                    continue
                country = str(lead.get("country") or "").upper()
                if requested and country not in requested:
                    continue
                industry = str(lead.get("industry") or "").casefold()
                if any(keyword and keyword in industry for keyword in excluded):
                    continue
                if per_country.get(country, 0) >= cap:
                    continue
                # ponytail: SQLite lower() is ASCII-only, so Turkish/accented
                # name variants won't dedup here; add a normalized_name column
                # (quality.normalize_name) to leads if duplicate rates matter.
                duplicate = self.db.one(
                    "SELECT id FROM leads WHERE company_id=? AND country=? AND "
                    "(lower(company_name)=lower(?) OR (website IS NOT NULL AND website=?)) LIMIT 1",
                    (company_id, country, lead["company_name"], lead.get("website")),
                )
                if duplicate:
                    continue
                self.db.execute(
                    "INSERT INTO leads(id,company_id,scan_id,company_name,website,country,data,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_id("lead"), company_id, payload.get("scan_id"), lead["company_name"],
                     lead.get("website"), country, json_dump(lead), stamp, stamp),
                )
                per_country[country] = per_country.get(country, 0) + 1
        elif run_type == "lead_research":
            research_id = new_id("res")
            self.db.execute(
                "INSERT INTO research(id,company_id,lead_id,status,insights,run_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (research_id, company_id, payload.get("lead_id"), "succeeded",
                 json_dump(output), run["id"], stamp, stamp),
            )
            self.db.execute("UPDATE agent_runs SET output_ref=? WHERE id=?", (research_id, run["id"]))
        elif run_type == "lead_research_refresh":
            # Imported lazily to preserve the agent_service -> models dependency
            # direction; agentic orchestration itself depends on this service.
            from .lead_research.agentic import AgenticResearchService

            accepted = AgenticResearchService(
                self.db, runs=self,
            ).accept_refresh_output(company_id, run["id"], payload, output)
            self.event(
                run["id"], company_id, "refresh_persisted",
                f"accepted {len(accepted)} refreshed fact(s)",
            )
        elif run_type == "contact_discovery":
            cap = int(payload.get("max_contacts_per_company", 5))
            per_lead: dict[str | None, int] = {}
            for contact in output.get("contacts", []):
                lead_id = contact.get("lead_id") or (payload.get("lead_ids") or [None])[0]
                if per_lead.get(lead_id, 0) >= cap:
                    continue
                if contact.get("email") and self.db.one(
                    "SELECT id FROM contacts WHERE company_id=? AND lower(email)=lower(?) LIMIT 1",
                    (company_id, contact["email"]),
                ):
                    continue
                failures = validate_contact_record(contact)
                blocked = bool(contact.get("do_not_contact"))
                verification = verify_contact_evidence(contact, contact.get("evidence") or [])
                self.db.execute(
                    "INSERT INTO contacts(id,company_id,lead_id,email,phone,linkedin_url,status,do_not_contact,data,"
                    "verification_tier,contact_kind,verification_method,verification_evidence_ids,"
                    "verification_checked_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("con"), company_id, lead_id, contact.get("email"), contact.get("phone"),
                     canonical_linkedin_url(contact.get("linkedin_url")),
                     "blocked" if blocked else "invalid" if failures else
                     "verified" if verification.tier == "green" else "active", int(blocked),
                     json_dump({**contact, "validation_failures": failures,
                                "verification": verification.model_dump(mode="json")}),
                     verification.tier, verification.contact_kind, verification.method,
                     json_dump(verification.evidence_ids), verification.checked_at, stamp, stamp),
                )
                per_lead[lead_id] = per_lead.get(lead_id, 0) + 1
        elif run_type == "outreach_generation":
            recipients = payload.get("recipients") or {}
            selected_language = str(payload.get("language") or "en").strip().lower()
            reported_language = str(output.get("language") or selected_language).strip().lower()
            content = {
                "to": recipients.get("to") or output.get("to"),
                "cc": recipients.get("cc", []),
                "subject": output.get("subject"), "body": output.get("body"),
                # The selected campaign language is authoritative. Model output
                # may be rejected for disagreeing, but never relabels a send.
                "language": selected_language,
            }
            content.update({key: value for key, value in (payload.get("delivery_context") or {}).items()
                            if key in {"country", "reply_to"}})
            preferences = self.db.one(
                "SELECT data FROM company_sections WHERE company_id=? AND section='sales_preferences'",
                (company_id,),
            )
            sales = json_load(preferences["data"], {}) if preferences else {}
            fixed_subject = sales.get("fixed_subject_line")
            if isinstance(fixed_subject, dict):
                fixed_subject = fixed_subject.get(str(content.get("language") or "en").lower())
            verdict = preflight_message(content, fixed_subject=fixed_subject).as_dict()
            if reported_language != selected_language:
                verdict["failures"] = list(dict.fromkeys([
                    *verdict["failures"], "language_mismatch",
                ]))
                verdict["pass"] = False
            message_id = new_id("msg")
            status = "pending_approval" if verdict["pass"] else "qa_failed"
            generation = {
                "language": selected_language,
                "template_version": payload.get("template_version"),
                "run_id": run["id"],
            }
            self.db.execute(
                "INSERT INTO outreach_messages(id,company_id,campaign_id,lead_id,contact_id,channel,status,revision,"
                "content_hash,content,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, company_id, payload.get("campaign_id"), payload.get("lead_id"),
                 payload.get("contact_id"), payload.get("channel", "email"), status, 1,
                 content_hash(content), json_dump(content),
                 json_dump({"qa_verdict": verdict, "generation": generation}), stamp, stamp),
            )
            self.db.execute("UPDATE agent_runs SET output_ref=? WHERE id=?", (message_id, run["id"]))
            # A rewrite retires the message it replaced, but only once the
            # replacement exists — a failed rewrite must leave the original
            # reviewable. Scoped by company_id, and only for messages that have
            # not been approved or delivered, so history is never rewritten.
            superseded = payload.get("supersedes_message_id")
            if superseded and superseded != message_id:
                self.db.execute(
                    "UPDATE outreach_messages SET superseded_by=?,updated_at=? "
                    "WHERE id=? AND company_id=? AND superseded_by IS NULL "
                    "AND status IN ('pending_approval','qa_failed')",
                    (message_id, stamp, superseded, company_id),
                )
        elif run_type == "linkedin_note_generation":
            action_id = new_id("li")
            self.db.execute(
                "INSERT INTO linkedin_actions(id,company_id,lead_id,contact_id,status,profile_url,note,data,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (action_id, company_id, payload.get("lead_id"), payload.get("contact_id"), "generated",
                 output.get("profile_url"), output.get("note"), json_dump({}), stamp, stamp),
            )
            self.db.execute("UPDATE agent_runs SET output_ref=? WHERE id=?", (action_id, run["id"]))

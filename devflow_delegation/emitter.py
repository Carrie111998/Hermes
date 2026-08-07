"""Shared DDP emitter — the ONE delegate() surface for every Hermes producer.

Sequence (spec: "Shared emitter"):
  1. normalize + validate the request
  2. resolve and verify target against the allowlist
  3. compute the dedup fingerprint
  4. apply source threshold, dedup, cooldown, and rate-limit policy
  5. transactionally insert or classify the ledger record
  6. atomically write the mailbox envelope for queued work
  7. emit lifecycle telemetry
  8. return a structured DelegationResult (never raise for policy outcomes)

Failures between steps are reconciled by reconcile() — no LLM repairs
control-plane state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from devflow_delegation import contract
from devflow_delegation.allowlist import Allowlist, AllowlistError, load_allowlist, resolve_target
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.policy import (
    build_policy,
    check_rate_limits,
    evaluate_source_threshold,
    first_rate_limit_crossing,
    in_cooldown,
    load_policy_overrides,
    policy_for,
)


@dataclass
class DelegationResult:
    status: str                      # queued | duplicate | suppressed | declined
    request_id: Optional[str] = None
    fingerprint: Optional[str] = None
    reason: str = ""


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DelegationEmitter:
    def __init__(self, *, ledger=None, allowlist=None, bus=None, inbox_dir=None, policy_map=None):
        from events import paths

        self.ledger = ledger if ledger is not None else DelegationLedger(paths.delegation_ledger_path())
        if allowlist is not None:
            self.allowlist = allowlist
        else:
            try:
                self.allowlist = load_allowlist(paths.devflow_allowlist_path())
            except AllowlistError:
                self.allowlist = Allowlist(version="missing", targets={})  # fail closed
        self.bus = bus if bus is not None else self._make_bus()
        self.inbox_dir = Path(inbox_dir) if inbox_dir is not None else paths.devflow_inbox_dir()
        overrides = load_policy_overrides(paths.devflow_policy_path())
        self.policy_map = policy_map if policy_map is not None else build_policy(overrides)

    @staticmethod
    def _make_bus():
        from events.bus import EventBus

        return EventBus()

    # ------------------------------------------------------------------ delegate
    def delegate(
        self,
        *,
        source,
        kind,
        title,
        problem_statement,
        evidence,
        acceptance_criteria,
        target=None,
        severity,
        priority,
        confidence,
        proposed_approach=None,
        safety_notes: Iterable[str] = (),
        idempotency_key: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> DelegationResult:
        # 1. normalize
        if isinstance(source, str):
            source = {"agent": source}
        source_agent = str(source.get("agent") or "").strip()
        source_kind = str(source.get("kind") or source_agent).strip()
        finding_id = str(source.get("finding_id") or "").strip()

        payload: Dict[str, Any] = {
            "schema_version": contract.SCHEMA_VERSION,
            "type": contract.MSG_TYPE,
            "idempotency_key": idempotency_key or "",
            "source": {"agent": source_agent, "kind": source_kind, "finding_id": finding_id},
            "kind": kind,
            "title": title,
            "problem_statement": problem_statement,
            "evidence": list(evidence or []),
            "acceptance_criteria": list(acceptance_criteria or []),
            "severity": severity,
            "priority": priority,
            "confidence": confidence,
            "proposed_approach": proposed_approach or "",
            "safety_notes": [s for s in (safety_notes or ()) if str(s).strip()],
            "target": target,
        }

        # 1b. validate structure (target/idem handled below)
        errs = contract.validate_payload(payload)
        if errs:
            return DelegationResult("declined", reason="invalid:" + ",".join(errs))

        policy = policy_for(self.policy_map, source_kind)
        effective_mode = mode or policy.mode

        # 2. resolve target (fail closed)
        target_repo = str((target or {}).get("repo") or "").strip()
        target_subsystem = str((target or {}).get("subsystem") or "").strip()
        if not target_repo or not target_subsystem or resolve_target(self.allowlist, target_repo) is None:
            self._record_declined(payload, reason="target_unresolved",
                                  fingerprint="none", target_repo=target_repo or "unresolved",
                                  target_subsystem=target_subsystem or "unresolved")
            return DelegationResult("declined", reason="target_unresolved")

        # 3. fingerprint + default idempotency key
        fingerprint = contract.compute_fingerprint(
            target_repo=target_repo, target_subsystem=target_subsystem,
            kind=kind, title=title, problem_statement=problem_statement)
        if not payload["idempotency_key"]:
            payload["idempotency_key"] = f"auto:{fingerprint}"

        # 4. policy gates
        threshold = evaluate_source_threshold(policy, confidence=float(confidence), severity=severity)
        if not threshold.allowed:
            self._record_declined(payload, reason=threshold.reason, fingerprint=fingerprint,
                                  target_repo=target_repo, target_subsystem=target_subsystem)
            return DelegationResult("declined", reason=threshold.reason)

        # 4b. dedup against an ACTIVE request with the same fingerprint/idem key
        active = self.ledger.find_active_by_fingerprint(fingerprint)
        if active is None:
            existing = self.ledger.find_by_idempotency_key(payload["idempotency_key"])
            if existing is not None and existing["state"] not in {"DECLINED", "SUPPRESSED", "DUPLICATE"}:
                active = existing
        if active is not None:
            for item in payload["evidence"]:
                self.ledger.append_evidence(active["request_id"], item)
            self._emit_lifecycle_event("DUPLICATE", active["request_id"],
                                       extra={"reason": "duplicate_active"})
            return DelegationResult("duplicate", request_id=active["request_id"],
                                    fingerprint=fingerprint, reason="duplicate_active")

        # 4c. terminal cooldown
        last = self.ledger.latest_terminal_for_fingerprint(fingerprint)
        if last is not None and in_cooldown(policy=policy, terminal_state=last["state"],
                                            terminal_updated_at_iso=last["updated_at"], now=_now()):
            reason = "cooldown_success" if last["state"] in {"MERGED", "AUTO_MERGED", "DEPLOYED"} \
                else "cooldown_declined"
            self._record_suppressed(payload, reason=reason, fingerprint=fingerprint,
                                    target_repo=target_repo, target_subsystem=target_subsystem)
            return DelegationResult("suppressed", fingerprint=fingerprint, reason=reason)

        # 4d. rate limits (source, global, critical budget)
        window_start = (_now() - timedelta(hours=policy.window_hours)).isoformat()
        # Capture the PRE-insert source count: the summarized-alert crossing is
        # judged on the count at the moment of the decision, before
        # _record_suppressed adds this request's own SUPPRESSED row (which
        # would otherwise push the count past the limit and mask the crossing).
        source_count = self.ledger.count_since(source_agent, window_start)
        decision = check_rate_limits(
            policy,
            source_count=source_count,
            global_count=self.ledger.count_since(None, window_start),
            critical_used=self.ledger.count_critical_since(window_start),
            is_critical=(severity == "critical"),
        )
        if not decision.allowed:
            suppressed_row = self._record_suppressed(
                payload, reason=decision.reason, fingerprint=fingerprint,
                target_repo=target_repo, target_subsystem=target_subsystem)
            if decision.reason == "rate_limit_source" and first_rate_limit_crossing(
                    source_count, policy.max_per_window):
                self._emit_lifecycle_event(
                    "SUPPRESSED", suppressed_row["request_id"],
                    extra={"reason": decision.reason, "source": source_agent, "summarized": True})
            return DelegationResult("suppressed", fingerprint=fingerprint, reason=decision.reason)

        # 5-8. queue (or dry-run classify)
        if effective_mode != "queue":
            return DelegationResult("queued", fingerprint=fingerprint, reason="dry_run")

        req = contract.parse_request(payload)
        # A prior TERMINAL attempt of this fingerprint may already hold the auto
        # idempotency key: 4b lets terminal rows through so a fingerprint can
        # re-open once its cooldown expires (policy: "DECLINED rows gate
        # re-opens"). The auto key is derived from the fingerprint, so a naive
        # re-open would collide on the UNIQUE idempotency_key and raise
        # sqlite3.IntegrityError out of delegate() — violating "never raise for
        # policy outcomes". Give the re-opened request a per-attempt key so it
        # inserts cleanly; active dedup is fingerprint-based (4b), never idem-key
        # based, so uniqueness of the auto key is not relied on for dedup. Any
        # row found here is necessarily terminal (a non-terminal one would have
        # deduped at 4b), so this only fires on a legitimate post-cooldown reopen.
        if req.idempotency_key.startswith("auto:") and \
                self.ledger.find_by_idempotency_key(req.idempotency_key) is not None:
            req.idempotency_key = f"{req.idempotency_key}:{req.request_id}"
        self.ledger.insert_request(req)
        try:
            self.write_envelope(req)
        except OSError:
            # Durable ledger row exists; the reconcile tick rewrites the
            # envelope. Never insert a second request to compensate.
            return DelegationResult("queued", request_id=req.request_id,
                                    fingerprint=fingerprint, reason="queued_envelope_pending")
        self._emit_work_requested(req)
        return DelegationResult("queued", request_id=req.request_id,
                                fingerprint=fingerprint, reason="queued")

    # ----------------------------------------------------------------- helpers
    def _record_declined(self, payload, *, reason, fingerprint, target_repo, target_subsystem):
        req = self._synthetic_row(payload, state="DECLINED", fingerprint=fingerprint,
                                  target_repo=target_repo, target_subsystem=target_subsystem)
        self._insert_synthetic(req, state="DECLINED", terminal_reason=reason)

    def _record_suppressed(self, payload, *, reason, fingerprint, target_repo, target_subsystem):
        req = self._synthetic_row(payload, state="SUPPRESSED", fingerprint=fingerprint,
                                  target_repo=target_repo, target_subsystem=target_subsystem)
        return self._insert_synthetic(req, state="SUPPRESSED", terminal_reason=reason)

    def _synthetic_row(self, payload, *, state, fingerprint, target_repo, target_subsystem):
        """Build a WorkRequest for declined/suppressed records (they still get
        ledger rows so dedup/cooldown/rate-limit history is durable)."""
        p = dict(payload)
        p["target"] = {"repo": target_repo, "subsystem": target_subsystem}
        if not p.get("idempotency_key"):
            seed = _norm(p["title"])
            p["idempotency_key"] = "unresolved:" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        if not p.get("target", {}).get("subsystem"):
            p["target"]["subsystem"] = "unresolved"
        req = contract.parse_request(p)
        req.dedup_fingerprint = fingerprint
        return req

    def _insert_synthetic(self, req, *, state, terminal_reason):
        existing = self.ledger.find_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return existing
        self.ledger.insert_request(req)
        self.ledger.set_state(req.request_id, state, terminal_reason=terminal_reason)
        return self.ledger.get_request(req.request_id)

    def write_envelope(self, req) -> Path:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        ts = _now().strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9-]+", "-", req.source_agent.lower()).strip("-") or "source"
        final = self.inbox_dir / f"{ts}_{contract.MSG_TYPE}_{slug}_{req.request_id}.json"
        tmp = final.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(req.to_envelope(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)  # atomic
        return final

    def _emit_work_requested(self, req) -> None:
        from events.schema import EventType

        self.bus.emit(
            event_type=EventType.DEVFLOW_WORK_REQUESTED,
            source="ddp.emitter",
            payload={
                "request_id": req.request_id,
                "idempotency_key": req.idempotency_key,
                "fingerprint": req.dedup_fingerprint,
                "source_agent": req.source_agent,
                "source_kind": req.source_kind,
                "kind": req.kind,
                "title": req.title,
                "target": {"repo": req.target_repo, "subsystem": req.target_subsystem},
                "severity": req.severity,
                "priority": req.priority,
            },
            correlation_id=req.request_id,
        )

    def _emit_lifecycle_event(self, to_state, request_id, *, extra=None) -> None:
        from devflow_delegation.lifecycle import STATE_EVENTS

        et = STATE_EVENTS.get(to_state)
        if et is None or self.bus is None:
            return
        payload = {"request_id": request_id, "to_state": to_state}
        if extra:
            payload.update(extra)
        self.bus.emit(event_type=et, source="ddp.emitter", payload=payload,
                      correlation_id=request_id)

    # --------------------------------------------------------------- reconcile
    def reconcile(self) -> Dict[str, int]:
        """Deterministic reconciliation of ledger <-> mailbox partial failures.

        - adopt: envelope in inbox with no ledger row -> insert it (idempotent)
        - rewrite: REQUESTED ledger row with no envelope file -> rewrite it
        Never inserts a second request for an existing identity.
        """
        adopted = rewritten = 0
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        for fp in sorted(self.inbox_dir.glob("*.json")):
            try:
                env = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if env.get("type") == "DEVFLOW_FIX_REQUEST":
                try:
                    env = contract.parse_v2_fix_request(env)
                    req = contract.parse_request(env)
                except contract.ContractError:
                    continue
                env = req.to_envelope()
            if env.get("type") != contract.MSG_TYPE:
                continue
            idem = env.get("idempotency_key")
            if idem and self.ledger.find_by_idempotency_key(idem) is None:
                try:
                    self.ledger.adopt_envelope(env)
                    adopted += 1
                except Exception:
                    continue

        on_disk = {f.name for f in self.inbox_dir.glob("*.json")}
        for row in self.ledger.list_requests(state="REQUESTED", limit=1000):
            if not any(row["request_id"] in name for name in on_disk):
                env = json.loads(row["envelope_json"])
                req = contract.parse_request(env)
                req.request_id = row["request_id"]
                req.created_at = row["created_at"]
                try:
                    self.write_envelope(req)
                    rewritten += 1
                except OSError:
                    continue
        return {"adopted": adopted, "rewritten": rewritten}

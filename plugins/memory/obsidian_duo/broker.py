"""Embedded, service-ready Memory Duo broker orchestration."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from .config import ObsidianDuoConfig
from .contracts import (
    Authority,
    BrokerStatus,
    CandidateDecision,
    MemoryCandidate,
    MemoryEvent,
    Verification,
    MemoryPacket,
    RetrievalRequest,
    MemoryRecord,
    MemoryStatus,
)
from .policy import MemoryPolicy
from .retrieval import MemoryRetriever
from .store import SqliteMemoryStore
from .vault import ObsidianVault
from .store import new_id
from .security import assert_candidate_safe_to_persist, redact_secrets


@dataclass(frozen=True)
class RecoveryResult:
    recovered: int = 0
    malformed: int = 0


class EmbeddedMemoryBroker:
    def __init__(self, *, config: ObsidianDuoConfig, store: SqliteMemoryStore, vault: ObsidianVault, policy: MemoryPolicy, retriever: MemoryRetriever, inference=None, sync_adapter=None):
        self.config = config
        self.store = store
        self.vault = vault
        self.policy = policy
        self.retriever = retriever
        self.inference = inference
        self.sync_adapter = sync_adapter
        self._events: queue.Queue = queue.Queue(maxsize=config.queue_maxsize)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = "UNAVAILABLE"
        self._state_lock = threading.Lock()
        self._event_buffers: dict[str, list[MemoryEvent]] = {}
        self._event_buffer_lock = threading.Lock()
        self._last_managed_scan = 0.0
        self._last_external_catalog_refresh = 0.0

    def start(self) -> None:
        self.store.initialize()
        self.vault.ensure_managed_structure()
        self.recover()
        if self.config.index_mode in {"lazy", "managed_first"}:
            self.vault.refresh_external_catalog(self.store)
            self._last_external_catalog_refresh = time.monotonic()
        self._state = "READY"

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._worker_loop, name="hermes-memory-broker", daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    event = self._events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if event is None:
                    self._events.task_done()
                    break
                try:
                    self.store.metrics_increment(f"event.{event.event_type}")
                    self._retain_event(event)
                    if event.event_type in {"task_complete", "session_end", "delegation_result"}:
                        if event.event_type == "session_end" and not event.session_id:
                            with self._event_buffer_lock:
                                buffers = [
                                    (session_key, list(retained))
                                    for session_key, retained in self._event_buffers.items()
                                ]
                            for session_key, retained in buffers:
                                if retained:
                                    self.consolidate(event.event_type, retained)
                                with self._event_buffer_lock:
                                    self._event_buffers.pop(session_key, None)
                        else:
                            session_key = event.session_id or "__default__"
                            with self._event_buffer_lock:
                                retained = list(self._event_buffers.get(session_key, ()))
                            if retained:
                                self.consolidate(event.event_type, retained)
                                with self._event_buffer_lock:
                                    self._event_buffers.pop(session_key, None)
                finally:
                    self._events.task_done()
        finally:
            self.store.close()

    def observe(self, event: MemoryEvent) -> None:
        self.store.initialize()
        self._ensure_worker()
        try:
            self._events.put_nowait(event)
            return
        except queue.Full:
            pass

        important = event.event_type in {"user_correction", "explicit_remember", "decision_confirmed"}
        if not important:
            self.store.metrics_increment("events.dropped")
            return
        with self._events.mutex:
            kept = []
            dropped = False
            while self._events.queue:
                current = self._events.queue.popleft()
                if not dropped and current.event_type in {"turn", "session_end"}:
                    dropped = True
                    self._events.unfinished_tasks -= 1
                    continue
                kept.append(current)
            self._events.queue.extend(kept)
            if dropped:
                self._events.queue.append(event)
                self._events.unfinished_tasks += 1
            else:
                self.store.metrics_increment("events.deferred")

    def _retain_event(self, event: MemoryEvent) -> None:
        """Keep only bounded, non-secret lifecycle context for later consolidation."""
        content = redact_secrets((event.content or "")[:4000])
        if not content and event.event_type == "turn":
            return
        retained = MemoryEvent(
            event_type=event.event_type,
            content=content,
            mission_id=event.mission_id,
            task_id=event.task_id,
            agent_id=event.agent_id,
            parent_agent_id=event.parent_agent_id,
            workspace_id=event.workspace_id,
            project_id=event.project_id,
            session_id=event.session_id,
            metadata=dict(event.metadata),
        )
        session_key = event.session_id or "__default__"
        with self._event_buffer_lock:
            buffer = self._event_buffers.setdefault(session_key, [])
            buffer.append(retained)
            del buffer[:-32]

    def retrieve(self, request: RetrievalRequest) -> MemoryPacket:
        self.store.initialize()
        now = time.monotonic()
        if now - self._last_managed_scan >= self.config.managed_scan_min_interval_seconds:
            self.vault.scan_managed_changes(self.store)
            self.process_manual_changes()
            self._last_managed_scan = now
        if self.config.index_mode not in {"lazy", "managed_first"}:
            return self.retriever.retrieve(request)
        now = time.monotonic()
        if now - self._last_external_catalog_refresh >= self.config.external_catalog_refresh_seconds:
            self.vault.refresh_external_catalog(self.store)
            self._last_external_catalog_refresh = now
        packet = self.retriever.retrieve(request)
        if packet.memories:
            return packet
        if self.vault.index_external_paths(
            self.store,
            limit=self.config.external_index_batch_size,
            query=request.query,
        ):
            return self.retriever.retrieve(request)
        return packet

    def propose(self, candidate: MemoryCandidate, *, host_confirmed: bool = False, auto_promote: bool = False) -> CandidateDecision:
        try:
            assert_candidate_safe_to_persist(candidate)
        except ValueError:
            return CandidateDecision("reject", reason="secret credentials detected")
        try:
            self.vault._canonical_folder(candidate.memory_type)
        except ValueError:
            return CandidateDecision("reject", reason="unsupported memory_type")
        if not host_confirmed and not auto_promote:
            candidate = replace(
                candidate,
                authority=Authority.AGENT,
                verification=Verification.UNVERIFIED,
                metadata={**dict(candidate.metadata), "event_kind": "tool_proposal"},
            )
        elif auto_promote:
            if candidate.authority is Authority.USER:
                candidate = replace(candidate, authority=Authority.AGENT)
            if candidate.verification is Verification.USER_CONFIRMED:
                candidate = replace(candidate, verification=Verification.INFERRED)
        existing = self.store.hot_memory_candidates(limit=max(32, self.config.recall_max_memories * 2))
        decision = self.policy.merge_or_conflict(existing, candidate)
        if decision.action in {"promote", "supersede"}:
            memory_id = str(candidate.metadata.get("memory_id") or new_id("memory"))
            record = MemoryRecord(
                memory_id=memory_id,
                content=candidate.content,
                memory_type=candidate.memory_type,
                scope=candidate.scope,
                authority=candidate.authority,
                verification=candidate.verification,
                confidence=float(candidate.metadata.get("confidence", 0.0) or 0.0),
                importance=float(candidate.metadata.get("importance", 0.0) or 0.0),
                evidence_ids=tuple(item.evidence_id for item in candidate.evidence),
                source_session_id=str(candidate.metadata.get("source_session_id") or ""),
                task_id=str(candidate.metadata.get("task_id") or ""),
                project_id=str(candidate.metadata.get("project_id") or ""),
                child_session_id=str(candidate.metadata.get("child_session_id") or ""),
                mission_id=str(candidate.metadata.get("mission_id") or ""),
                agent_id=str(candidate.metadata.get("agent_id") or ""),
                relationships=(
                    (f"supersedes:{decision.memory_id}",)
                    if decision.action == "supersede" and decision.memory_id
                    else ()
                ),
            )
            txn_id = new_id("txn")
            path = self.vault._managed_path(record)
            payload = {"path": str(path), "memory_id": memory_id}
            self.store.record_journal(txn_id, "promote", "prepared", payload)
            self.vault.write_managed_note(record)
            stat = path.stat()
            content_hash = self.vault._hash(path)
            payload.update({"content_hash": content_hash, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
            self.store.record_journal(txn_id, "promote", "written", payload)
            self.store.upsert_memory(record, "promotion")
            if decision.action == "supersede" and decision.memory_id:
                old = self.store.get_memory(decision.memory_id)
                if old is not None:
                    self.store.upsert_memory(
                        replace(old, status=MemoryStatus.SUPERSEDED),
                        "superseded by user correction",
                    )
                    self.store.record_relationship(
                        record.memory_id, old.memory_id, "supersedes", {"reason": decision.reason}
                    )
            for evidence in candidate.evidence:
                self.store.insert_evidence(evidence)
                self.store.link_evidence(memory_id, evidence.evidence_id)
            self.store.set_note_index(str(path), memory_id, stat.st_mtime_ns, stat.st_size, content_hash)
            self.store.record_journal(txn_id, "promote", "indexed", payload)
            self.store.record_journal(txn_id, "promote", "committed", payload)
            if self.sync_adapter is not None and hasattr(self.sync_adapter, "mark_dirty"):
                self.sync_adapter.mark_dirty("promotion")
            return CandidateDecision("promote", memory_id=memory_id, reason=decision.reason)
        if decision.action == "conflict":
            candidate_id = self.store.stage_candidate(candidate)
            if decision.memory_id:
                self.store.record_conflict(decision.memory_id, candidate_id, decision.reason)
            return decision
        if decision.action == "stage":
            self.store.stage_candidate(candidate)
        return decision

    def flush(self, reason: str, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._events.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        if self.sync_adapter is not None:
            result = self.sync_adapter.flush()
            if not result.success:
                self._state = "DEGRADED"
                return False
        return True

    def recover(self) -> RecoveryResult:
        self._state = "RECOVERING"
        self.store.initialize()
        scan = self.vault.scan_managed_changes(self.store)
        recovered = 0
        conn = self.store.connection()
        rows = conn.execute(
            "SELECT txn_id FROM journal WHERE state IN ('prepared','written','indexed')"
        ).fetchall()
        for row in rows:
            journal = conn.execute("SELECT * FROM journal WHERE txn_id=?", (row["txn_id"],)).fetchone()
            try:
                import json
                payload = json.loads(journal["payload"])
                path = Path(payload["path"])
                if not path.is_file() or (payload.get("content_hash") and self.vault._hash(path) != payload["content_hash"]):
                    raise ValueError("managed note missing or changed")
                memory_id = str(payload["memory_id"])
                if self.store.get_memory(memory_id) is None:
                    self.vault.scan_managed_changes(self.store)
                indexed = conn.execute("SELECT 1 FROM note_index WHERE path=? AND memory_id=?", (str(path), memory_id)).fetchone()
                if self.store.get_memory(memory_id) is None or indexed is None:
                    raise ValueError("durable index incomplete")
                self.store.record_journal(row["txn_id"], journal["operation"], "committed", {**payload, "recovered": True})
                recovered += 1
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
                self.store.record_journal(row["txn_id"], journal["operation"], "recovery_failed", {"reason": str(exc)[:120]})
        self.process_manual_changes()
        self._state = "READY"
        return RecoveryResult(recovered=recovered, malformed=len(scan.malformed_paths))

    def process_manual_changes(self) -> int:
        """Index changed managed notes as user corrections without rewriting them."""
        changed = 0
        conn = self.store.connection()
        for path in sorted(self.vault.managed_root.rglob("*.md")):
            stat = path.stat()
            previous = conn.execute(
                "SELECT memory_id,mtime_ns,size,content_hash,parse_status FROM note_index WHERE path=?",
                (str(path),),
            ).fetchone()
            if previous is None:
                continue
            if previous["parse_status"] not in {"manual_pending", "needs_attention"} and previous["mtime_ns"] == stat.st_mtime_ns and previous["size"] == stat.st_size:
                continue
            content_hash = self.vault._hash(path)
            if content_hash == previous["content_hash"] and previous["parse_status"] != "manual_pending":
                continue
            try:
                parsed = self.vault.parse_note(path)
                old = self.store.get_memory(previous["memory_id"] or parsed.memory_id)
                if old is None:
                    continue
                from .security import assert_safe_to_persist
                assert_safe_to_persist(parsed.body)
                self.vault._canonical_folder(parsed.metadata.get("memory_type") or old.memory_type)
                updated = self.policy.apply_user_edit(old, parsed)
                self.store.upsert_memory(updated, "manual user edit")
                self.store.set_note_index(str(path), updated.memory_id, stat.st_mtime_ns, stat.st_size, content_hash)
                if self.sync_adapter is not None and hasattr(self.sync_adapter, "mark_dirty"):
                    self.sync_adapter.mark_dirty("manual_edit")
                changed += 1
            except Exception:
                self.store.set_note_index(
                    str(path), previous["memory_id"], stat.st_mtime_ns, stat.st_size,
                    content_hash, "needs_attention"
                )
        return changed

    def consolidate(self, reason: str, events: list[MemoryEvent]) -> int:
        lifecycle_types = {"task_complete", "session_end", "delegation_result"}
        if reason == "session_end":
            lifecycle_types |= {"turn", "user_correction", "explicit_remember", "decision_confirmed"}
        retained = [event for event in events if event.event_type in lifecycle_types][:32]
        if self.inference is not None and retained:
            evidence = self._event_evidence(retained)
            result = self.inference.consolidate(retained, evidence)
            if getattr(result, "deferred", False) or not result.parsed:
                for event in retained:
                    self.propose(MemoryCandidate(
                        event.content,
                        metadata={"reason": reason, "event_kind": event.event_type},
                    ))
                return len(retained)
            candidates = result.parsed.get("candidates", []) if result.parsed else []
            for item in candidates:
                if isinstance(item, dict) and item.get("content"):
                    self.propose(
                        self._candidate_from_extraction(item, retained, evidence, reason),
                        auto_promote=True,
                    )
            return len(candidates)
        for event in retained:
            self.propose(MemoryCandidate(event.content, metadata={"reason": reason, "event_kind": event.event_type}))
        return len(retained)

    @staticmethod
    def _event_evidence(events: list[MemoryEvent]):
        import hashlib
        from .contracts import EvidenceRecord

        return [EvidenceRecord(
            evidence_id="event_" + hashlib.sha256(
                f"{event.session_id}|{event.event_type}|{event.content}".encode()
            ).hexdigest()[:20],
            kind=event.event_type,
            content=event.content,
            source="memory_duo",
            session_id=event.session_id,
        ) for event in events if event.content]

    @staticmethod
    def _candidate_from_extraction(item, events, evidence, reason):
        from .contracts import Verification

        try:
            verification = Verification(str(item.get("verification") or Verification.INFERRED.value))
        except ValueError:
            verification = Verification.INFERRED
        if verification is Verification.USER_CONFIRMED:
            verification = Verification.INFERRED
        confidence = float(item.get("confidence") or 0.0)
        evidence_ids = {str(value) for value in item.get("evidence_ids", ()) if value}
        selected = tuple(record for record in evidence if not evidence_ids or record.evidence_id in evidence_ids)
        auto_safe = (
            verification in {Verification.SOURCE_SUPPORTED, Verification.DIRECTLY_OBSERVED}
            and confidence >= 0.75
            and bool(selected)
        )
        metadata = {
            "reason": reason,
            "event_kind": "auto_consolidated" if auto_safe else "turn",
            "confidence": confidence,
            "source_session_id": str(item.get("source_session_id") or next((event.session_id for event in events if event.session_id), "")),
            "task_id": str(item.get("task_id") or next((event.task_id for event in events if event.task_id), "")),
            "project_id": str(item.get("project_id") or next((event.project_id for event in events if event.project_id), "")),
            "mission_id": str(item.get("mission_id") or next((event.mission_id for event in events if event.mission_id), "")),
            "agent_id": str(item.get("agent_id") or next((event.agent_id for event in events if event.agent_id), "")),
        }
        return MemoryCandidate(
            str(item["content"]),
            memory_type=str(item.get("memory_type") or "fact"),
            scope=str(item.get("scope") or "global"),
            authority=Authority.SOURCE if auto_safe else Authority.AGENT,
            verification=verification,
            evidence=selected,
            metadata=metadata,
        )

    def status(self) -> BrokerStatus:
        return BrokerStatus(
            state=self._state,
            indexed_notes=self.store.connection().execute("SELECT COUNT(*) FROM note_index").fetchone()[0],
            pending_events=self._events.unfinished_tasks,
            incomplete_transactions=self.store.connection().execute(
                "SELECT COUNT(*) FROM journal WHERE state != 'committed'"
            ).fetchone()[0],
        )

    def shutdown(self, timeout: float) -> None:
        self.flush("shutdown", timeout)
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=max(0.0, timeout))
        self.store.close()

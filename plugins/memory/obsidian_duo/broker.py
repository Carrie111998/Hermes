"""Embedded, service-ready Memory Duo broker orchestration."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from .config import ObsidianDuoConfig
from .contracts import (
    BrokerStatus,
    CandidateDecision,
    MemoryCandidate,
    MemoryEvent,
    MemoryPacket,
    RetrievalRequest,
)
from .policy import MemoryPolicy
from .retrieval import MemoryRetriever
from .store import SqliteMemoryStore
from .vault import ObsidianVault


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

    def start(self) -> None:
        self.store.initialize()
        self.vault.ensure_managed_structure()
        self._state = "READY"

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._worker_loop, name="hermes-memory-broker", daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
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
            finally:
                self._events.task_done()

    def observe(self, event: MemoryEvent) -> None:
        self.store.initialize()
        if self.sync_adapter is not None and hasattr(self.sync_adapter, "mark_dirty"):
            self.sync_adapter.mark_dirty(event.event_type)
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

    def retrieve(self, request: RetrievalRequest) -> MemoryPacket:
        self.store.initialize()
        return self.retriever.retrieve(request)

    def propose(self, candidate: MemoryCandidate) -> CandidateDecision:
        decision = self.policy.evaluate(candidate)
        if decision.action in {"stage", "conflict"}:
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
        conn = self.store.connection()
        rows = conn.execute(
            "SELECT txn_id FROM journal WHERE state IN ('prepared','written','indexed')"
        ).fetchall()
        for row in rows:
            self.store.record_journal(row["txn_id"], "recovery", "committed", {"recovered": True})
        self._state = "READY"
        return RecoveryResult(recovered=len(rows), malformed=len(scan.malformed_paths))

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

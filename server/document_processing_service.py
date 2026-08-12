"""Background lifecycle for turning an uploaded document into a usable one.

The upload route returns as soon as the original is durable; this coordinator
does the work behind it. Its whole job is keeping two things apart:

* what the customer sees — ``uploaded`` → ``processing`` → ``ready`` /
  ``needs_attention`` / ``failed``, with a sentence that tells them what to do;
* what an operator sees — the reason code and sanitized diagnostic, recorded on
  the attempt row and nowhere a customer response can reach.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from agent.document_processing import ProcessingDisposition, process_document

from .document_artifacts import AttemptRecord, DocumentArtifactRepository
from .observability import log

__all__ = ["DocumentProcessingService", "PUBLIC_FAILURES"]

# reason_code → (customer-visible status, customer-visible sentence).
# The copy names the file and the next action, never the machinery: no
# processor names, no "conversion", no "OCR", no "Markdown".
PUBLIC_FAILURES: dict[str, tuple[str, str]] = {
    "encrypted": (
        "failed",
        "We couldn't process this file. Please upload an unlocked copy or try another format.",
    ),
    "advanced_processing_unavailable": (
        "needs_attention",
        "This file needs attention before it can be used.",
    ),
    "no_extractable_text": (
        "needs_attention",
        "We couldn't read any text from this file. Please upload a text-based version.",
    ),
    "resource_limit": (
        "failed",
        "This file is too large or too complex to process. Please upload a smaller version.",
    ),
    "malformed": (
        "failed",
        "We couldn't read this file. Please re-save it and upload it again.",
    ),
    "unsupported_format": (
        "failed",
        "We can't use this file type. Please upload a document, spreadsheet, or PDF.",
    ),
    "undecodable_text": (
        "failed",
        "We couldn't read this file. Please save it as UTF-8 text and upload it again.",
    ),
    "processing_timeout": (
        "failed",
        "This file took too long to process. Please upload a smaller version.",
    ),
    "output_too_large": (
        "needs_attention",
        "This file needs attention before it can be used.",
    ),
}

_FALLBACK_FAILURE = ("failed", "We couldn't process this file. Please try uploading it again.")


class DocumentProcessingService:
    def __init__(
        self,
        repository: DocumentArtifactRepository,
        *,
        workers: int = 2,
        timeout_seconds: float = 180,
        max_output_bytes: int = 50 * 1024 * 1024,
    ):
        self.repository = repository
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        # Swappable so tests can drive dispositions directly, and so a future
        # surface can supply a different local processor without a subclass.
        self.processor = process_document
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)), thread_name_prefix="doc-processing"
        )
        # ponytail: one lock over the whole settled map — contention here is a
        # dict write per document, orders of magnitude below the conversion.
        self._lock = threading.Lock()
        self._settled: dict[tuple[str, str], AttemptRecord] = {}
        self._events: dict[tuple[str, str], threading.Event] = {}
        self._closed = False

    # ── public API ─────────────────────────────────────────────────────────

    def submit(self, company_id: str, document_id: str, *, force: bool = False) -> AttemptRecord:
        """Queue processing and return the freshly opened attempt."""
        if self._closed:
            raise RuntimeError("document processing service is shut down")

        attempt = self.repository.start_attempt(company_id, document_id)
        key = (company_id, document_id)
        with self._lock:
            self._settled.pop(key, None)
            event = self._events.setdefault(key, threading.Event())
            event.clear()
        self._pool.submit(self._process, company_id, document_id, attempt, force)
        return attempt

    def retry(self, company_id: str, document_id: str) -> AttemptRecord:
        """Operator-triggered reprocessing. Always redoes the work."""
        return self.submit(company_id, document_id, force=True)

    def wait_until_settled(
        self, company_id: str, document_id: str, timeout: float = 30
    ) -> AttemptRecord | None:
        key = (company_id, document_id)
        with self._lock:
            event = self._events.setdefault(key, threading.Event())
        if not event.wait(timeout):
            return None
        with self._lock:
            return self._settled.get(key)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ── worker ─────────────────────────────────────────────────────────────

    def _process(
        self, company_id: str, document_id: str, attempt: AttemptRecord, force: bool
    ) -> None:
        try:
            self._run_attempt(company_id, document_id, attempt, force)
        except BaseException as exc:  # noqa: BLE001 — a worker must never die silently
            log(
                f"document processing crashed for {document_id}: {type(exc).__name__}",
                logging.ERROR,
            )
            self._settle(company_id, document_id, attempt, "worker_error", str(type(exc).__name__))

    def _run_attempt(
        self, company_id: str, document_id: str, attempt: AttemptRecord, force: bool
    ) -> None:
        original = self.repository.get_original(company_id, document_id)
        if original is None:
            self._settle(company_id, document_id, attempt, "missing_original", None)
            return

        # An identical input that already produced a verified sidecar needs no
        # second conversion — re-promote it and settle.
        reusable = self.repository.get_reusable_processed(
            company_id, document_id, original.checksum, force=force
        )
        if reusable is not None:
            self._finish_ready(company_id, document_id, attempt, reusable.id)
            return

        try:
            path = self.repository.materialize(company_id, original.id)
        except (LookupError, IOError) as exc:
            self._settle(company_id, document_id, attempt, "unreadable_source", str(exc))
            return

        started = time.monotonic()
        # A dedicated single-slot executor, NOT self._pool: this thread is
        # already one of the pool's workers, so submitting the conversion back
        # into the same pool deadlocks the moment workers == 1. Concurrency is
        # bounded by self._pool; this executor only exists to make the
        # conversion abandonable when it overruns the timeout.
        runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc-convert")
        try:
            future = runner.submit(self.processor, path=path, filename=original.filename)
            try:
                result = future.result(timeout=self.timeout_seconds)
            except FutureTimeout:
                # The thread is not killable. Stop waiting on it and let it die
                # on its own; whatever it eventually returns is discarded.
                self._settle(company_id, document_id, attempt, "processing_timeout", None)
                return
            except BaseException as exc:  # noqa: BLE001
                self._settle(company_id, document_id, attempt, "conversion_failed",
                             type(exc).__name__)
                return
        finally:
            runner.shutdown(wait=False)

        if not result.ok:
            self._settle(
                company_id, document_id, attempt,
                result.reason_code or "conversion_failed", result.diagnostic,
            )
            return

        markdown = result.markdown or ""
        if not markdown.strip():
            self._settle(company_id, document_id, attempt, "no_extractable_text", None)
            return
        if len(markdown.encode("utf-8")) > self.max_output_bytes:
            self._settle(company_id, document_id, attempt, "output_too_large", None)
            return

        processed = self.repository.store_processed(
            company_id, document_id, attempt.id, markdown,
            metadata={
                "source_format": result.source_format,
                "used_fallback": result.used_fallback,
                "disposition": result.disposition.value,
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        )
        self._finish_ready(company_id, document_id, attempt, processed.id)

    # ── settlement ─────────────────────────────────────────────────────────

    def _finish_ready(
        self, company_id: str, document_id: str, attempt: AttemptRecord, artifact_id: str
    ) -> None:
        settled = self.repository.finish_attempt(
            company_id, attempt.id, "ready", processed_artifact_id=artifact_id
        )
        self._publish(company_id, document_id, settled)

    def _settle(
        self,
        company_id: str,
        document_id: str,
        attempt: AttemptRecord,
        reason_code: str,
        diagnostic: str | None,
    ) -> None:
        """Close a failed attempt without disturbing any promoted artifact."""
        public_status, public_message = PUBLIC_FAILURES.get(reason_code, _FALLBACK_FAILURE)
        settled = self.repository.finish_attempt(
            company_id,
            attempt.id,
            public_status,
            reason_code=reason_code,
            diagnostic=diagnostic,
            public_message=public_message,
        )
        self._publish(company_id, document_id, settled)

    def _publish(self, company_id: str, document_id: str, settled: AttemptRecord) -> None:
        key = (company_id, document_id)
        with self._lock:
            self._settled[key] = settled
            self._events.setdefault(key, threading.Event()).set()

"""Native-Postgres job-state reader for the applier pre-flight (Fix A).

The applier's Fix A pre-flight ([`IntentApplier._already_satisfied`]) asks a
single question — *what business_state is this job in right now?* — and uses it
to suppress redundant no-op intents before they POST to the congestion-prone
jobflow-api (:4100). We answer it from the **native** Postgres (:5432, fast,
authoritative) rather than :4100, precisely so the check never adds load to the
service it is trying to protect.

Contract: callable ``reader(job_id) -> Optional[str]``.
  * returns the ``current_business_state`` string for the job, or
  * ``None`` when the job is unknown OR the read failed for any reason.

FAIL-SOFT is the whole point: a ``None`` makes the applier fall through to the
normal dual-write path, so a Postgres hiccup can only ever *disable the
optimization*, never block a real transition. To avoid paying a connect-timeout
on every intent while Postgres is down, a failed read arms a short cooldown
during which calls return ``None`` immediately.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DSN = "postgres://jobflow:jobflow@127.0.0.1:5432/jobflow"

# job_id in intents is the jobs.external_job_key (full UUID form).
_QUERY = "select current_business_state from jobs where external_job_key = %s limit 1"


class NativePgJobStateReader:
    """Lazy, reconnecting, fail-soft reader of jobs.current_business_state.

    Holds one autocommit connection, reopened on demand. Any error resets the
    connection and arms ``down_cooldown_seconds`` of immediate-``None`` returns
    so a Postgres outage doesn't cost a connect-timeout per intent.
    """

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        connect_timeout: float = 2.0,
        down_cooldown_seconds: float = 30.0,
    ) -> None:
        self._dsn = dsn or os.environ.get("HERMES_JOBFLOW_PG_DSN", _DEFAULT_DSN)
        self._connect_timeout = connect_timeout
        self._down_cooldown_seconds = down_cooldown_seconds
        self._conn = None
        self._down_until = 0.0

    def _connect(self):
        import psycopg  # imported lazily so a missing driver disables Fix A, not the gateway

        return psycopg.connect(
            self._dsn, connect_timeout=self._connect_timeout, autocommit=True
        )

    def _reset(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def __call__(self, job_id: str) -> Optional[str]:
        now = time.monotonic()
        if now < self._down_until:
            return None
        try:
            if self._conn is None or getattr(self._conn, "closed", False):
                self._conn = self._connect()
            with self._conn.cursor() as cur:
                cur.execute(_QUERY, (job_id,))
                row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            self._reset()
            self._down_until = time.monotonic() + self._down_cooldown_seconds
            logger.debug(
                "job-state-reader: read failed for job=%s; pre-flight disabled "
                "for %.0fs (fail-soft)", job_id, self._down_cooldown_seconds,
                exc_info=True,
            )
            return None


def build_default_reader() -> Optional[NativePgJobStateReader]:
    """Construct the reader, or ``None`` if the psycopg driver is unavailable.

    Returning ``None`` (rather than raising) keeps the subscriber registerable
    on minimal installs without a Postgres driver — Fix A simply stays off.
    """
    try:
        import psycopg  # noqa: F401
    except Exception:
        logger.info(
            "job-state-reader: psycopg not importable; applier pre-flight (Fix A) disabled"
        )
        return None
    return NativePgJobStateReader()

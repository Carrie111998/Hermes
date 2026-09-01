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

# No password: the loopback server authenticates this user without one, so an
# embedded credential bought nothing and shipped to a PUBLIC fork
# (github.com/daragao3/hermes-agent) where a 2026-09-01 exposure sweep had to
# read and clear it by hand. A deployment that genuinely needs a password
# supplies the whole DSN through HERMES_JOBFLOW_PG_DSN below.
_DEFAULT_DSN = "postgres://jobflow@127.0.0.1:5432/jobflow"

# An intent's job_id is the value the applier POSTs to :4100 as /jobs/<id> --
# that is jobs.id. It is NOT jobs.external_job_key: those two columns disagree on
# every one of the 5389 live rows (measured 2026-08-24). Matching external_job_key
# alone therefore either matched nothing, leaving the pre-flight inert, or -- for
# the 53 rows whose external_job_key equals some OTHER row's id -- matched a
# DIFFERENT job and let that stranger's business state decide a real intent's
# fate. Every one of the 15 times this pre-flight has ever fired in production it
# was reading the wrong row, and five operator approvals were silently discarded
# on 2026-08-23 as a result.
#
# Resolve the way the WRITE path resolves, or the pre-flight is not answering the
# question the caller is asking. jobs.id wins. external_job_key remains a
# fallback because some producers really do address a job by its external key
# (observed: State Street 9a668393), but it can never outrank an id match --
# ORDER BY matched_by_id DESC puts the id row first whenever both exist.
_QUERY = """
    select current_business_state, (id::text = %s) as matched_by_id
    from jobs
    where id::text = %s or external_job_key = %s
    order by matched_by_id desc
    limit 1
"""

# Bound the STATEMENT phase, not just connect.
#
# ``connect_timeout`` below already bounds phase one, but psycopg has no default
# timeout on phase two: once connected, a query blocks forever. A container that
# accepts the TCP connection and then stops servicing queries -- the exact
# disk-starvation shape that wedged cron.postgres.sync for nine consecutive
# ~61-minute runs on 2026-08-14 -- would therefore hang ``cur.execute`` here with
# no ceiling at all. This reader runs INSIDE the gateway process, on the
# tracker-intent-applier event-bus subscriber, so that hang would stall the
# subscriber rather than one cron tick, and the module docstring's fail-soft
# contract ("a Postgres hiccup can only ever disable the optimization, never
# block a real transition") would be silently false.
#
# Budget derived from this query's own measured cost against the live DB: the
# single indexed external_job_key lookup returns in ~0.0015s. 2000ms is over a
# thousand times that, and deliberately mirrors the 2.0s connect budget -- both
# phases now cost at most the same bounded amount before the reader gives up,
# returns None, and arms the existing cooldown. Env-overridable.
_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("HERMES_JOBFLOW_PG_STATEMENT_TIMEOUT_MS", "2000")
)


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
            self._dsn,
            connect_timeout=self._connect_timeout,
            autocommit=True,
            # Server-side, so it covers every statement on the session without
            # touching the call site. Exceeding it raises, which the caller's
            # broad ``except Exception`` already turns into the fail-soft
            # ``None`` + cooldown path.
            options="-c statement_timeout=" + str(_STATEMENT_TIMEOUT_MS),
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
                cur.execute(_QUERY, (job_id, job_id, job_id))
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

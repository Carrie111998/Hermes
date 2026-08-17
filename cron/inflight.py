"""In-flight cron accessors for the gateway shutdown path.

``gateway/run.py`` has imported :func:`current_inflight_correlation_ids` from
this module since 2026-04-30 (M1, the GATEWAY_STARTED / GATEWAY_STOPPED
lifecycle events), inside a ``try/except`` that falls back to ``[]``. The module
never existed, so every GATEWAY_STOPPED event ever emitted carried an empty
``inflight_cron_correlation_ids`` list — a restart could not say which crons it
had killed, and a cron cut short by a shutdown was indistinguishable from one
that wedged (it surfaced ~20 minutes later as a generic CRON_STALE, if at all).

This module supplies the missing answer. It deliberately does NOT own a
registry of its own: ``cron/scheduler.py`` already maintains ``_in_flight``
(the Guard #3 same-job concurrency registry, added 2026-04-30), whose
``_InFlightRecord`` carries exactly the field needed. That comment in
``scheduler.py`` says the registry shape was chosen to "also support Guard #1,
cron_aborted on shutdown, which needs to enumerate currently-running jobs" —
this is that consumer.

**Why we read ``sys.modules`` instead of importing the scheduler.**
``cron.scheduler`` is a large module and this runs on the shutdown path, where
a first-time import would be both slow and pointless: if the scheduler was
never imported in this process, no cron can be in flight, so the correct answer
is ``[]``. Reading the already-loaded module keeps the query free and side
effect free.

The correlation id is the ``cron_started`` event_id, matching the existing
``prior_cron_started_event_id`` convention used by ``cron_skipped_duplicate``.
"""

import logging
import sys
from typing import List

logger = logging.getLogger(__name__)

_SCHEDULER_MODULE = "cron.scheduler"


def current_inflight_correlation_ids() -> List[str]:
    """``cron_started`` event ids for every cron currently in flight.

    Returns an empty list — never raises — when the scheduler was never
    loaded, when nothing is running, or when the registry cannot be read.
    Callers are on the gateway shutdown path: a failure to answer must not
    abort the GATEWAY_STOPPED emission.

    Records whose ``cron_started_event_id`` is still ``None`` are skipped.
    A job registers its in-flight slot *before* ``on_job_started`` returns, so
    there is a real window in which no correlation id exists yet; emitting a
    ``None`` into the payload would be a null entry, not a correlation.
    """
    module = sys.modules.get(_SCHEDULER_MODULE)
    if module is None:
        return []
    try:
        lock = module._in_flight_lock
        registry = module._in_flight
        with lock:
            # Copy under the lock; callers iterate outside it.
            records = list(registry.values())
    except Exception:
        logger.debug("current_inflight_correlation_ids failed", exc_info=True)
        return []

    ids: List[str] = []
    for record in records:
        event_id = getattr(record, "cron_started_event_id", None)
        if event_id:
            ids.append(event_id)
    return ids

"""HTTP client for the JobOps API (default :4100).

Two endpoints are wrapped:
  - POST /api/v1/jobs/{id}/intents/transition  (used by dashboard + reply handlers)
  - POST /api/v1/legacy/jobs/{id}/stage         (used by the applier to write Postgres
                                                  with a tracker_only-allowed source)

Errors are normalised into transient (5xx, network) vs permanent (4xx) so callers
can decide retry policy.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class JobOpsClientError(Exception):
    pass


class JobOpsClientTransientError(JobOpsClientError):
    """5xx, network errors -- retry-eligible."""


class JobOpsClientPermanentError(JobOpsClientError):
    """4xx -- caller should NOT retry; logic bug or validation failure."""


class JobOpsClient:
    def __init__(self, base_url: str = "http://127.0.0.1:4100", timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def post_intent(
        self,
        *,
        job_id: str,
        stage: str,
        actor_id: str,
        source: str,
        notes: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an intent. Returns the JobOps response dict."""
        return self._post(
            path=f"/api/v1/jobs/{job_id}/intents/transition",
            body={
                "stage": stage,
                "actorId": actor_id,
                "source": source,
                "notes": notes,
                "metadata": metadata or {},
            },
        )

    def post_legacy_stage(
        self,
        *,
        job_id: str,
        stage: str,
        actor_id: str,
        source: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """Direct stage update (must use a tracker_only-allowed actor/source)."""
        return self._post(
            path=f"/api/v1/legacy/jobs/{job_id}/stage",
            body={
                "stage": stage,
                "actorId": actor_id,
                "source": source,
                "notes": notes,
            },
        )

    def _post(self, *, path: str, body: dict) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8") if exc.fp else ""
            except Exception:
                pass
            msg = f"JobOps API {url} returned {exc.code} {exc.reason}: {err_body}"
            if 500 <= exc.code < 600:
                raise JobOpsClientTransientError(msg) from exc
            raise JobOpsClientPermanentError(msg) from exc
        except URLError as exc:
            raise JobOpsClientTransientError(f"JobOps API {url} unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            # Socket READ-timeout: ``urlopen(req, timeout=N)`` raises a *bare*
            # ``TimeoutError`` (== ``socket.timeout`` on 3.10+) from
            # ``getresponse()`` when the server accepts the TCP connection but
            # hangs on the response — e.g. :4100 under a Temporal-down 500-storm.
            # ``TimeoutError`` is an ``OSError`` subclass but NOT a ``URLError``,
            # so without this branch it escaped ``_post`` uncaught and crashed
            # the applier's ``poll()`` tick (2026-07-12: reprocess loop + dup
            # PIPELINE_UPDATE mirror). Retry-eligible.
            raise JobOpsClientTransientError(
                f"JobOps API {url} timed out after {self.timeout_seconds}s"
            ) from exc
        except OSError as exc:
            # Other socket-level failures (connection reset, broken pipe, etc.).
            # Below URLError/TimeoutError so those keep their specific messages;
            # this is the catch-all for the remaining retry-eligible OSErrors.
            raise JobOpsClientTransientError(f"JobOps API {url} socket error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise JobOpsClientTransientError(f"JobOps API {url} returned invalid JSON: {exc}") from exc

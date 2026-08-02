"""Per-agent failure clustering.

Detects when a Hermes agent emits 3 consecutive failures of the same kind
(e.g. 3 captcha bails for Applier, 3 timeouts for Scout) so that the post-
hoc Critic trigger from Hermes Revival §6 fires immediately rather than
waiting for the weekly retro.

Two pieces:
  - classify_failure_type(error_text): regex-based normalization of free-
    form error strings into a small fixed vocabulary.
  - FailureClusterDetector: per-source sliding window with file-backed
    state.  record(...) returns a cluster info dict whenever the last
    THRESHOLD entries for a source share the same failure_type, otherwise
    None.  State is persisted so detection survives gateway/scheduler
    restarts and cross-process windows still cluster correctly.

State JSON shape (failure_cluster_state.json):
    {
      "scout": [
        {"ts": "2026-04-26T10:00:00+00:00", "type": "captcha"},
        {"ts": "2026-04-26T10:05:00+00:00", "type": "captcha"},
        {"ts": "2026-04-26T10:10:00+00:00", "type": "captcha",
         "details": {"error_code": "CAPTCHA_BLOCKED", "phase": "login"}}
      ],
      "matcher": [...]
    }
"""

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Order matters — first matching pattern wins.  More specific signals
# (captcha, rate_limit) come before more generic ones (network, timeout).
_CLASSIFIER_PATTERNS: List = [
    ("captcha", re.compile(r"\bcaptcha\b", re.IGNORECASE)),
    ("rate_limit", re.compile(r"\b(429|rate[\s_-]?limit)\b", re.IGNORECASE)),
    ("auth", re.compile(r"\b(401|403|unauthori[sz]ed|forbidden|authentication)\b", re.IGNORECASE)),
    # Bidirectional: matches "<vendor> ... <error>" OR "<error> ... <vendor>"
    ("model_error", re.compile(
        r"\b(anthropic|openai|model)\b.*\b(error|overloaded|500)\b"
        r"|\b(error|overloaded|500)\b.*\b(anthropic|openai|model)\b",
        re.IGNORECASE,
    )),
    ("parse", re.compile(r"\b(json|parse|decode|decoder)\b", re.IGNORECASE)),
    ("network", re.compile(r"\b(econnreset|network|connection)\b", re.IGNORECASE)),
    ("timeout", re.compile(r"\b(timeout|timed[\s_-]out)\b", re.IGNORECASE)),
]

THRESHOLD = 3
WINDOW_SIZE = 5  # keep this many recent entries per source

# Optional diagnostics that producers can prove at the failure boundary. Unknown
# keys are discarded rather than becoming an open-ended persistence channel.
_DETAIL_FIELDS = frozenset({
    "exception_type",
    "error_code",
    "phase",
    "deadline_seconds",
    "latest_cause",
})


def _json_safe_details(details: Optional[dict]) -> Dict[str, Any]:
    """Return allowlisted, JSON-serializable diagnostics only."""
    if not isinstance(details, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key in _DETAIL_FIELDS:
        value = details.get(key)
        if value is None:
            continue
        if key == "deadline_seconds":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
        elif not isinstance(value, str):
            continue
        if isinstance(value, str):
            try:
                value = redact_sensitive_text(
                    value,
                    force=True,
                    redact_url_credentials=True,
                )
            except Exception:
                # This state is later emitted to notification surfaces. Fail
                # closed rather than persisting an unredacted diagnostic.
                continue
        safe[key] = value
    return safe


def classify_failure_type(error_text: Optional[str]) -> str:
    """Normalize a free-form error string into one of:
    captcha, rate_limit, auth, model_error, parse, network, timeout, unknown.
    """
    if not error_text:
        return "unknown"
    for label, pattern in _CLASSIFIER_PATTERNS:
        if pattern.search(error_text):
            return label
    return "unknown"


@dataclass(frozen=True)
class ClusterInfo:
    """Returned by FailureClusterDetector.record() when threshold crossed."""
    source: str
    failure_type: str
    count: int
    first_seen: str  # ISO8601
    last_seen: str   # ISO8601
    last_details: Dict[str, Any]


class FailureClusterDetector:
    """Per-source sliding-window detector with file-backed state.

    Thread-safe (single-process lock).  Cross-process safety relies on the
    state file's atomic write + the fact that the gateway is the only
    expected concurrent writer; the cron scheduler shares the gateway's
    process in the typical Hermes deployment.
    """

    def __init__(self, state_path: Path, threshold: int = THRESHOLD,
                 window_size: int = WINDOW_SIZE):
        self.state_path = Path(state_path)
        self.threshold = threshold
        self.window_size = window_size
        self._lock = threading.Lock()

    def record(
        self,
        source: str,
        success: bool,
        error_text: Optional[str] = None,
        *,
        details: Optional[dict] = None,
    ) -> Optional[ClusterInfo]:
        """Record one outcome for an agent.  Returns ClusterInfo iff the
        last `threshold` entries for `source` share the same failure_type.

        Optional details are allowlisted and JSON-checked before persistence.
        On success, the source's window is cleared (any prior cluster is
        considered resolved).  Returns None on success.
        """
        with self._lock:
            state = self._load()
            if success:
                state.pop(source, None)
                self._save(state)
                return None

            failure_type = classify_failure_type(error_text)
            now = datetime.now(timezone.utc).isoformat()
            entries = state.get(source, [])
            entry: Dict[str, Any] = {"ts": now, "type": failure_type}
            safe_details = _json_safe_details(details)
            if safe_details:
                entry["details"] = safe_details
            entries.append(entry)
            entries = entries[-self.window_size:]
            state[source] = entries
            self._save(state)

            if len(entries) < self.threshold:
                return None
            recent = entries[-self.threshold:]
            types = {e["type"] for e in recent}
            if len(types) != 1:
                return None
            return ClusterInfo(
                source=source,
                failure_type=failure_type,
                count=self.threshold,
                first_seen=recent[0]["ts"],
                last_seen=recent[-1]["ts"],
                last_details=dict(recent[-1].get("details") or {}),
            )

    def _load(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.state_path.exists():
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("failure_cluster_state malformed (not dict); resetting")
                return {}
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("failure_cluster_state read failed (%s); resetting", e)
            return {}

    def _save(self, state: Dict[str, List[Dict[str, Any]]]) -> None:
        try:
            atomic_json_write(self.state_path, state)
        except OSError:
            logger.exception("failure_cluster_state write failed")

"""CriticSubscriber — post-hoc Critic trigger.

Listens for AGENT_FAILURE_CLUSTER events emitted by CronEventEmitter and
invokes critic_retro.py with --cluster scoping so Critic produces a
focused retro for the cluster within ~30 minutes (per Hermes Revival §6).

Subprocess-based invocation (not in-process) because:
  - Critic loads its own profile-scoped environment (~/.hermes/profiles/critic/)
  - Critic retro can take longer than the subscriber poll interval
  - Subscriber must not block other events on the bus

Debounces same-(source, failure_type) clusters within a configurable
window to avoid storming Critic when a cluster persists across multiple
detection windows.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber
from hermes_constants import real_executable

logger = logging.getLogger(__name__)

# Default Critic retro script location.  Caller can override via
# ``critic_script_path`` (used by tests).
DEFAULT_CRITIC_SCRIPT = (
    Path.home() / ".hermes" / "profiles" / "critic" / "workspace" / "critic_retro.py"
)
DEFAULT_DEBOUNCE_SECONDS = 300


class CriticSubscriber(BaseSubscriber):
    subscriber_id = "critic-trigger"
    poll_interval_seconds = 5
    event_types = [EventType.AGENT_FAILURE_CLUSTER]

    def __init__(
        self,
        bus: EventBus,
        critic_script_path: Optional[Path] = None,
        debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    ):
        super().__init__(bus)
        self.critic_script_path = Path(critic_script_path or DEFAULT_CRITIC_SCRIPT)
        self.debounce_seconds = debounce_seconds
        # cluster_key -> monotonic timestamp of last invocation
        self._last_invoked: Dict[str, float] = {}

    def handle(self, event: Event) -> None:
        source = event.payload.get("source") or event.source
        failure_type = event.payload.get("failure_type", "unknown")
        cluster_key = f"{source}:{failure_type}"

        now = time.monotonic()
        prev = self._last_invoked.get(cluster_key)
        if prev is not None and (now - prev) < self.debounce_seconds:
            logger.info(
                "CriticSubscriber: debounced cluster %s (last invoked %ds ago)",
                cluster_key, int(now - prev),
            )
            return

        if not self.critic_script_path.exists():
            logger.warning(
                "CriticSubscriber: critic_retro.py not found at %s — skipping",
                self.critic_script_path,
            )
            return

        cmd: List[str] = [
            real_executable(),
            str(self.critic_script_path),
            "--cluster",
            f"agent={source},type={failure_type}",
        ]
        try:
            # Non-blocking subprocess (NOT fully detached on Windows).
            # No stdout/stderr capture — Critic writes its own retros/ files.
            # Popen returns immediately.
            #
            # Windows caveat: without creationflags=DETACHED_PROCESS|
            # CREATE_NEW_PROCESS_GROUP the child inherits the gateway's
            # Job Object, so a gateway kill terminates an in-flight Critic
            # retro. Acceptable for v1 — retros are idempotent and the
            # next cluster event will re-trigger. Promote to true detach
            # in v2 if retro loss is observed in production.
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            self._last_invoked[cluster_key] = now
            logger.info(
                "CriticSubscriber: invoked Critic for cluster %s", cluster_key,
            )
        except OSError as e:
            logger.exception(
                "CriticSubscriber: failed to spawn Critic for %s: %s",
                cluster_key, e,
            )
            # Do NOT re-raise — base subscriber would count it toward the
            # circuit breaker.  Subprocess spawn failure is observable via
            # the log; the next cluster event will retry.

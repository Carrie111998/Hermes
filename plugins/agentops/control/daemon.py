"""Explicit foreground lifecycle for the isolated observe-only control plane."""

from __future__ import annotations

import threading
import time
import os
from dataclasses import dataclass
from pathlib import Path

from plugins.agentops.control.api import ControlAPI, request_health
from plugins.agentops.control.config import AgentOpsConfig, load_agentops_config
from plugins.agentops.control.events import EventSpool
from plugins.agentops.control.models import AuthorityMode, ControlPlaneHealth
from plugins.agentops.control.store import AgentOpsStore, StoreMigrationError, open_store


@dataclass
class DaemonHandle:
    socket_path: Path
    stop_event: threading.Event
    thread: threading.Thread

    def health(self) -> dict:
        return request_health(self.socket_path)

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("daemon did not stop")


class ObserveOnlyDaemon:
    def __init__(self, config: AgentOpsConfig):
        self.config = config
        self.store: AgentOpsStore | None = None
        self.spool = EventSpool(config.spool_dir, max_bytes=config.event_spool_max_bytes)
        self.reasons = list(config.safe_start_reasons)
        self.api: ControlAPI | None = None

    def _health(self) -> dict:
        store_available = self.store is not None
        audit_chain_valid: bool | None = None
        event_count = 0
        if self.store is not None:
            try:
                audit_chain_valid = self.store.verify_audit_chain()
                event_count = self.store.event_count()
                if not audit_chain_valid and "audit_chain_invalid" not in self.reasons:
                    self.reasons.append("audit_chain_invalid")
            except Exception:
                audit_chain_valid = False
                if "store_unavailable" not in self.reasons:
                    self.reasons.append("store_unavailable")
        health = ControlPlaneHealth(
            ready=True,
            authority_mode=AuthorityMode.OBSERVE_ONLY,
            safe_start_reasons=tuple(self.reasons),
            store_available=store_available,
            audit_chain_valid=audit_chain_valid,
            event_count=event_count,
            spool_depth=self.spool.depth(),
            global_write_enabled=False,
        )
        return health.to_dict()

    def run(self, stop_event: threading.Event) -> int:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config.state_dir, 0o700)
        except OSError:
            pass
        try:
            self.store = open_store(self.config.sqlite_path)
            try:
                self.spool.replay(self.store)
            except Exception:
                self.reasons.append("spool_replay_failed")
            if not self.store.verify_audit_chain():
                self.reasons.append("audit_chain_invalid")
        except StoreMigrationError:
            self.store = None
            self.reasons.append("store_migration_failed")
        self.api = ControlAPI(self.config.socket_path, self._health)
        try:
            self.api.start()
        except Exception:
            if self.store is not None:
                self.store.close()
            return 1
        try:
            while not stop_event.wait(0.05):
                pass
        finally:
            self.api.stop()
            if self.store is not None:
                self.store.close()
        return 0


def run_daemon(config: AgentOpsConfig, stop_event: threading.Event) -> int:
    """Run an explicit local daemon; this function has no Target side effects."""
    return ObserveOnlyDaemon(config).run(stop_event)


def start_daemon_thread(config_path: Path, *, timeout_seconds: float = 5) -> DaemonHandle:
    """Test helper that starts a foreground daemon in a managed thread."""
    config = load_agentops_config(Path(config_path))
    stop_event = threading.Event()
    thread = threading.Thread(target=run_daemon, args=(config, stop_event), name="agentops-test-daemon", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if config.socket_path.exists():
            return DaemonHandle(socket_path=config.socket_path, stop_event=stop_event, thread=thread)
        if not thread.is_alive():
            break
        time.sleep(0.01)
    stop_event.set()
    thread.join(timeout=1)
    raise RuntimeError("agentops daemon did not create health socket")

"""Cross-process physical-node capacity leases for Kanban workers.

The registry is intentionally filesystem-backed and guarded by POSIX
``flock`` so independent dispatcher processes sharing ``HERMES_KANBAN_HOME``
make one atomic capacity decision. Lease expiry provides crash recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only feature
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True)
class NodeLease:
    """One acquired physical-node capacity slot."""

    pool: "PosixNodeLeasePool"
    node: str
    profile: str
    owner: str

    def release(self) -> bool:
        return self.pool.release(owner=self.owner, node=self.node)


class PosixNodeLeasePool:
    """Atomic, process-shared capacity registry keyed by physical node."""

    def __init__(
        self,
        *,
        root: Path | str,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        if fcntl is None:
            raise RuntimeError("physical node leases require POSIX fcntl.flock")
        self.root = Path(root)
        self._state_path = self.root / "state.json"
        self._lock_path = self.root / "state.lock"
        self._now_fn = now_fn

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self._read_state()
            self._prune_expired(state)
            yield state
            self._write_state(state)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"nodes": {}}
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
            return {"nodes": {}}
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._state_path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _prune_expired(self, state: dict[str, Any]) -> None:
        now = float(self._now_fn())
        nodes = state.setdefault("nodes", {})
        for node in list(nodes):
            node_state = nodes.get(node)
            if not isinstance(node_state, dict):
                nodes.pop(node, None)
                continue
            leases = node_state.get("leases")
            if not isinstance(leases, list):
                leases = []
            node_state["leases"] = [
                lease
                for lease in leases
                if isinstance(lease, dict)
                and float(lease.get("expires_at", 0) or 0) > now
            ]
            if not node_state["leases"]:
                nodes.pop(node, None)

    def try_acquire(
        self,
        *,
        profile: str,
        owner: str,
        profile_to_node: Mapping[str, str],
        capacities: Mapping[str, int],
        ttl_seconds: int,
    ) -> Optional[NodeLease]:
        node = str(profile_to_node.get(profile) or "").strip()
        if not node or not owner:
            return None
        try:
            capacity = int(capacities.get(node, 0))
            ttl = int(ttl_seconds)
        except (TypeError, ValueError):
            return None
        if capacity < 1 or ttl < 1:
            return None

        now = float(self._now_fn())
        with self._locked_state() as state:
            node_state = state.setdefault("nodes", {}).setdefault(
                node, {"capacity": capacity, "leases": []}
            )
            node_state["capacity"] = capacity
            leases = node_state.setdefault("leases", [])
            for existing in leases:
                if existing.get("owner") == owner:
                    existing.update(
                        profile=profile,
                        expires_at=now + ttl,
                    )
                    return NodeLease(self, node, profile, owner)
            if len(leases) >= capacity:
                return None
            leases.append(
                {
                    "owner": owner,
                    "profile": profile,
                    "expires_at": now + ttl,
                }
            )
        return NodeLease(self, node, profile, owner)

    def release(self, *, owner: str, node: Optional[str] = None) -> bool:
        removed = False
        with self._locked_state() as state:
            nodes = state.setdefault("nodes", {})
            targets = [node] if node else list(nodes)
            for target in targets:
                node_state = nodes.get(target)
                if not isinstance(node_state, dict):
                    continue
                leases = node_state.get("leases", [])
                kept = [lease for lease in leases if lease.get("owner") != owner]
                if len(kept) != len(leases):
                    removed = True
                    node_state["leases"] = kept
                if not kept:
                    nodes.pop(target, None)
        return removed

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._locked_state() as state:
            result: dict[str, dict[str, Any]] = {}
            for node, node_state in state.get("nodes", {}).items():
                leases = node_state.get("leases", [])
                result[node] = {
                    "capacity": int(node_state.get("capacity", 0) or 0),
                    "in_use": len(leases),
                    "owners": sorted(str(lease.get("owner") or "") for lease in leases),
                    "profiles": sorted(str(lease.get("profile") or "") for lease in leases),
                }
            return result


def default_pool() -> PosixNodeLeasePool:
    home = Path(os.environ.get("HERMES_KANBAN_HOME") or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return PosixNodeLeasePool(root=home / "kanban" / "node-leases")

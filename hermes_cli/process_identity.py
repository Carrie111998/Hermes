"""Process identity: spawn tags, the machine-wide spawn ledger, and the
Windows job-object self-attach.

Three layers that make every long-lived Hermes process positively
identifiable, so reapers (``hermes update``, Desktop startup sweeps) never
have to guess lineage from PPID archaeology or cmdline pattern-matching:

1. **Spawn tag** (``HERMES_SPAWN`` env var): every spawner stamps its children
   with ``v1:<install_id>:<purpose>:<spawner_pid>:<spawner_create>``. A scanner
   that can read the child's environment classifies it instantly: which
   install, what it is, who spawned it, and when.

2. **Spawn ledger** (``spawn-ledger.json`` at the machine Hermes root): every
   long-lived process (serve/dashboard backend, gateway) self-registers
   ``pid + create_time + purpose + spawner`` at startup. ``pid`` alone is
   forgeable by reuse; the ``(pid, create_time)`` pair is not. Reapers
   cross-check live processes against the ledger for positive identification
   even when environment reads are denied (Windows frequently denies
   ``Process.environ()`` cross-session).

3. **Job object self-attach** (Windows): a backend places itself in a job with
   ``KILL_ON_JOB_CLOSE`` so its whole child tree dies atomically with it —
   no launcher→worker two-hop chains left holding ``.pyd`` locks after the
   visible root is killed. ``BREAKAWAY_OK`` is set so the existing
   ``CREATE_BREAKAWAY_FROM_JOB`` spawns (gateway relaunch during update,
   watchers that must outlive their spawner) keep working unchanged.

All of it is best-effort and fail-safe: identity failures degrade to the
legacy heuristics, they never block startup or updates. The ledger tolerates
corruption the same way ``backend-ownership.json`` does post-#89298:
an unreadable ledger is quarantined aside (``.corrupt``), never rewritten
blind.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SPAWN_ENV_VAR = "HERMES_SPAWN"
_TAG_VERSION = "v1"
LEDGER_FILENAME = "spawn-ledger.json"

#: Purposes a reaper may treat as "safe to kill when the owner is gone".
#: Interactive processes (chat, REPLs) are deliberately NOT in this set.
REAPABLE_PURPOSES = frozenset({"serve", "dashboard", "gateway", "mcp-helper"})

_IS_WINDOWS = platform.system() == "Windows"

# Module-global job handle: must live exactly as long as this process so the
# kernel closes it (and kills the job) when we die. Never close it manually.
_JOB_HANDLE = None
_LEDGER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Install identity
# ---------------------------------------------------------------------------

def install_id(project_root: Optional[Path] = None) -> str:
    """Stable 12-hex identifier for THIS install (derived from its path).

    Lets a reaper reject processes from a different Hermes install on the
    same machine without path comparisons at scan time.
    """
    if project_root is None:
        try:
            from hermes_constants import PROJECT_ROOT as _root

            project_root = Path(_root)
        except Exception:
            project_root = Path(__file__).resolve().parent.parent
    try:
        canonical = str(Path(project_root).resolve()).lower()
    except OSError:
        canonical = str(project_root).lower()
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:12]


def _own_create_time() -> Optional[float]:
    try:
        import psutil

        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Layer 1 — spawn tags
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpawnTag:
    install: str
    purpose: str
    spawner_pid: int
    spawner_create: Optional[float]


def build_spawn_tag(purpose: str, *, project_root: Optional[Path] = None) -> str:
    """Value for the child's ``HERMES_SPAWN`` env var, stamped by the spawner."""
    create = _own_create_time()
    create_part = f"{create:.3f}" if create is not None else "-"
    return ":".join(
        (_TAG_VERSION, install_id(project_root), purpose, str(os.getpid()), create_part)
    )


def spawn_env(purpose: str, *, project_root: Optional[Path] = None) -> dict[str, str]:
    """Env fragment a spawner merges into a child's environment."""
    return {SPAWN_ENV_VAR: build_spawn_tag(purpose, project_root=project_root)}


def parse_spawn_tag(raw: object) -> Optional[SpawnTag]:
    """Parse a ``HERMES_SPAWN`` value; ``None`` for anything malformed."""
    if not isinstance(raw, str):
        return None
    parts = raw.split(":")
    if len(parts) != 5 or parts[0] != _TAG_VERSION:
        return None
    _, install, purpose, pid_s, create_s = parts
    if not install or not purpose:
        return None
    try:
        pid = int(pid_s)
    except ValueError:
        return None
    if pid <= 0:
        return None
    create: Optional[float] = None
    if create_s != "-":
        try:
            create = float(create_s)
        except ValueError:
            return None
    return SpawnTag(install=install, purpose=purpose, spawner_pid=pid, spawner_create=create)


# ---------------------------------------------------------------------------
# Layer 2 — spawn ledger
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    pid: int
    create_time: Optional[float]
    purpose: str
    install: str
    spawner_pid: Optional[int]
    spawner_create: Optional[float]
    registered_at: float
    argv: str
    # Structured launch identity (#63206): what a relauncher needs to bring
    # this runtime back after an update, without parsing argv. Empty for
    # purposes that don't supply it; readers must use .get() — older ledger
    # files on disk predate these keys.
    host: str = ""
    port: Optional[int] = None
    profile: str = ""
    # The FULL argv as a list. ``argv`` above is a lossy human-readable
    # rendering of it — a joined string cannot round-trip an argument that
    # contains whitespace, and it used to be truncated at ten tokens, which
    # is exactly where `--host`/`--port`/`--profile` sit on a slightly longer
    # command. A relauncher must use this list and hand it to the OS verbatim.
    argv_list: Optional[list] = None
    # The HERMES_HOME this runtime was running under. The relaunch sets it in
    # the replacement's environment; without a producer here that lookup found
    # nothing and the replacement silently inherited the updater's home.
    hermes_home: str = ""
    # The source SHA this process is running, stamped by the process ITSELF.
    # For serve/dashboard backends — which do not write gateway_state.json —
    # this is the only runtime-kind-correct proof that a replacement came up
    # on post-update code.
    code_sha: str = ""


def _ledger_path() -> Path:
    """Machine-root ledger path (shared by every profile of this install)."""
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root()) / LEDGER_FILENAME
    except Exception:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home()) / LEDGER_FILENAME


def _read_ledger(path: Path) -> Optional[list[dict]]:
    """Entries list, ``[]`` for empty/missing, ``None`` for CORRUPT.

    Mirrors the #89298 contract: corrupt is a distinct state that must never
    be silently treated as an empty roster.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return None
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    return [e for e in parsed if isinstance(e, dict)]


class LedgerUnreadable(RuntimeError):
    """The spawn ledger exists but could not be read as a roster.

    Raised only for ``strict`` readers — the ones whose next act depends on
    the roster being the whole truth (the pre-mutation update inventory).
    Everyone else keeps the lenient, self-healing behaviour: quarantine the
    file and carry on with an empty list.
    """


def _quarantine_ledger(path: Path) -> None:
    parked = path.with_suffix(path.suffix + ".corrupt")
    try:
        os.replace(path, parked)
        logger.warning("spawn ledger was unreadable; moved to %s", parked)
    except OSError:
        pass


def _pid_alive_matches(pid: int, create_time: Optional[float]) -> Optional[bool]:
    """True/False when provable; ``None`` when psutil can't say."""
    try:
        import psutil
    except Exception:
        return None
    try:
        proc = psutil.Process(int(pid))
        if create_time is None:
            return True
        return abs(float(proc.create_time()) - float(create_time)) < 2.0
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared-checkout startup barrier
# ---------------------------------------------------------------------------
#
# ``hermes update`` quiesces the fleet, re-inventories it at the gate, and
# sweeps again after the stops. That closes every window the updater can
# SEE. It cannot close the one it cannot: a runtime launched between the
# final recollection and the first byte written to the checkout was in no
# inventory, so it was never stopped, and it spends the whole mutation
# importing from a tree that is being replaced underneath it. Re-collecting
# harder only narrows a TOCTOU; the starter has to participate.
#
# So the updater takes a durable lease before its final inventory and every
# startup path checks it before initializing anything. The lease is a file
# next to the spawn ledger — the same machine-root scope the ledger already
# has — because the updater that finishes the job is frequently NOT the
# process that started it (the Windows hand-off child, a ``systemd-run``
# scope), and in-memory authorization does not survive that boundary.

STARTUP_BARRIER_FILENAME = "update-startup-barrier.json"

#: How long a lease stays honoured when its owner cannot be probed at all.
#: A provably-live owner keeps its barrier past this; the TTL exists only so
#: an updater killed so hard that even its identity is unreadable cannot
#: wedge every startup on the machine forever.
STARTUP_BARRIER_TTL = 1800.0
#: How long a startup waits for an active barrier before refusing. Long
#: enough to absorb an ordinary update's mutation window, short enough that
#: a supervisor's start timeout is not what surfaces the problem.
STARTUP_BARRIER_WAIT_TIMEOUT = 90.0
STARTUP_BARRIER_POLL_INTERVAL = 0.25
#: Operator override for the wait budget, in seconds. A site whose
#: supervisor start timeout is tighter than ours needs to be able to say so
#: — and a `0` turns the barrier into a pure refusal.
STARTUP_BARRIER_WAIT_ENV = "HERMES_STARTUP_BARRIER_WAIT_SECONDS"

#: Startups are blocked in ``mutating`` only. Once the checkout is whole
#: again the updater flips the lease to ``relaunching``: it still owns the
#: record (that is what makes recovery possible), but a fresh interpreter
#: reading the NEW tree is precisely what the relaunch phase needs next —
#: blocking there would deadlock the update against its own restarts.
BARRIER_PHASE_MUTATING = "mutating"
BARRIER_PHASE_RELAUNCHING = "relaunching"

_BARRIER_LOCK = threading.Lock()
#: ``(pid, create_time)`` of the lease THIS process wrote, or ``None``.
_barrier_owner_token: Optional[tuple] = None


class StartupBarrierActive(RuntimeError):
    """A shared-checkout mutation is in progress; startup must not proceed."""


def machine_id() -> str:
    """Host identity, so a shared Hermes root cannot block other machines."""
    try:
        return platform.node() or "unknown-machine"
    except Exception:
        return "unknown-machine"


def _default_barrier_wait() -> float:
    """The wait budget, honouring the operator override. Never raises."""
    raw = os.environ.get(STARTUP_BARRIER_WAIT_ENV, "")
    if raw.strip():
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            logger.warning(
                "ignoring unparsable %s=%r", STARTUP_BARRIER_WAIT_ENV, raw
            )
    return STARTUP_BARRIER_WAIT_TIMEOUT


def _barrier_path() -> Path:
    """Machine-root barrier path (shared by every profile of this install)."""
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root()) / STARTUP_BARRIER_FILENAME
    except Exception:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home()) / STARTUP_BARRIER_FILENAME


def _read_barrier(path: Path) -> tuple:
    """``(record, readable)``. ``(None, True)`` means no lease is held.

    ``readable`` is ``False`` for a file that exists but is not a lease —
    the state a startup must treat as "an update may be running", never as
    "no update is running".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    if not text.strip():
        return None, True
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None, False
    if not isinstance(parsed, dict):
        return None, False
    return parsed, True


def _barrier_owner_is_gone(record: dict, now: float) -> bool:
    """Is this lease recoverable? Proof required, absence of evidence is not.

    ``True`` only when the owner is provably dead, or when it cannot be
    probed at all AND the lease has outlived its TTL. A live owner is never
    expired out from under a slow update.
    """
    owner_pid = record.get("owner_pid")
    if isinstance(owner_pid, int) and owner_pid > 0:
        alive = _pid_alive_matches(owner_pid, record.get("owner_create"))
        if alive is True:
            return False
        if alive is False:
            return True
    # Unprovable owner (no pid recorded, psutil unavailable, another user's
    # process): the TTL is the only escape, and a lease with no readable
    # expiry keeps blocking.
    try:
        return float(now) >= float(record.get("expires_at"))
    except (TypeError, ValueError):
        return False


def _recover_barrier(path: Path) -> None:
    """Drop a lease whose owner is provably gone. Best-effort."""
    try:
        path.unlink()
    except OSError:
        pass
    except Exception:  # pragma: no cover - defensive
        pass


def _barrier_applies_to(record: dict, profile: str, project_root) -> bool:
    """Is this lease about THIS checkout, on THIS machine, for THIS profile?"""
    if str(record.get("install") or "") != install_id(project_root):
        return False
    if str(record.get("machine") or "") != machine_id():
        return False
    profiles = record.get("profiles")
    if isinstance(profiles, (list, tuple)) and profiles:
        # A narrower, profile-scoped lease. An unnamed startup is still
        # covered: we cannot show it is one of the profiles left running.
        if profile and str(profile) not in [str(p) for p in profiles]:
            return False
    return True


def startup_barrier_reason(
    purpose: str,
    *,
    profile: str = "",
    project_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> str:
    """Why *purpose* must not start right now, or ``""`` when it may.

    Never raises: an unreadable lease answers with a reason (fail closed),
    not with an exception the caller's ``except Exception`` would swallow
    into "carry on".
    """
    try:
        path = _barrier_path()
    except Exception as exc:  # pragma: no cover - defensive
        return (
            f"refusing to start {purpose}: the update startup barrier could "
            f"not be located ({exc}), so a shared-checkout update cannot be "
            "ruled out"
        )
    record, readable = _read_barrier(path)
    if not readable:
        return (
            f"refusing to start {purpose}: the update startup barrier at "
            f"{path} is unreadable, so a `hermes update` mutating the shared "
            "checkout cannot be ruled out. Remove the file once you have "
            "confirmed no update is running"
        )
    if record is None:
        return ""
    try:
        if not _barrier_applies_to(record, profile, project_root):
            return ""
        if str(record.get("phase") or BARRIER_PHASE_MUTATING) != (
            BARRIER_PHASE_MUTATING
        ):
            return ""
        stamp = time.time() if now is None else float(now)
        if _barrier_owner_is_gone(record, stamp):
            _recover_barrier(path)
            return ""
    except Exception as exc:  # pragma: no cover - defensive
        return (
            f"refusing to start {purpose}: the update startup barrier at "
            f"{path} could not be evaluated ({exc})"
        )
    owner = record.get("owner_pid")
    return (
        f"refusing to start {purpose}: `hermes update` (pid {owner}) is "
        "mutating the shared checkout this process would import from. "
        "Startup is held until the update releases the barrier"
    )


def await_startup_clearance(
    purpose: str,
    *,
    profile: str = "",
    project_root: Optional[Path] = None,
    timeout: Optional[float] = None,
    poll_interval: float = STARTUP_BARRIER_POLL_INTERVAL,
    monotonic=time.monotonic,
    sleep=time.sleep,
    on_wait: Optional[object] = None,
) -> None:
    """Block until the checkout is stable, then return — or refuse.

    Waiting is the friendly half: an ordinary update's mutation window is
    seconds, and a supervised gateway that waits it out comes back on the
    new code with no restart loop. Refusing is the half that matters:
    whatever happens, this process does not initialize against a checkout
    somebody is rewriting.
    """
    budget = _default_barrier_wait() if timeout is None else max(float(timeout), 0.0)
    deadline = monotonic() + budget
    announced = False
    while True:
        reason = startup_barrier_reason(
            purpose, profile=profile, project_root=project_root
        )
        if not reason:
            return
        if monotonic() >= deadline:
            raise StartupBarrierActive(reason)
        if on_wait is not None and not announced:
            announced = True
            try:
                on_wait(reason)  # type: ignore[operator]
            except Exception:
                pass
        sleep(max(float(poll_interval), 0.0))


def _barrier_record_is_ours(record: dict) -> bool:
    owner_pid = record.get("owner_pid")
    if not isinstance(owner_pid, int) or owner_pid != os.getpid():
        return False
    recorded = record.get("owner_create")
    mine = _own_create_time()
    if recorded is None or mine is None:
        return True
    try:
        return abs(float(recorded) - float(mine)) < 2.0
    except (TypeError, ValueError):
        return False


def _write_barrier_record(path: Path, payload: dict) -> bool:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("could not write the update startup barrier: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def acquire_startup_barrier(
    *,
    profiles=(),
    project_root: Optional[Path] = None,
    reason: str = "",
    ttl: float = STARTUP_BARRIER_TTL,
) -> bool:
    """Take the lease that holds every startup off this checkout.

    ``False`` — never an exception — when the lease belongs to someone
    else, or when the file cannot be read or written. Every one of those is
    "we cannot prove we are the only mutator", and the caller's answer to
    that is to abort before touching anything.

    ``profiles`` narrows the lease; the empty default means the whole
    checkout, which is what a git/venv mutation actually affects.
    """
    global _barrier_owner_token
    path = _barrier_path()
    now = time.time()
    with _BARRIER_LOCK:
        record, readable = _read_barrier(path)
        if not readable:
            logger.warning(
                "the update startup barrier at %s is unreadable; refusing to "
                "assume no other update holds it",
                path,
            )
            return False
        if record is not None and not _barrier_record_is_ours(record):
            if not _barrier_applies_to(record, "", project_root):
                # Another install or machine wrote here. Overwriting would
                # erase a lease we cannot reason about.
                logger.warning(
                    "the update startup barrier at %s belongs to another "
                    "install/machine; refusing to take it over",
                    path,
                )
                return False
            if not _barrier_owner_is_gone(record, now):
                return False
        create = _own_create_time()
        payload = {
            "install": install_id(project_root),
            "machine": machine_id(),
            "owner_pid": os.getpid(),
            "owner_create": create,
            "profiles": [str(p) for p in (profiles or ())],
            "phase": BARRIER_PHASE_MUTATING,
            "acquired_at": now,
            "expires_at": now + max(float(ttl), 0.0),
            "reason": str(reason or ""),
        }
        if not _write_barrier_record(path, payload):
            return False
        _barrier_owner_token = (payload["owner_pid"], payload["owner_create"])
        return True


def set_startup_barrier_phase(phase: str) -> bool:
    """Move OUR lease to *phase*. ``False`` when we do not hold one."""
    path = _barrier_path()
    with _BARRIER_LOCK:
        record, readable = _read_barrier(path)
        if not readable or record is None:
            return False
        if not _barrier_record_is_ours(record):
            return False
        record["phase"] = str(phase)
        return _write_barrier_record(path, record)


def release_startup_barrier() -> bool:
    """Drop OUR lease. Never drops one written by another process.

    That restriction is what makes the command-boundary backstop safe: a
    parent that handed the update to a detached child must not free the
    child's lease on its way out.
    """
    global _barrier_owner_token
    path = _barrier_path()
    with _BARRIER_LOCK:
        record, readable = _read_barrier(path)
        if not readable:
            # Corrupt, and we know we wrote one: leaving it would block every
            # startup on the machine until the TTL. Ours to clear.
            if _barrier_owner_token is not None:
                _recover_barrier(path)
                _barrier_owner_token = None
                return True
            return False
        if record is None:
            _barrier_owner_token = None
            return True
        if not _barrier_record_is_ours(record):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not release the update startup barrier: %s", exc)
            return False
        _barrier_owner_token = None
        return True


def forget_startup_barrier_ownership() -> None:
    """Drop the in-process "we wrote a lease" note (test hygiene)."""
    global _barrier_owner_token
    _barrier_owner_token = None


def register_self(
    purpose: str,
    *,
    project_root: Optional[Path] = None,
    detail: Optional[dict] = None,
) -> bool:
    """Record this process in the machine spawn ledger. Best-effort.

    Called at the top of every long-lived entry point (serve/dashboard
    backend, gateway run loop). Dead entries — ``(pid, create_time)`` no
    longer live — are pruned on every write so the ledger tracks reality
    instead of growing forever.

    ``detail`` optionally carries structured launch identity (#63206) —
    ``host``/``port``/``profile`` — so the update pipeline can relaunch a
    manually-started serve with its real bind address instead of guessing
    from argv.

    Refuses (``False``, nothing written) while the shared-checkout startup
    barrier is held. The entry points gate earlier and harder — this is the
    backstop for any path that reaches registration anyway, and it keeps the
    updater's final inventory true: a row that appears after the last
    recollection is a runtime nothing will stop.
    """
    barrier = startup_barrier_reason(
        purpose,
        profile=str((detail or {}).get("profile") or ""),
        project_root=project_root,
    )
    if barrier:
        logger.warning("%s", barrier)
        return False
    tag = parse_spawn_tag(os.environ.get(SPAWN_ENV_VAR))
    spawner_pid: Optional[int] = tag.spawner_pid if tag else None
    spawner_create: Optional[float] = tag.spawner_create if tag else None
    if spawner_pid is None:
        # Desktop compatibility: the Electron app already stamps children with
        # HERMES_PARENT_PID (+ optional `winms:<ms>` start marker) for its
        # parent-death watchdog. Reuse it as spawner identity so ledger
        # lineage works with every Desktop version, no TS change needed.
        try:
            raw = int(os.environ.get("HERMES_PARENT_PID", ""))
            if raw > 0:
                spawner_pid = raw
        except (TypeError, ValueError):
            pass
        marker = os.environ.get("HERMES_PARENT_START_MARKER", "")
        if spawner_pid is not None and marker.startswith("winms:"):
            try:
                spawner_create = float(marker.split(":", 1)[1]) / 1000.0
            except (ValueError, IndexError):
                spawner_create = None
    entry = LedgerEntry(
        pid=os.getpid(),
        create_time=_own_create_time(),
        purpose=purpose,
        install=install_id(project_root),
        spawner_pid=spawner_pid,
        spawner_create=spawner_create,
        registered_at=time.time(),
        argv="",
    )
    if detail:
        try:
            entry.host = str(detail.get("host") or "")
            port = detail.get("port")
            entry.port = int(port) if port is not None else None
            entry.profile = str(detail.get("profile") or "")
        except (TypeError, ValueError):
            pass
    try:
        import sys as _sys

        # The list is the record; the joined string is kept only so existing
        # readers (log lines, the dashboard scan's cmdline matcher) keep
        # working. No truncation: dropping the eleventh token silently
        # dropped `--port` from longer commands, and a respawn from a
        # truncated argv is a WRONG process, not a failed one.
        entry.argv_list = [str(part) for part in _sys.argv]
        entry.argv = " ".join(entry.argv_list[:10])
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        entry.hermes_home = str(get_hermes_home())
    except Exception:
        logger.debug("could not record HERMES_HOME in the spawn ledger", exc_info=True)
    try:
        from hermes_cli.build_info import get_code_identity

        entry.code_sha = str(get_code_identity().get("sha") or "")
    except Exception:
        logger.debug("could not stamp the running code sha", exc_info=True)

    return _append_entry(entry)


def _append_entry(entry: LedgerEntry) -> bool:
    """Prune dead entries and append ``entry`` — the ONLY ledger write path.

    Serialized under ``_LEDGER_LOCK`` with an atomic tmp+replace, exactly as
    ``register_self`` has always written (kept single so #91660's lock-
    serialization guarantees hold: no writer ever touches the file outside
    this function).
    """
    path = _ledger_path()
    with _LEDGER_LOCK:
        entries = _read_ledger(path)
        if entries is None:
            _quarantine_ledger(path)
            entries = []
        pruned: list[dict] = []
        for e in entries:
            pid = e.get("pid")
            if not isinstance(pid, int) or pid == entry.pid:
                continue
            alive = _pid_alive_matches(pid, e.get("create_time"))
            if alive is False:
                continue  # provably dead → prune
            pruned.append(e)
        pruned.append(asdict(entry))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            logger.debug("spawn ledger write failed", exc_info=True)
            return False


def register_child(
    pid: int,
    purpose: str,
    *,
    project_root: Optional[Path] = None,
) -> bool:
    """Record a CHILD process this process just spawned. Best-effort.

    Mirror of :func:`register_self` for children that cannot register
    themselves (stdio MCP helper subprocesses, #61514: arbitrary
    ``npx``/binary servers never import Hermes code). The entry records the
    child's ``(pid, create_time)`` with THIS process as the spawner, so
    reapers get the same positive-identity contract:

    - a live helper whose spawner is still alive is never reaped
      (``spawner_is_dead`` → ``False``);
    - a helper whose spawner ``(pid, create_time)`` is provably gone is a
      reapable orphan.

    Never raises; returns ``False`` when the child already exited (no
    provable ``create_time`` means no forge-proof identity — don't record a
    pid-only entry a reuse could impersonate) or the write failed.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil

        child_create: Optional[float] = float(psutil.Process(pid).create_time())
    except Exception:
        return False
    entry = LedgerEntry(
        pid=pid,
        create_time=child_create,
        purpose=purpose,
        install=install_id(project_root),
        spawner_pid=os.getpid(),
        spawner_create=_own_create_time(),
        registered_at=time.time(),
        argv="",
    )
    try:
        import psutil

        entry.argv_list = [str(part) for part in psutil.Process(pid).cmdline()]
        entry.argv = " ".join(entry.argv_list[:10])
    except Exception:
        pass
    return _append_entry(entry)


def ledger_entries(
    *, project_root: Optional[Path] = None, strict: bool = False
) -> list[dict]:
    """Live-verified ledger entries for THIS install.

    Entries whose ``(pid, create_time)`` no longer matches a live process are
    excluded (PID reuse reads as dead, thanks to the create-time pair).

    Corruption is where the two caller classes part ways. By default a
    corrupt or unreadable ledger is quarantined and read as empty —
    identical philosophy to the backend-ownership fix (#89298): never let
    corruption erase or fake a roster, never let it block a startup reaper
    either. But "no backends are registered" and "the roster is unreadable"
    are NOT the same fact, and a caller about to authorize a checkout
    mutation must not act on the second as if it were the first. Those
    callers pass ``strict=True`` and get :class:`LedgerUnreadable`.

    A strict read deliberately does NOT quarantine: moving the damaged file
    aside would make the very next read return a positively-empty roster,
    turning a fail-closed abort into a fail-open retry.
    """
    want_install = install_id(project_root)
    path = _ledger_path()
    with _LEDGER_LOCK:
        entries = _read_ledger(path)
        if entries is None:
            if strict:
                raise LedgerUnreadable(
                    f"the spawn ledger at {path} is corrupt or unreadable, so "
                    "the set of running serve/dashboard backends cannot be "
                    "established"
                )
            _quarantine_ledger(path)
            return []
    out: list[dict] = []
    for e in entries:
        if e.get("install") != want_install:
            continue
        pid = e.get("pid")
        if not isinstance(pid, int):
            continue
        if _pid_alive_matches(pid, e.get("create_time")) is False:
            continue
        out.append(e)
    return out


def spawner_is_dead(entry: dict) -> Optional[bool]:
    """Is the recorded spawner of this entry provably gone?

    ``True`` → owner gone (orphaned by identity, not by PPID guessing).
    ``False`` → owner still alive. ``None`` → no spawner recorded / unprovable.
    """
    spawner_pid = entry.get("spawner_pid")
    if not isinstance(spawner_pid, int) or spawner_pid <= 0:
        return None
    alive = _pid_alive_matches(spawner_pid, entry.get("spawner_create"))
    if alive is None:
        return None
    return not alive


def reap_orphaned_mcp_helpers(
    *,
    project_root: Optional[Path] = None,
    kill_fn=None,
) -> list[int]:
    """Kill ledger-registered stdio MCP helpers whose spawner is provably dead.

    Startup-sweep rung mirroring ``_reap_orphaned_desktop_local_serves``
    (dashboard_procs.py), but ledger-driven instead of cmdline-heuristic:
    a helper is reaped ONLY when

    - it has a live ``(pid, create_time)`` ledger entry for THIS install with
      purpose ``mcp-helper`` (``ledger_entries`` already excludes dead/
      foreign entries), and
    - its recorded spawner is **provably dead** (``spawner_is_dead`` is
      ``True`` — never ``None``/unprovable, never a live spawner).

    Best-effort, never raises; returns the PIDs it terminated. ``kill_fn``
    is injectable for tests (defaults to psutil terminate→wait→kill).
    """
    reaped: list[int] = []
    try:
        entries = ledger_entries(project_root=project_root)
    except Exception:
        return reaped
    own_pid = os.getpid()
    for entry in entries:
        try:
            if entry.get("purpose") != "mcp-helper":
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or pid <= 0 or pid == own_pid:
                continue
            if spawner_is_dead(entry) is not True:
                continue  # live or unprovable spawner → never touch
            if kill_fn is not None:
                kill_fn(pid)
            else:
                import psutil

                proc = psutil.Process(pid)
                # Re-verify identity at the moment of kill (PID-reuse guard).
                create = entry.get("create_time")
                if create is not None and abs(
                    float(proc.create_time()) - float(create)
                ) >= 2.0:
                    continue
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except psutil.TimeoutExpired:
                    proc.kill()
            reaped.append(pid)
        except Exception:
            logger.debug("mcp-helper orphan reap failed for %s", entry, exc_info=True)
    if reaped:
        logger.info("reaped %d orphaned stdio MCP helper(s): %s", len(reaped), reaped)
    return reaped


# ---------------------------------------------------------------------------
# Layer 3 — Windows job-object self-attach
# ---------------------------------------------------------------------------

def attach_self_to_kill_on_close_job() -> bool:
    """Place this process in a job that dies (whole tree) when we die.

    Windows-only, best-effort, idempotent. ``BREAKAWAY_OK`` is included so
    children spawned with ``CREATE_BREAKAWAY_FROM_JOB`` (gateway relaunch
    during update, detached watchers) keep escaping exactly as they do today.
    Nested jobs are supported since Windows 8, so being inside another job
    (Terminal, CI runners) does not prevent the attach on any supported OS.
    """
    global _JOB_HANDLE
    if not _IS_WINDOWS or _JOB_HANDLE is not None:
        return _JOB_HANDLE is not None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
        JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
        JobObjectExtendedLimitInformation = 9

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_BREAKAWAY_OK
            | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        )
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(job)
            return False
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(job)
            return False
        _JOB_HANDLE = job  # keep alive for the life of the process — never close
        logger.debug("attached to kill-on-close job object")
        return True
    except Exception:
        logger.debug("job object self-attach failed", exc_info=True)
        return False

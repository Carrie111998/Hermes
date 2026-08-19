"""Remoko keep-alive when a worker burns its iteration budget (LS-2777).

Budget exhaustion is an owner fork, not a spawn/crash failure. A live
Kanban card (or a delegate_task / Bot Chat sidecar) stays parked until
one Remoko tap grants +90 turns, parks, or stops the work.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Protocol

try:
    import fcntl as _fcntl
except ImportError:  # Windows — sidecar claim still serializes in-process.
    _fcntl = None

logger = logging.getLogger(__name__)

CHOICE_GIVE_MORE = "Give 90 more"
CHOICE_GIVE_ONCE = "Give 90 once"
CHOICE_PARK = "Park it"
CHOICE_STOP = "Stop this"
CHOICES = (CHOICE_GIVE_MORE, CHOICE_GIVE_ONCE, CHOICE_PARK, CHOICE_STOP)

STATUS_PENDING = "pending"
STATUS_GRANTED = "granted"
STATUS_PARKED = "parked"
STATUS_STOPPED = "stopped"

POLICY_EXTEND_REPEAT = "extend_repeat"
POLICY_EXTEND_ONCE = "extend_once"
POLICY_PARK = "park"
POLICY_STOP = "stop"

EXTENSION_TURNS = 90
MAX_RECOMMENDED_GRANTS = 3
BUDGET_EXHAUST_NEEDLE = "Iteration budget exhausted"
DEFAULT_BASE_MAX_TURNS = 90
HEADLINE = "Keep this work going?"
SEND_CLAIM_PREFIX = "sending:"
SEND_CLAIM_TTL_SECONDS = 30.0
_SIDECAR_THREAD_LOCK = threading.Lock()

DECISION_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_GRANTED, STATUS_PARKED, STATUS_STOPPED}
)
BLOCKING_DECISION_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_PARKED, STATUS_STOPPED}
)

BUDGET_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS kanban_budget_decisions (
    task_id              TEXT PRIMARY KEY,
    request_id           TEXT,
    external_id          TEXT,
    status               TEXT NOT NULL,
    policy               TEXT,
    extensions_count     INTEGER NOT NULL DEFAULT 0,
    extra_turns          INTEGER NOT NULL DEFAULT 0,
    last_budget_burn_at  INTEGER,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
)
"""

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


class RemokoClient(Protocol):
    def ask_question(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def report_execution(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def mark_processed(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_response(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass
class BudgetDecision:
    task_id: str
    request_id: Optional[str] = None
    external_id: Optional[str] = None
    status: str = STATUS_PENDING
    policy: Optional[str] = None
    extensions_count: int = 0
    extra_turns: int = 0
    last_budget_burn_at: Optional[int] = None
    created_at: int = 0
    updated_at: int = 0
    store: str = "kanban"  # "kanban" | "sidecar"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request_id": self.request_id or "",
            "external_id": self.external_id or "",
            "status": self.status,
            "policy": self.policy or "",
            "extensions_count": int(self.extensions_count),
            "extra_turns": int(self.extra_turns),
            "last_budget_burn_at": self.last_budget_burn_at,
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
        }


class NullRemokoClient:
    """No network. Used under pytest when a test did not inject a client."""

    def ask_question(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "remoko disabled (inject remoko_client)"}

    def report_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "remoko disabled (inject remoko_client)"}

    def mark_processed(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "remoko disabled (inject remoko_client)"}

    def get_response(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "pending"}


class RecordingRemokoClient:
    """In-memory Remoko double for unit tests. Never hits the live inbox."""

    def __init__(self) -> None:
        self.ask_calls: list[dict[str, Any]] = []
        self.report_calls: list[dict[str, Any]] = []
        self.mark_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self._n = 0
        self.fail_ask = False
        self.answers: dict[str, str] = {}

    def ask_question(self, **kwargs: Any) -> dict[str, Any]:
        self.ask_calls.append(dict(kwargs))
        if self.fail_ask:
            raise RuntimeError("remoko send failed")
        self._n += 1
        request_id = f"req-budget-{self._n}"
        return {"ok": True, "request_id": request_id, "external_id": kwargs.get("external_id")}

    def report_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.report_calls.append(dict(kwargs))
        return {"ok": True}

    def mark_processed(self, **kwargs: Any) -> dict[str, Any]:
        self.mark_calls.append(dict(kwargs))
        return {"ok": True}

    def get_response(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(dict(kwargs))
        request_id = str(kwargs.get("request_id") or "")
        choice = self.answers.get(request_id)
        if not choice:
            return {"status": "pending", "request_id": request_id}
        return {
            "status": "answered",
            "request_id": request_id,
            "response": {"choice": choice},
        }


class LiveRemokoClient:
    """Best-effort live Remoko MCP caller. Never used under pytest."""

    def ask_question(self, **kwargs: Any) -> dict[str, Any]:
        return _invoke_remoko_tool("ask_question", kwargs)

    def report_execution(self, **kwargs: Any) -> dict[str, Any]:
        return _invoke_remoko_tool("report_execution", kwargs)

    def mark_processed(self, **kwargs: Any) -> dict[str, Any]:
        return _invoke_remoko_tool("mark_processed", kwargs)

    def get_response(self, **kwargs: Any) -> dict[str, Any]:
        return _invoke_remoko_tool("get_response", kwargs)


def default_remoko_client() -> RemokoClient:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return NullRemokoClient()
    return LiveRemokoClient()


def _invoke_remoko_tool(suffix: str, args: Mapping[str, Any]) -> dict[str, Any]:
    name = f"mcp__remoko__{suffix}"
    try:
        from tools.registry import registry

        handler = None
        get = getattr(registry, "get_handler", None) or getattr(registry, "get", None)
        if callable(get):
            handler = get(name)
        if handler is None:
            tools = getattr(registry, "tools", None) or getattr(registry, "_tools", None)
            if isinstance(tools, dict):
                entry = tools.get(name) or {}
                handler = entry.get("handler") if isinstance(entry, dict) else None
        if handler is None:
            return {"ok": False, "error": f"{name} not registered"}
        raw = handler(dict(args))
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "error": raw[:300], "raw": raw}
            return parsed if isinstance(parsed, dict) else {"ok": True, "result": parsed}
        return {"ok": True, "result": raw}
    except Exception as exc:
        logger.warning("remoko %s failed: %s", suffix, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


def effective_kanban_max_iterations(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[int]:
    """Return HERMES_KANBAN_MAX_ITERATIONS when this process is a kanban worker.

    Honoured ahead of ``agent.max_turns`` so a granted +90 survives restart.
    Process-env reads are identity-gated: cron / delegate_task children
    inherit the worker's os.environ but must not take its iteration lock.
    An explicit *env* mapping (dispatcher spawn, unit tests) is trusted
    as-is because the caller is constructing a worker identity.
    """
    if env is None:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if not is_dispatcher_owned_worker_context():
            return None
        source = os.environ
    else:
        source = env
    if not str(source.get("HERMES_KANBAN_TASK") or "").strip():
        return None
    raw = source.get("HERMES_KANBAN_MAX_ITERATIONS")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_base_max_turns(hermes_home: Optional[str] = None) -> int:
    try:
        from hermes_cli.config import load_config

        cfg: Mapping[str, Any]
        if hermes_home:
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override

            token = set_hermes_home_override(hermes_home)
            try:
                cfg = load_config() or {}
            finally:
                reset_hermes_home_override(token)
        else:
            cfg = load_config() or {}
        agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        raw = (agent_cfg or {}).get("max_turns") or cfg.get("max_turns") or DEFAULT_BASE_MAX_TURNS
        value = int(raw)
        return value if value > 0 else DEFAULT_BASE_MAX_TURNS
    except Exception:
        return DEFAULT_BASE_MAX_TURNS


def worker_max_iterations_value(
    extra_turns: int,
    *,
    hermes_home: Optional[str] = None,
    base_max_turns: Optional[int] = None,
) -> int:
    base = (
        int(base_max_turns)
        if base_max_turns is not None
        else resolve_base_max_turns(hermes_home)
    )
    extra = max(0, int(extra_turns or 0))
    return max(1, base + extra)


def ensure_budget_decisions_table(conn: sqlite3.Connection) -> None:
    conn.execute(BUDGET_DECISIONS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_budget_decisions_status "
        "ON kanban_budget_decisions(status)"
    )


def external_id_for(task_id: str, extensions_count: int) -> str:
    return f"obj-{task_id}-budget-{int(extensions_count) + 1}"


def recommended_choice(extensions_count: int) -> str:
    return CHOICE_PARK if int(extensions_count) >= MAX_RECOMMENDED_GRANTS else CHOICE_GIVE_MORE


def build_remoko_card(
    *,
    task_id: str,
    extensions_count: int,
    budget_used: int,
    budget_max: int,
    extra_turns: int = 0,
) -> dict[str, Any]:
    recommend = recommended_choice(extensions_count)
    after_three = int(extensions_count) >= MAX_RECOMMENDED_GRANTS
    if after_three:
        purpose_extra = (
            " This card has already been given three extra bursts, so the "
            "recommended tap is now Park it — do not silently grant a fourth."
        )
        why = "Three extra bursts already landed; parking keeps the work without burning more turns."
        next_step = "If you tap Park it, the same card stays paused until a later tap."
    else:
        purpose_extra = ""
        why = "The work already started; killing it wastes the progress."
        next_step = "If you tap Give 90 more, I add 90 turns and restart the same card."

    context = (
        f"This job ran out of turns mid-pipeline ({budget_used}/{budget_max}). "
        f"Without a tap it will sit dead.{purpose_extra}\n\n"
        f"{next_step}\n\n"
        f"{CHOICE_GIVE_MORE} — plus: the same job keeps going. minus: it can ask again later.\n"
        f"{CHOICE_GIVE_ONCE} — plus: one more burst, then it stops asking. minus: the next burn parks with no new card.\n"
        f"{CHOICE_PARK} — plus: nothing more runs until you come back. minus: the work stays paused.\n"
        f"{CHOICE_STOP} — plus: no more auto-runs. minus: the started work stays unfinished.\n\n"
        f"Why: {why}"
    )
    if extra_turns:
        context += f"\n\nAlready added: {extra_turns} extra turns."
    context += f"\nCard: {task_id}"

    consequence = (
        "The same card stays paused. No merge, deploy, or restart."
        if recommend == CHOICE_PARK
        else "The same card gets 90 more turns and starts again. No merge, deploy, or restart."
    )
    return {
        "question": HEADLINE,
        "context": context[:2000],
        "choices": list(CHOICES),
        "allow_freeform": False,
        "priority": "high",
        "agent_name": "Hermes",
        "wait_seconds": 0,
        "external_id": external_id_for(task_id, extensions_count),
        "project": task_id,
        "risk": "medium",
        "recommendation": f"{recommend} — {why}"[:1000],
        "consequence": consequence[:1000],
        "prohibitions": [
            "No merge from this tap",
            "No deploy from this tap",
            "No gateway or dispatcher restart from this tap",
            "No second Remoko card for this same burn",
        ],
    }


def _now() -> int:
    return int(time.time())


def _row_to_decision(row: sqlite3.Row, *, store: str = "kanban") -> BudgetDecision:
    return BudgetDecision(
        task_id=row["task_id"],
        request_id=(row["request_id"] or None) or None,
        external_id=(row["external_id"] or None) or None,
        status=row["status"],
        policy=(row["policy"] or None) or None,
        extensions_count=int(row["extensions_count"] or 0),
        extra_turns=int(row["extra_turns"] or 0),
        last_budget_burn_at=row["last_budget_burn_at"],
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        store=store,
    )


def get_decision(conn: sqlite3.Connection, task_id: str) -> Optional[BudgetDecision]:
    ensure_budget_decisions_table(conn)
    row = conn.execute(
        "SELECT * FROM kanban_budget_decisions WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return _row_to_decision(row) if row else None


def extra_turns_for_task(task_id: str, conn: Optional[sqlite3.Connection] = None) -> int:
    owns = conn is None
    if owns:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
    try:
        decision = get_decision(conn, task_id)
        return int(decision.extra_turns) if decision else 0
    except Exception:
        return 0
    finally:
        if owns:
            try:
                conn.close()
            except Exception:
                pass


def budget_decision_blocks_dispatch(
    conn: sqlite3.Connection, task_id: str
) -> bool:
    decision = get_decision(conn, task_id)
    return bool(decision and decision.status in BLOCKING_DECISION_STATUSES)


def _upsert_decision(conn: sqlite3.Connection, decision: BudgetDecision) -> None:
    ensure_budget_decisions_table(conn)
    now = _now()
    decision.updated_at = now
    if not decision.created_at:
        decision.created_at = now
    conn.execute(
        """
        INSERT INTO kanban_budget_decisions (
            task_id, request_id, external_id, status, policy,
            extensions_count, extra_turns, last_budget_burn_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            request_id = excluded.request_id,
            external_id = excluded.external_id,
            status = excluded.status,
            policy = excluded.policy,
            extensions_count = excluded.extensions_count,
            extra_turns = excluded.extra_turns,
            last_budget_burn_at = excluded.last_budget_burn_at,
            updated_at = excluded.updated_at
        """,
        (
            decision.task_id,
            decision.request_id or "",
            decision.external_id or "",
            decision.status,
            decision.policy or "",
            int(decision.extensions_count),
            int(decision.extra_turns),
            decision.last_budget_burn_at,
            int(decision.created_at),
            int(decision.updated_at),
        ),
    )


def sidecar_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        raw = os.environ.get("HERMES_HOME")
        home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    return home / "state" / "budget-keepalive"


def _safe_sidecar_name(subject_id: str) -> str:
    cleaned = _SAFE_ID_RE.sub("-", str(subject_id or "").strip())
    return cleaned[:180] or "unknown"


def _sidecar_path(subject_id: str) -> Path:
    return sidecar_dir() / f"{_safe_sidecar_name(subject_id)}.json"


def load_sidecar(subject_id: str) -> Optional[BudgetDecision]:
    path = _sidecar_path(subject_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("unreadable budget sidecar %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    return BudgetDecision(
        task_id=str(payload.get("task_id") or subject_id),
        request_id=str(payload.get("request_id") or "") or None,
        external_id=str(payload.get("external_id") or "") or None,
        status=str(payload.get("status") or STATUS_PENDING),
        policy=str(payload.get("policy") or "") or None,
        extensions_count=int(payload.get("extensions_count") or 0),
        extra_turns=int(payload.get("extra_turns") or 0),
        last_budget_burn_at=payload.get("last_budget_burn_at"),
        created_at=int(payload.get("created_at") or 0),
        updated_at=int(payload.get("updated_at") or 0),
        store="sidecar",
    )


def save_sidecar(decision: BudgetDecision) -> None:
    path = _sidecar_path(decision.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    decision.updated_at = _now()
    if not decision.created_at:
        decision.created_at = decision.updated_at
    payload = decision.to_dict()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def is_bot_chat_agent(agent: Any) -> bool:
    try:
        from tools.bot_mode_probe import BOT_CHAT_TITLE
    except Exception:
        BOT_CHAT_TITLE = "Bot Chat"
    title = str(getattr(agent, "_session_title_hint", "") or "").strip()
    if not title:
        session_db = getattr(agent, "_session_db", None)
        session_id = getattr(agent, "session_id", None)
        getter = getattr(session_db, "get_session_title", None) if session_db else None
        if callable(getter) and session_id:
            try:
                title = str(getter(session_id) or "").strip()
            except Exception:
                title = ""
    return title == BOT_CHAT_TITLE


def sidecar_subject_id(agent: Any = None) -> Optional[str]:
    """Id for the sidecar store, or None when this burn belongs on the board."""
    if agent is not None:
        sub = getattr(agent, "_subagent_id", None)
        if isinstance(sub, str) and sub.strip():
            return sub.strip()
    try:
        from agent.delegation_context import is_delegated_child_process_context

        delegated = is_delegated_child_process_context()
    except Exception:
        delegated = bool(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"))
    if delegated:
        return (
            str(os.environ.get("HERMES_DELEGATE_ID") or "").strip()
            or str(getattr(agent, "session_id", "") or "").strip()
            or "delegated-child"
        )
    if agent is not None and is_bot_chat_agent(agent):
        return str(getattr(agent, "session_id", "") or "").strip() or "bot-chat"
    return None


def _attest_origin(
    args: Mapping[str, Any],
    *,
    task_id: str,
    run_id: Any,
    session_id: str,
) -> dict[str, Any]:
    """Reuse remoko-approval-delivery origin attestation when the lib is present."""
    out = dict(args)
    try:
        import sys

        lib = Path.home() / ".hermes" / "lib"
        if lib.is_dir():
            value = str(lib)
            if value in sys.path:
                sys.path.remove(value)
            sys.path.insert(0, value)
        import remoko_approval_delivery as rad

        turn_id = f"budget-{out.get('external_id') or task_id}"
        tool_call_id = f"keepalive-{out.get('external_id') or task_id}"
        run_token = str(run_id or os.environ.get("HERMES_KANBAN_RUN_ID") or "0")
        session_token = (
            session_id
            or str(os.environ.get("HERMES_SESSION_ID") or "").strip()
            or f"kanban-{task_id}"
        )
        origin = {
            "task_id": task_id,
            "run_id": run_token,
            "session_id": session_token,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
        }
        origin["source_thread"] = rad._origin_token(
            task_id=task_id,
            run_id=run_token,
            session_id=session_token,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )
        board_row = {
            "id": task_id,
            "task_id": task_id,
            "run_id": run_token,
        }
        rad.attest_origin_against_board(origin, board_row)
        out = rad.augment_request_args(out, origin)
        rad.ensure_request(out, origin)
    except Exception as exc:
        logger.debug("remoko origin attestation skipped: %s", exc)
        out.setdefault("project", task_id)
        out.setdefault("source_thread", f"hermes:kanban:{task_id}:budget")
    return out


def _extract_request_id(result: Mapping[str, Any]) -> str:
    for key in ("request_id", "id"):
        value = result.get(key)
        if value:
            return str(value)
    nested = result.get("result")
    if isinstance(nested, dict):
        for key in ("request_id", "id"):
            value = nested.get(key)
            if value:
                return str(value)
    return ""


def _send_card(
    remoko_client: RemokoClient,
    card: Mapping[str, Any],
    *,
    task_id: str,
    run_id: Any = None,
    session_id: str = "",
) -> str:
    enriched = _attest_origin(
        card, task_id=task_id, run_id=run_id, session_id=session_id
    )
    result = remoko_client.ask_question(**enriched)
    if not isinstance(result, Mapping):
        return ""
    return _extract_request_id(result)


def _new_send_claim() -> str:
    return f"{SEND_CLAIM_PREFIX}{os.getpid()}:{time.time_ns()}:{secrets.token_hex(8)}"


def _send_claim_age_seconds(request_id: Optional[str]) -> Optional[float]:
    rid = str(request_id or "").strip()
    if not rid.startswith(SEND_CLAIM_PREFIX):
        return None
    parts = rid.split(":")
    if len(parts) < 4:
        return float("inf")
    try:
        minted_ns = int(parts[2])
    except ValueError:
        return float("inf")
    return max(0.0, (time.time_ns() - minted_ns) / 1e9)


def _has_live_request_id(request_id: Optional[str]) -> bool:
    rid = str(request_id or "").strip()
    return bool(rid) and _send_claim_age_seconds(rid) is None


def _request_is_claimable(request_id: Optional[str]) -> bool:
    """Empty or a stale sending token may be claimed. A live Remoko id may not."""
    rid = str(request_id or "").strip()
    if not rid:
        return True
    age = _send_claim_age_seconds(rid)
    if age is None:
        return False
    return age >= SEND_CLAIM_TTL_SECONDS


def _send_claimed_card(
    remoko_client: RemokoClient,
    card: Mapping[str, Any],
    *,
    task_id: str,
    session_id: str = "",
) -> str:
    try:
        return _send_card(
            remoko_client, card, task_id=task_id, session_id=session_id
        )
    except Exception as exc:
        logger.warning("remoko ask_question failed for %s: %s", task_id, exc)
        return ""


def _complete_or_clear_kanban_claim(
    conn: sqlite3.Connection,
    decision: BudgetDecision,
    claim: str,
    request_id: str,
) -> None:
    from hermes_cli import kanban_db as kb

    now = _now()
    persisted = request_id or ""
    with kb.write_txn(conn, allow_nested=True):
        cur = conn.execute(
            """
            UPDATE kanban_budget_decisions
               SET request_id = ?, updated_at = ?
             WHERE task_id = ?
               AND request_id = ?
            """,
            (persisted, now, decision.task_id, claim),
        )
        if cur.rowcount == 1:
            decision.request_id = persisted or None
            decision.updated_at = now
            return
        live = get_decision(conn, decision.task_id)
        if live is not None:
            decision.request_id = live.request_id
            decision.status = live.status
            decision.extra_turns = live.extra_turns
            decision.updated_at = live.updated_at


@contextlib.contextmanager
def _sidecar_lock(subject_id: str) -> Iterator[None]:
    path = _sidecar_path(subject_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    handle = open(lock_path, "a+b")
    _SIDECAR_THREAD_LOCK.acquire()
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if _fcntl is not None:
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
        _SIDECAR_THREAD_LOCK.release()


def _complete_or_clear_sidecar_claim(
    subject_id: str,
    decision: BudgetDecision,
    claim: str,
    request_id: str,
) -> None:
    with _sidecar_lock(subject_id):
        live = load_sidecar(subject_id)
        if live is None or live.request_id != claim:
            if live is not None:
                decision.request_id = live.request_id
                decision.status = live.status
                decision.extra_turns = live.extra_turns
                decision.updated_at = live.updated_at
            return
        live.request_id = request_id or None
        save_sidecar(live)
        decision.request_id = live.request_id
        decision.status = live.status
        decision.updated_at = live.updated_at


def _answered_choice(payload: Any) -> str:
    if not isinstance(payload, Mapping) or payload.get("status") != "answered":
        return ""
    response = payload.get("response")
    if not isinstance(response, Mapping):
        response = payload
    return _normalize_answer(response)


def _park_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    budget_used: int,
    budget_max: int,
    reason: str,
    payload: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    from hermes_cli import kanban_db as kb

    run_id = kb._end_run(
        conn,
        task_id,
        outcome="budget_exhausted",
        status="budget_exhausted",
        error=reason[:500],
        metadata={
            "budget_used": budget_used,
            "budget_max": budget_max,
            **(payload or {}),
        },
    )
    conn.execute(
        """
        UPDATE tasks
           SET status = 'blocked',
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               block_kind = 'needs_input'
         WHERE id = ?
           AND status IN ('running', 'ready', 'blocked')
        """,
        (task_id,),
    )
    event_payload = {
        "budget_used": budget_used,
        "budget_max": budget_max,
        "error": reason[:500],
        **(payload or {}),
    }
    kb._append_event(
        conn, task_id, "budget_exhausted", event_payload, run_id=run_id
    )
    return run_id


def _consecutive_failures(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return 0
    return int(row["consecutive_failures"] or 0)


def record_kanban_budget_exhausted(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    budget_used: int,
    budget_max: int,
    remoko_client: Optional[RemokoClient] = None,
    session_id: str = "",
) -> BudgetDecision:
    """First-class keep-alive for a live Kanban task."""
    from hermes_cli import kanban_db as kb

    client = remoko_client or default_remoko_client()
    ensure_budget_decisions_table(conn)
    failures_before = _consecutive_failures(conn, task_id)
    now = _now()
    reason = (
        f"{BUDGET_EXHAUST_NEEDLE} ({budget_used}/{budget_max}) — "
        "task could not complete within the allowed iterations"
    )
    claim = _new_send_claim()
    send_card: Optional[dict[str, Any]] = None
    decision: Optional[BudgetDecision] = None

    with kb.write_txn(conn, allow_nested=True):
        existing = get_decision(conn, task_id)

        if existing and existing.status in (
            STATUS_PENDING,
            STATUS_PARKED,
            STATUS_STOPPED,
        ):
            if existing.status == STATUS_PENDING and _request_is_claimable(
                existing.request_id
            ):
                send_card = build_remoko_card(
                    task_id=task_id,
                    extensions_count=existing.extensions_count,
                    budget_used=budget_used,
                    budget_max=budget_max,
                    extra_turns=existing.extra_turns,
                )
                existing.request_id = claim
                existing.external_id = existing.external_id or send_card["external_id"]
                _upsert_decision(conn, existing)
            decision = existing
        elif (
            existing
            and existing.status == STATUS_GRANTED
            and existing.policy == POLICY_EXTEND_ONCE
        ):
            existing.status = STATUS_PARKED
            existing.policy = POLICY_PARK
            existing.last_budget_burn_at = now
            existing.external_id = existing.external_id or external_id_for(
                task_id, existing.extensions_count
            )
            _park_task(
                conn,
                task_id,
                budget_used=budget_used,
                budget_max=budget_max,
                reason=reason,
                payload={
                    "auto_parked": True,
                    "policy": POLICY_EXTEND_ONCE,
                    "external_id": existing.external_id,
                    "consecutive_failures": failures_before,
                },
            )
            _upsert_decision(conn, existing)
            if _consecutive_failures(conn, task_id) != failures_before:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
                    (failures_before, task_id),
                )
            decision = existing
        else:
            extensions_count = existing.extensions_count if existing else 0
            extra_turns = existing.extra_turns if existing else 0
            created_at = existing.created_at if existing else now
            send_card = build_remoko_card(
                task_id=task_id,
                extensions_count=extensions_count,
                budget_used=budget_used,
                budget_max=budget_max,
                extra_turns=extra_turns,
            )
            decision = BudgetDecision(
                task_id=task_id,
                request_id=claim,
                external_id=send_card["external_id"],
                status=STATUS_PENDING,
                policy="",
                extensions_count=extensions_count,
                extra_turns=extra_turns,
                last_budget_burn_at=now,
                created_at=created_at,
                updated_at=now,
            )
            _park_task(
                conn,
                task_id,
                budget_used=budget_used,
                budget_max=budget_max,
                reason=reason,
                payload={
                    "external_id": decision.external_id,
                    "consecutive_failures": failures_before,
                },
            )
            _upsert_decision(conn, decision)
            if _consecutive_failures(conn, task_id) != failures_before:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
                    (failures_before, task_id),
                )

    assert decision is not None
    if send_card is None:
        return decision

    request_id = _send_claimed_card(
        client, send_card, task_id=task_id, session_id=session_id
    )
    _complete_or_clear_kanban_claim(conn, decision, claim, request_id)
    return decision


def record_sidecar_budget_exhausted(
    subject_id: str,
    *,
    budget_used: int,
    budget_max: int,
    remoko_client: Optional[RemokoClient] = None,
    session_id: str = "",
) -> BudgetDecision:
    client = remoko_client or default_remoko_client()
    now = _now()
    claim = _new_send_claim()
    send_card: Optional[dict[str, Any]] = None
    decision: Optional[BudgetDecision] = None

    with _sidecar_lock(subject_id):
        existing = load_sidecar(subject_id)
        if existing and existing.status in (
            STATUS_PENDING,
            STATUS_PARKED,
            STATUS_STOPPED,
        ):
            if existing.status == STATUS_PENDING and _request_is_claimable(
                existing.request_id
            ):
                send_card = build_remoko_card(
                    task_id=subject_id,
                    extensions_count=existing.extensions_count,
                    budget_used=budget_used,
                    budget_max=budget_max,
                    extra_turns=existing.extra_turns,
                )
                existing.request_id = claim
                existing.external_id = existing.external_id or send_card["external_id"]
                save_sidecar(existing)
            decision = existing
        elif (
            existing
            and existing.status == STATUS_GRANTED
            and existing.policy == POLICY_EXTEND_ONCE
        ):
            existing.status = STATUS_PARKED
            existing.policy = POLICY_PARK
            existing.last_budget_burn_at = now
            save_sidecar(existing)
            decision = existing
        else:
            extensions_count = existing.extensions_count if existing else 0
            extra_turns = existing.extra_turns if existing else 0
            send_card = build_remoko_card(
                task_id=subject_id,
                extensions_count=extensions_count,
                budget_used=budget_used,
                budget_max=budget_max,
                extra_turns=extra_turns,
            )
            decision = BudgetDecision(
                task_id=subject_id,
                request_id=claim,
                external_id=send_card["external_id"],
                status=STATUS_PENDING,
                policy="",
                extensions_count=extensions_count,
                extra_turns=extra_turns,
                last_budget_burn_at=now,
                created_at=existing.created_at if existing else now,
                store="sidecar",
            )
            save_sidecar(decision)

    assert decision is not None
    if send_card is None:
        return decision

    request_id = _send_claimed_card(
        client, send_card, task_id=subject_id, session_id=session_id
    )
    _complete_or_clear_sidecar_claim(subject_id, decision, claim, request_id)
    return decision


def record_iteration_budget_exhausted(
    *,
    task_id: Optional[str] = None,
    budget_used: int,
    budget_max: int,
    agent: Any = None,
    remoko_client: Optional[RemokoClient] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[BudgetDecision]:
    """Route a budget burn to the board table or a sidecar. Never a death count."""
    sidecar_id = sidecar_subject_id(agent)
    if sidecar_id:
        return record_sidecar_budget_exhausted(
            sidecar_id,
            budget_used=budget_used,
            budget_max=budget_max,
            remoko_client=remoko_client,
            session_id=str(getattr(agent, "session_id", "") or ""),
        )

    live_task = str(task_id or "").strip()
    if not live_task:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        if is_dispatcher_owned_worker_context():
            live_task = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not live_task:
        return None

    owns = conn is None
    if owns:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
    try:
        return record_kanban_budget_exhausted(
            conn,
            live_task,
            budget_used=budget_used,
            budget_max=budget_max,
            remoko_client=remoko_client,
            session_id=str(getattr(agent, "session_id", "") or ""),
        )
    finally:
        if owns:
            try:
                conn.close()
            except Exception:
                pass


def _normalize_answer(answer: Any) -> str:
    if isinstance(answer, Mapping):
        for key in ("choice", "answer", "text", "value"):
            if answer.get(key):
                return str(answer[key]).strip()
    return str(answer or "").strip()


def _stale_reason(conn: sqlite3.Connection, task_id: str, decision: BudgetDecision) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if task is None:
        return "task missing"
    if task.status in {"done", "archived"}:
        return f"task already {task.status}"
    burned_at = int(decision.last_budget_burn_at or 0)
    if burned_at:
        newer = conn.execute(
            """
            SELECT 1 FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
               AND ended_at IS NOT NULL
               AND ended_at >= ?
             LIMIT 1
            """,
            (task_id, burned_at),
        ).fetchone()
        if newer:
            return "a newer run already completed"
    return None


def consume_budget_decision(
    conn: sqlite3.Connection,
    task_id: str,
    answer: Any,
    *,
    remoko_client: Optional[RemokoClient] = None,
) -> dict[str, Any]:
    """Apply one Remoko tap after revalidation. Never increment the death counter."""
    from hermes_cli import kanban_db as kb

    client = remoko_client or default_remoko_client()
    decision = get_decision(conn, task_id)
    if decision is None:
        return {"ok": False, "reason": "no budget decision", "applied": False}

    choice = _normalize_answer(answer)
    request_id = decision.request_id or ""

    def _report(outcome: str, note: str) -> None:
        if not request_id:
            return
        try:
            client.report_execution(
                request_id=request_id,
                outcome=outcome,
                note=note[:1000],
            )
        except Exception as exc:
            logger.warning("report_execution failed for %s: %s", task_id, exc)

    def _mark() -> None:
        if not request_id:
            return
        try:
            client.mark_processed(request_id=request_id)
        except Exception as exc:
            logger.warning("mark_processed failed for %s: %s", task_id, exc)

    if decision.status == STATUS_STOPPED and choice != CHOICE_STOP:
        _report("failed", "This card was already stopped.")
        _mark()
        return {"ok": False, "reason": "stopped", "applied": False, "stale": True}

    if decision.status == STATUS_GRANTED:
        _report("failed", "This tap was already used.")
        _mark()
        return {"ok": False, "reason": "already granted", "applied": False, "stale": True}

    stale = _stale_reason(conn, task_id, decision)
    if stale:
        _report("failed", f"Stale tap: {stale}. No extra turns applied.")
        _mark()
        return {"ok": False, "reason": stale, "applied": False, "stale": True}

    if choice == CHOICE_GIVE_MORE:
        with kb.write_txn(conn):
            # unblock_task opens its own txn; apply grant fields first, then unblock.
            decision.extra_turns = int(decision.extra_turns) + EXTENSION_TURNS
            decision.extensions_count = int(decision.extensions_count) + 1
            decision.status = STATUS_GRANTED
            decision.policy = POLICY_EXTEND_REPEAT
            _upsert_decision(conn, decision)
        task = kb.get_task(conn, task_id)
        if task and task.status == "blocked":
            kb.unblock_task(conn, task_id)
        _report("completed", "Added 90 turns and restarted the same card.")
        _mark()
        return {
            "ok": True,
            "applied": True,
            "choice": choice,
            "extra_turns": decision.extra_turns,
            "policy": decision.policy,
            "max_iterations": worker_max_iterations_value(decision.extra_turns),
        }

    if choice == CHOICE_GIVE_ONCE:
        with kb.write_txn(conn):
            decision.extra_turns = int(decision.extra_turns) + EXTENSION_TURNS
            decision.extensions_count = int(decision.extensions_count) + 1
            decision.status = STATUS_GRANTED
            decision.policy = POLICY_EXTEND_ONCE
            _upsert_decision(conn, decision)
        task = kb.get_task(conn, task_id)
        if task and task.status == "blocked":
            kb.unblock_task(conn, task_id)
        _report("completed", "Added 90 turns once. The next burn will park with no new card.")
        _mark()
        return {
            "ok": True,
            "applied": True,
            "choice": choice,
            "extra_turns": decision.extra_turns,
            "policy": decision.policy,
            "max_iterations": worker_max_iterations_value(decision.extra_turns),
        }

    if choice == CHOICE_PARK:
        with kb.write_txn(conn):
            decision.status = STATUS_PARKED
            decision.policy = POLICY_PARK
            _upsert_decision(conn, decision)
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'blocked',
                       block_kind = 'needs_input'
                 WHERE id = ?
                   AND status IN ('blocked', 'ready', 'running')
                """,
                (task_id,),
            )
        _report("completed", "Parked. The card waits for a later tap.")
        _mark()
        return {"ok": True, "applied": True, "choice": choice, "status": STATUS_PARKED}

    if choice == CHOICE_STOP:
        with kb.write_txn(conn):
            decision.status = STATUS_STOPPED
            decision.policy = POLICY_STOP
            _upsert_decision(conn, decision)
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'blocked',
                       block_kind = 'needs_input'
                 WHERE id = ?
                   AND status IN ('blocked', 'ready', 'running')
                """,
                (task_id,),
            )
        _report("completed", "Stopped. This card will not be dispatched.")
        _mark()
        return {"ok": True, "applied": True, "choice": choice, "status": STATUS_STOPPED}

    _report("failed", f"Unknown tap {choice!r}.")
    return {"ok": False, "reason": f"unknown choice {choice!r}", "applied": False}


def consume_sidecar_budget_decision(
    subject_id: str,
    answer: Any,
    *,
    remoko_client: Optional[RemokoClient] = None,
) -> dict[str, Any]:
    client = remoko_client or default_remoko_client()
    decision = load_sidecar(subject_id)
    if decision is None:
        return {"ok": False, "reason": "no budget decision", "applied": False}
    choice = _normalize_answer(answer)
    request_id = decision.request_id or ""

    def _report(outcome: str, note: str) -> None:
        if not request_id:
            return
        try:
            client.report_execution(request_id=request_id, outcome=outcome, note=note[:1000])
        except Exception as exc:
            logger.warning("sidecar report_execution failed: %s", exc)

    def _mark() -> None:
        if not request_id:
            return
        try:
            client.mark_processed(request_id=request_id)
        except Exception as exc:
            logger.warning("sidecar mark_processed failed: %s", exc)

    if decision.status == STATUS_STOPPED and choice != CHOICE_STOP:
        _report("failed", "This work was already stopped.")
        _mark()
        return {"ok": False, "reason": "stopped", "applied": False, "stale": True}
    if decision.status == STATUS_GRANTED:
        _report("failed", "This tap was already used.")
        _mark()
        return {"ok": False, "reason": "already granted", "applied": False, "stale": True}

    if choice == CHOICE_GIVE_MORE:
        decision.extra_turns += EXTENSION_TURNS
        decision.extensions_count += 1
        decision.status = STATUS_GRANTED
        decision.policy = POLICY_EXTEND_REPEAT
        save_sidecar(decision)
        _report("completed", "Added 90 turns.")
        _mark()
        return {"ok": True, "applied": True, "choice": choice, "extra_turns": decision.extra_turns}
    if choice == CHOICE_GIVE_ONCE:
        decision.extra_turns += EXTENSION_TURNS
        decision.extensions_count += 1
        decision.status = STATUS_GRANTED
        decision.policy = POLICY_EXTEND_ONCE
        save_sidecar(decision)
        _report("completed", "Added 90 turns once.")
        _mark()
        return {"ok": True, "applied": True, "choice": choice, "extra_turns": decision.extra_turns}
    if choice == CHOICE_PARK:
        decision.status = STATUS_PARKED
        decision.policy = POLICY_PARK
        save_sidecar(decision)
        _report("completed", "Parked.")
        _mark()
        return {"ok": True, "applied": True, "choice": choice, "status": STATUS_PARKED}
    if choice == CHOICE_STOP:
        decision.status = STATUS_STOPPED
        decision.policy = POLICY_STOP
        save_sidecar(decision)
        _report("completed", "Stopped.")
        _mark()
        return {"ok": True, "applied": True, "choice": choice, "status": STATUS_STOPPED}
    return {"ok": False, "reason": f"unknown choice {choice!r}", "applied": False}


def reconcile_budget_keepalive(
    conn: sqlite3.Connection,
    *,
    remoko_client: Optional[RemokoClient] = None,
) -> dict[str, Any]:
    """Retry unsent pending cards and apply answered taps. Never auto-resumes."""
    from hermes_cli import kanban_db as kb

    client = remoko_client or default_remoko_client()
    ensure_budget_decisions_table(conn)
    retried = 0
    consumed = 0
    rows = conn.execute(
        "SELECT * FROM kanban_budget_decisions WHERE status = ?",
        (STATUS_PENDING,),
    ).fetchall()
    for row in rows:
        decision = _row_to_decision(row)
        if not _has_live_request_id(decision.request_id):
            if not _request_is_claimable(decision.request_id):
                continue
            card = build_remoko_card(
                task_id=decision.task_id,
                extensions_count=decision.extensions_count,
                budget_used=0,
                budget_max=0,
                extra_turns=decision.extra_turns,
            )
            card["external_id"] = decision.external_id or card["external_id"]
            claim = _new_send_claim()
            with kb.write_txn(conn):
                live = get_decision(conn, decision.task_id)
                if (
                    live is None
                    or live.status != STATUS_PENDING
                    or not _request_is_claimable(live.request_id)
                ):
                    continue
                live.request_id = claim
                live.external_id = live.external_id or card["external_id"]
                _upsert_decision(conn, live)
            request_id = _send_claimed_card(client, card, task_id=decision.task_id)
            _complete_or_clear_kanban_claim(conn, decision, claim, request_id)
            if request_id:
                retried += 1
            continue
        try:
            payload = client.get_response(request_id=decision.request_id)
        except Exception:
            continue
        choice = _answered_choice(payload)
        if not choice:
            continue
        result = consume_budget_decision(
            conn, decision.task_id, choice, remoko_client=client
        )
        if result.get("ok"):
            consumed += 1

    sidecar_retried, sidecar_consumed = _reconcile_sidecars(client)
    return {
        "retried": retried,
        "consumed": consumed,
        "sidecar_retried": sidecar_retried,
        "sidecar_consumed": sidecar_consumed,
    }


def _reconcile_sidecars(client: RemokoClient) -> tuple[int, int]:
    root = sidecar_dir()
    if not root.is_dir():
        return 0, 0
    retried = 0
    consumed = 0
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("status") != STATUS_PENDING:
            continue
        subject_id = str(payload.get("task_id") or path.stem)
        request_id = str(payload.get("request_id") or "") or None
        if _has_live_request_id(request_id):
            try:
                response = client.get_response(request_id=request_id)
            except Exception:
                continue
            choice = _answered_choice(response)
            if not choice:
                continue
            result = consume_sidecar_budget_decision(
                subject_id, choice, remoko_client=client
            )
            if result.get("ok"):
                consumed += 1
            continue
        if not _request_is_claimable(request_id):
            continue
        card = build_remoko_card(
            task_id=subject_id,
            extensions_count=int(payload.get("extensions_count") or 0),
            budget_used=0,
            budget_max=0,
            extra_turns=int(payload.get("extra_turns") or 0),
        )
        card["external_id"] = payload.get("external_id") or card["external_id"]
        claim = _new_send_claim()
        with _sidecar_lock(subject_id):
            live = load_sidecar(subject_id)
            if (
                live is None
                or live.status != STATUS_PENDING
                or not _request_is_claimable(live.request_id)
            ):
                continue
            live.request_id = claim
            live.external_id = live.external_id or card["external_id"]
            save_sidecar(live)
        sent = _send_claimed_card(client, card, task_id=subject_id)
        scratch = live if live is not None else BudgetDecision(task_id=subject_id)
        _complete_or_clear_sidecar_claim(subject_id, scratch, claim, sent)
        if sent:
            retried += 1
    return retried, consumed

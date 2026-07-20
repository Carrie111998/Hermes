"""hermes-v2 H-41 — ``/swarm`` routing: v1 fan-out vs. kanban-backed swarm.

Above a configurable worker-count (or expected-runtime) threshold, a
``/swarm`` dispatch is materialised as a durable Kanban swarm
(root → workers → verifier → synthesizer) on a *dispatchable* board so the
gateway's retry / heartbeat / crash-reclaim net covers it — instead of a
one-shot ``delegate_task`` fan-out (v1). Below the threshold, v1 is
unchanged.

This module is the version-controlled substance of H-41. The kimi-mode
plugin's ``hermes_swarm_mode.py`` is a thin shim that reads config, counts
the workers in a ``delegate_task`` batch, and calls
:func:`route_and_materialize_swarm`; it builds on the existing
:func:`hermes_cli.kanban_swarm.create_swarm` primitive and the P-71
:func:`hermes_cli.kanban_db.board_is_dispatchable` guard.

Design guardrails (see the H-41 report and the ``swarm-router`` /
``worker-failure-discipline`` skills):

* **Fail-safe.** Any doubt routes to the v1 fan-out. Nothing in here raises
  into the caller — a broken board, a DB error, or a bad config value all
  degrade to ``mode="v1"`` with a reason string.
* **Board opt-in is sacred (P-71).** An *existing* board is NEVER flipped to
  ``dispatchable``. Only a board this function freshly creates from the
  configured slug may be opted in — and only when ``kanban_board_autocreate``
  is enabled. A configured board that exists but is not dispatchable → v1.
* **Workspace isolation is not forced here.** Per ``swarm-router`` (#4/#14)
  the kanban-swarm primitive uses the runtime's scratch defaults; workspace
  isolation is a project/operator config decision, not an automatic router
  effect. The H-43 workspace contract (``output/result.json`` +
  ``output/artifacts/`` + input-MD5 check) is carried in the worker/verifier
  card *bodies* (see the protocol constants below), not a forced
  ``workspace_path``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Iterable, Mapping, Optional

from hermes_cli.kanban_swarm import SwarmCreated, SwarmWorkerSpec, create_swarm

logger = logging.getLogger(__name__)

# --- documented config defaults (keys live under the ``swarm`` namespace) ---
# NB: these are DOCUMENTED defaults only; H-41 does not write them into
# ``config.yaml``. See the H-41 report for the config-key table.
DEFAULT_KANBAN_BACKED_MIN_WORKERS = 4
DEFAULT_KANBAN_BACKED_MIN_RUNTIME_SECONDS: Optional[int] = None  # disabled
DEFAULT_SWARM_BOARD = "swarm-work"
DEFAULT_BOARD_AUTOCREATE = False
DEFAULT_VERIFIER_ASSIGNEE = "reviewer"
DEFAULT_SYNTHESIZER_ASSIGNEE = "writer"
DEFAULT_WORKER_PROFILE = "worker"

# H-43 worker card body contract (carried in the body, not a workspace flag).
H43_WORKER_PROTOCOL = (
    "\n\n## H-43 workspace contract\n"
    "- Work only inside your own task workspace; the runtime allocates a "
    "fresh scratch dir per worker — do not touch sibling workspaces.\n"
    "- On entry: `cd` into the workspace and verify the input MD5 before "
    "editing.\n"
    "- Declare your scope and an explicit NOT-list first (barrier "
    "constraint).\n"
    "- On success: write `output/result.json` with `status: \"ok\"` and an "
    "`artifacts` list (paths under `output/artifacts/`).\n"
    "- On failure/exception: write `output/result.json` with "
    "`status: \"failed\"` and an `error` field before exiting.\n"
    "- An empty result is a failure — write result.json no matter what.\n"
)

# Worker-failure-discipline gate for the verifier card body.
VERIFIER_DISCIPLINE_PROTOCOL = (
    "\n\n## Worker-failure-discipline gate\n"
    "- Read every worker's `output/result.json` FROM DISK; never trust exit "
    "code 0 or a worker's claim.\n"
    "- Missing result.json, empty `output/`, unparseable json, "
    "`status != ok`, or a declared artifact missing on disk = FAIL.\n"
    "- Report exactly `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES` "
    "(never invented tokens); complete with metadata `{\"gate\": \"pass\"}` "
    "only when every worker is compliant.\n"
    "- Escalate to a human gate after 2 consecutive failures.\n"
)

VERIFIER_SKILLS = ["requesting-code-review", "worker-failure-discipline"]


@dataclass(frozen=True)
class SwarmRoutingConfig:
    """Resolved thresholds + board settings for the routing decision.

    Built from the ``swarm`` config sub-namespace via
    :meth:`from_mapping`; unknown / malformed values fall back to the
    documented defaults (fail-safe).
    """

    min_workers: int = DEFAULT_KANBAN_BACKED_MIN_WORKERS
    min_runtime_seconds: Optional[int] = DEFAULT_KANBAN_BACKED_MIN_RUNTIME_SECONDS
    board: str = DEFAULT_SWARM_BOARD
    board_autocreate: bool = DEFAULT_BOARD_AUTOCREATE
    verifier_assignee: str = DEFAULT_VERIFIER_ASSIGNEE
    synthesizer_assignee: str = DEFAULT_SYNTHESIZER_ASSIGNEE
    worker_profile: str = DEFAULT_WORKER_PROFILE

    @classmethod
    def from_mapping(
        cls, cfg: Optional[Mapping[str, Any]]
    ) -> "SwarmRoutingConfig":
        cfg = cfg or {}

        def _int(key: str, default: Optional[int]) -> Optional[int]:
            raw = cfg.get(key)
            if raw is None:
                return default
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        def _str(key: str, default: str) -> str:
            raw = cfg.get(key)
            if raw is None:
                return default
            text = str(raw).strip()
            return text or default

        min_workers = _int("kanban_backed_min_workers", DEFAULT_KANBAN_BACKED_MIN_WORKERS)
        # A non-positive threshold would route everything to kanban; clamp to
        # a sane floor so a fat-fingered ``0`` can't disable the v1 path.
        if min_workers is None or min_workers < 1:
            min_workers = DEFAULT_KANBAN_BACKED_MIN_WORKERS

        return cls(
            min_workers=min_workers,
            min_runtime_seconds=_int(
                "kanban_backed_min_runtime_seconds",
                DEFAULT_KANBAN_BACKED_MIN_RUNTIME_SECONDS,
            ),
            board=_str("kanban_board", DEFAULT_SWARM_BOARD),
            board_autocreate=bool(cfg.get("kanban_board_autocreate", DEFAULT_BOARD_AUTOCREATE)),
            verifier_assignee=_str("kanban_verifier", DEFAULT_VERIFIER_ASSIGNEE),
            synthesizer_assignee=_str("kanban_synthesizer", DEFAULT_SYNTHESIZER_ASSIGNEE),
            worker_profile=_str("kanban_worker_profile", DEFAULT_WORKER_PROFILE),
        )


def _as_config(config: Any) -> SwarmRoutingConfig:
    if isinstance(config, SwarmRoutingConfig):
        return config
    return SwarmRoutingConfig.from_mapping(config)


@dataclass(frozen=True)
class SwarmRouteDecision:
    """Pure routing verdict: v1 fan-out vs. kanban-backed swarm."""

    mode: str  # "v1" | "kanban"
    reason: str
    worker_count: int
    min_workers: int
    expected_runtime_seconds: Optional[int]
    min_runtime_seconds: Optional[int]


def decide_swarm_route(
    *,
    worker_count: int,
    config: Any = None,
    expected_runtime_seconds: Optional[int] = None,
) -> SwarmRouteDecision:
    """Decide whether a swarm of ``worker_count`` goes kanban-backed.

    Kanban-backed iff the batch has at least ``min_workers`` workers, OR an
    ``expected_runtime_seconds`` was supplied and meets/exceeds the (opt-in)
    runtime threshold. Otherwise v1. Pure; never raises.
    """
    cfg = _as_config(config)

    def _mk(mode: str, reason: str) -> SwarmRouteDecision:
        return SwarmRouteDecision(
            mode=mode,
            reason=reason,
            worker_count=worker_count,
            min_workers=cfg.min_workers,
            expected_runtime_seconds=expected_runtime_seconds,
            min_runtime_seconds=cfg.min_runtime_seconds,
        )

    if worker_count >= cfg.min_workers:
        return _mk(
            "kanban",
            f"worker_count {worker_count} >= kanban_backed_min_workers {cfg.min_workers}",
        )
    if (
        cfg.min_runtime_seconds is not None
        and expected_runtime_seconds is not None
        and expected_runtime_seconds >= cfg.min_runtime_seconds
    ):
        return _mk(
            "kanban",
            f"expected_runtime {expected_runtime_seconds}s >= "
            f"kanban_backed_min_runtime_seconds {cfg.min_runtime_seconds}s",
        )
    return _mk(
        "v1",
        f"worker_count {worker_count} < kanban_backed_min_workers {cfg.min_workers}"
        + (
            ""
            if cfg.min_runtime_seconds is None
            else f" and expected_runtime below {cfg.min_runtime_seconds}s"
        ),
    )


@dataclass(frozen=True)
class BoardResolution:
    """Outcome of resolving the configured board for a kanban dispatch."""

    board: str
    ok: bool  # True → dispatchable and safe to materialize on
    reason: str
    created: bool  # True → freshly created + opted-in by THIS call


def _kb():
    """Lazy import so tests that reset ``HERMES_HOME`` + purge ``hermes_cli``
    modules always resolve board paths against the *current* module."""
    from hermes_cli import kanban_db as kb  # local import on purpose

    return kb


def _board_exists(kb, slug: str) -> bool:
    """True if the board already exists on disk (board.json or its DB)."""
    try:
        return bool(
            kb.board_metadata_path(slug).exists()
            or kb.kanban_db_path(slug).exists()
        )
    except Exception:
        # If we cannot even tell, treat it as existing so we never
        # auto-create + opt-in a board we're unsure about (fail-safe).
        return True


def resolve_dispatch_board(config: Any = None, *, kb=None) -> BoardResolution:
    """Resolve the configured board, honouring the P-71 opt-in rules.

    Never raises. Rules:

    * Already dispatchable → ok.
    * Exists but not dispatchable → NOT ok (never auto-flip an existing
      board — P-71).
    * Missing + autocreate off → NOT ok (don't silently stand up infra).
    * Missing + autocreate on → create fresh from the config slug and opt it
      in (allowed: freshly created from config slug) → ok.
    """
    cfg = _as_config(config)
    kb = kb or _kb()
    slug = cfg.board
    try:
        if kb.board_is_dispatchable(slug):
            return BoardResolution(slug, True, "board is dispatchable", False)
        if _board_exists(kb, slug):
            return BoardResolution(
                slug,
                False,
                "board exists but is not dispatchable; not auto-flipping (P-71)",
                False,
            )
        if not cfg.board_autocreate:
            return BoardResolution(
                slug,
                False,
                "board does not exist and kanban_board_autocreate is off",
                False,
            )
        # Fresh board from the configured slug — allowed to opt in.
        kb.create_board(
            slug=slug,
            name=None,
            description="hermes-v2 H-41 swarm-work board (auto-created, dispatchable)",
        )
        kb.write_board_metadata(slug, dispatchable=True)
        if not kb.board_is_dispatchable(slug):  # paranoia: verify the opt-in stuck
            return BoardResolution(
                slug, False, "auto-created board did not become dispatchable", False
            )
        return BoardResolution(
            slug, True, "freshly created dispatchable board from config slug", True
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("swarm-routing: board resolution failed for %r: %s", slug, exc)
        return BoardResolution(slug, False, f"board resolution error: {exc}", False)


@dataclass(frozen=True)
class SwarmRouteResult:
    """What :func:`route_and_materialize_swarm` did.

    ``mode == "kanban"`` → the swarm was materialised and ``created`` holds
    the topology; the caller should NOT run the v1 fan-out. ``mode == "v1"``
    → the caller runs the v1 ``delegate_task`` fan-out (either below
    threshold, or a fail-safe fallback — ``reason`` says which).
    """

    mode: str  # "v1" | "kanban"
    reason: str
    board: Optional[str]
    created: Optional[SwarmCreated]
    board_created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "board": self.board,
            "created": self.created.as_dict() if self.created else None,
            "board_created": self.board_created,
        }


def worker_specs_from_delegate_tasks(
    tasks: Iterable[Mapping[str, Any]],
    *,
    default_profile: str = DEFAULT_WORKER_PROFILE,
    priority: int = 0,
) -> list[SwarmWorkerSpec]:
    """Map a ``delegate_task`` batch (``tasks=[{goal,context,role?,skills?}]``)
    to :class:`SwarmWorkerSpec`s. Tasks with an empty goal are dropped."""
    specs: list[SwarmWorkerSpec] = []
    for task in tasks:
        goal = str(task.get("goal") or "").strip()
        if not goal:
            continue
        title = goal.splitlines()[0][:80]
        body = goal
        context = str(task.get("context") or "").strip()
        if context:
            body = f"{body}\n\n{context}"
        role = str(task.get("role") or "").strip() or default_profile
        raw_skills = task.get("skills") or []
        skills = [str(s).strip() for s in raw_skills if str(s).strip()]
        specs.append(
            SwarmWorkerSpec(
                profile=role, title=title, body=body, skills=skills, priority=priority
            )
        )
    return specs


def route_and_materialize_swarm(
    *,
    goal: str,
    workers: Iterable[SwarmWorkerSpec],
    config: Any = None,
    expected_runtime_seconds: Optional[int] = None,
    verifier_assignee: Optional[str] = None,
    synthesizer_assignee: Optional[str] = None,
    tenant: Optional[str] = None,
    created_by: str = "kimi-mode-swarm",
    priority: int = 0,
    idempotency_key: Optional[str] = None,
    kb=None,
    connect: Optional[Callable[[str], Any]] = None,
) -> SwarmRouteResult:
    """Route a ``/swarm`` request and, when kanban-backed, materialise it.

    Returns a :class:`SwarmRouteResult`. Never raises: below-threshold, a
    missing / non-dispatchable board, or any materialization error all
    degrade to ``mode="v1"`` so the caller falls back to the v1 fan-out.

    ``kb`` / ``connect`` are injection seams for tests; production callers
    leave them ``None`` (real :mod:`hermes_cli.kanban_db`).
    """
    cfg = _as_config(config)
    worker_specs = list(workers)

    decision = decide_swarm_route(
        worker_count=len(worker_specs),
        config=cfg,
        expected_runtime_seconds=expected_runtime_seconds,
    )
    if decision.mode == "v1":
        return SwarmRouteResult("v1", decision.reason, None, None, False)

    # kanban-backed intended — resolve the target board (fail-safe).
    kb = kb or _kb()
    board_res = resolve_dispatch_board(cfg, kb=kb)
    if not board_res.ok:
        reason = f"kanban intended ({decision.reason}) but fell back to v1: {board_res.reason}"
        logger.info("swarm-routing: %s", reason)
        return SwarmRouteResult("v1", reason, board_res.board, None, board_res.created)

    # Materialise the durable swarm on the dispatchable board.
    _connect = connect or (lambda slug: kb.connect(board=slug))
    try:
        conn = _connect(board_res.board)
        try:
            created = create_swarm(
                conn,
                goal=goal,
                workers=worker_specs,
                verifier_assignee=verifier_assignee or cfg.verifier_assignee,
                synthesizer_assignee=synthesizer_assignee or cfg.synthesizer_assignee,
                tenant=tenant,
                created_by=created_by,
                priority=priority,
                idempotency_key=idempotency_key,
                worker_protocol=H43_WORKER_PROTOCOL,
                verifier_protocol=VERIFIER_DISCIPLINE_PROTOCOL,
                verifier_skills=VERIFIER_SKILLS,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        reason = f"kanban materialization failed, fell back to v1: {exc}"
        logger.warning("swarm-routing: %s", reason)
        return SwarmRouteResult("v1", reason, board_res.board, None, board_res.created)

    return SwarmRouteResult(
        "kanban", decision.reason, board_res.board, created, board_res.created
    )

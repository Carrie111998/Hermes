"""Task 12 — roadmap_devflow_intake.py migrated to the shared DDP emitter.

The intake script is loaded from its REAL deployed path (via
hermes_constants.get_default_hermes_root, which the conftest does NOT patch —
only events.paths.get_default_hermes_root is), while all of its WRITES route
through the DelegationEmitter whose canonical paths resolve into the patched
tmp root. The live `.roadmap_intake_emit_enabled` sentinel that sits next to the
deployed script would otherwise flip the dry-run default on, so the `intake`
fixture neutralizes it — the test asserts behavior, not deployment state.
"""
import importlib.util
import json
from pathlib import Path

import pytest

# The deployed intake script, relative to the Hermes root. Note that
# `profiles/` is tracked in the *outer* Hermes repo (`~/.hermes`), not in
# `agent-src` — so the anchor is `agent-src`'s parent, not the repo root.
_INTAKE_REL = Path("profiles") / "main" / "scripts" / "roadmap_devflow_intake.py"


def _find_hermes_root() -> Path | None:
    """Return the Hermes root (the checkout holding `profiles/`), or None.

    Searches upward from this file for the directory that actually contains
    the intake script. Deliberately *not* a fixed count of `.parent` hops:
    this file sits three levels deeper inside a git worktree
    (`agent-src/.claude/worktrees/<name>/tests/devflow_delegation/`) than in
    the main checkout (`agent-src/tests/devflow_delegation/`), so any fixed
    count that escapes the checkout is right in exactly one of the two
    layouts. The previous `parents[3]` was correct only from the main
    checkout and silently resolved to `agent-src/.claude/worktrees`
    otherwise.

    Git cannot anchor this either: `rev-parse --show-toplevel` returns the
    *worktree* path rather than `agent-src`, and `hermes_constants`'
    `get_default_hermes_root()` reads `HERMES_HOME`, which the suite
    redirects to a per-test tempdir (see `tests/conftest.py`) — the very
    reason this path is hand-derived. Searching for the artifact itself is
    correct at any nesting depth and needs neither a subprocess nor the
    environment.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / _INTAKE_REL).is_file():
            return candidate
    return None


_HERMES_ROOT = _find_hermes_root()
if _HERMES_ROOT is None:
    pytest.skip(
        f"Hermes root not found: no ancestor of {Path(__file__).resolve()} "
        f"contains {_INTAKE_REL.as_posix()}. The intake script is deployed in "
        "the outer ~/.hermes checkout, which is absent from this tree.",
        allow_module_level=True,
    )

INTAKE_SCRIPT = _HERMES_ROOT / _INTAKE_REL

FIXTURE_ROADMAP = """# Simplification roadmap (fixture)

## P1

| ID | Item | Priority | Reversibility | Traces to |
|----|------|----------|---------------|-----------|
| SR-901 | **Collapse duplicate probe helpers.** | **P1** | Revert the split. | probe_helper_a |
| SR-902 | Retired example. **DONE 2026-07-01:** shipped | P2 | n/a | n/a |
"""


def load_intake_module():
    # Load from the REAL deployed path, not get_default_hermes_root(): the
    # agent-src test harness points HERMES_HOME at a hermetic temp root, so the
    # script lives beside this test tree, not under the patched root. The root
    # is located by searching upward for the script itself (see
    # `_find_hermes_root`), which is layout-independent — a fixed hop count is
    # not, because a git worktree nests three levels deeper.
    path = INTAKE_SCRIPT
    spec = importlib.util.spec_from_file_location("roadmap_devflow_intake_migrated", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def intake(tmp_path):
    module = load_intake_module()
    # Neutralize the deployed emit sentinel so the dry-run default is
    # deterministic regardless of ~/.hermes/profiles/main/scripts/
    # .roadmap_intake_emit_enabled being present on this machine.
    module.EMIT_SENTINEL = tmp_path / ".no_emit_sentinel"
    return module


@pytest.fixture
def fixture_env(hermes_root, allowlist_file, tmp_path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(FIXTURE_ROADMAP, encoding="utf-8")
    # queue mode for arch-review so emission actually writes
    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"arch-review": {"mode": "queue"}}), encoding="utf-8")
    return roadmap


@pytest.fixture
def dry_run_policy_env(hermes_root, allowlist_file, tmp_path):
    # Identical to fixture_env but WITHOUT a policy.json override, so arch-review
    # inherits the shipped default mode="dry_run" — the stock-machine posture.
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(FIXTURE_ROADMAP, encoding="utf-8")
    return roadmap


def test_default_mode_still_dry_run(intake, fixture_env, hermes_root, capsys):
    rc = intake.main(["--roadmap", str(fixture_env),
                      "--mailbox", str(hermes_root / "mailbox" / "devflow"),
                      "--no-canvas"])
    assert rc == 0
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    assert not inbox.exists() or not list(inbox.glob("*.json"))


def test_emit_writes_v3_through_shared_emitter(intake, fixture_env, hermes_root, capsys):
    rc = intake.main(["--roadmap", str(fixture_env),
                      "--mailbox", str(hermes_root / "mailbox" / "devflow"),
                      "--emit", "--no-canvas"])
    assert rc == 0
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1, "SR-901 queued; SR-902 is DONE (closed)"
    env = json.loads(files[0].read_text(encoding="utf-8"))
    assert env["type"] == "DEVFLOW_WORK_REQUEST"
    assert env["schema_version"] == "3.0"
    assert env["idempotency_key"] == "roadmap:sr-901:v1"  # legacy key preserved
    # ledger row exists
    from devflow_delegation.emitter import DelegationEmitter

    em = DelegationEmitter()
    assert em.ledger.find_by_idempotency_key("roadmap:sr-901:v1") is not None


def test_emit_under_dry_run_policy_reports_shadow_not_emitted(
        intake, dry_run_policy_env, hermes_root, capsys):
    """Under the shipped dry_run default, --emit classifies but writes NOTHING.
    The script must not report those as EMITTED requests (that would make the
    sentinel-gated cron on a stock machine lie about queueing work)."""
    rc = intake.main(["--roadmap", str(dry_run_policy_env),
                      "--mailbox", str(hermes_root / "mailbox" / "devflow"),
                      "--emit", "--no-canvas"])
    assert rc == 0
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    assert not inbox.exists() or not list(inbox.glob("*.json")), \
        "dry_run policy mode must not write an envelope"
    out = capsys.readouterr().out
    assert "EMITTED 1" not in out, \
        "a dry_run shadow-classification must never be reported as EMITTED"
    assert "SHADOW-CLASSIFIED 1" in out, \
        "the shadow-classified count must be surfaced so the no-op is visible"


def test_emit_twice_dedups_via_ledger(intake, fixture_env, hermes_root, capsys):
    """Positive control for the emitter's durable dedup: delete the queued
    envelope between runs so the script's inbox pre-filter CANNOT be what
    dedups run #2 — only the shared emitter's ledger can. Under the
    pre-migration v2 code the ledger stays empty (total == 0), so this fails
    on revert."""
    argv = ["--roadmap", str(fixture_env),
            "--mailbox", str(hermes_root / "mailbox" / "devflow"),
            "--emit", "--no-canvas"]
    intake.main(argv)
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    first = list(inbox.glob("*.json"))
    assert len(first) == 1
    first[0].unlink()  # inbox pre-filter can no longer see run #1

    intake.main(argv)

    from devflow_delegation.emitter import DelegationEmitter

    em = DelegationEmitter()
    assert em.ledger.summary_counts()["total"] == 1, \
        "second emit must dedup through the ledger, not create a second row"
    assert list(inbox.glob("*.json")) == [], \
        "dedup short-circuits before write, so the envelope is not recreated"


def test_terminal_ledger_row_does_not_get_reblocked_by_legacy_processed_file(
        intake, fixture_env, hermes_root, capsys):
    """A terminal DDP record must reach the emitter's cooldown/reopen policy.

    Legacy v2 mailbox files are a migration fallback only. If their key is
    already known to the ledger, they must not turn a terminal row into an
    indefinite intake pre-filter block.
    """
    from devflow_delegation.emitter import DelegationEmitter

    mailbox = hermes_root / "mailbox" / "devflow"
    processed = mailbox / "processed"
    processed.mkdir(parents=True)
    processed.joinpath("legacy.json").write_text(json.dumps({
        "type": "DEVFLOW_FIX_REQUEST",
        "idempotency_key": "roadmap:sr-901:v1",
    }), encoding="utf-8")

    em = DelegationEmitter()
    request = em.delegate(
        source={"agent": "roadmap-intake", "kind": "arch-review", "finding_id": "SR-901"},
        kind="task",
        title="SR-901: Collapse duplicate probe helpers.",
        problem_statement="Collapse duplicate probe helpers.",
        evidence=[{"kind": "roadmap_row", "ref": "roadmap.md:L6", "summary": "fixture"}],
        acceptance_criteria=["Resolve SR-901."],
        target={"repo": "hermes", "subsystem": "roadmap"},
        severity="medium",
        priority="P1",
        confidence=0.8,
        idempotency_key="roadmap:sr-901:v1",
    )
    assert request.status == "queued"
    em.ledger.set_state(request.request_id, "DECLINED", terminal_reason="fixture")
    for queued in (mailbox / "inbox").glob("*.json"):
        queued.unlink()

    rc = intake.main([
        "--roadmap", str(fixture_env), "--mailbox", str(mailbox),
        "--emit", "--no-canvas",
    ])
    out = capsys.readouterr().out

    assert rc == 0
    assert "already-queued(dup): 0" in out
    assert "fresh: 1" in out
    assert "EMITTED 1 request(s)" in out
    assert len(list((mailbox / "inbox").glob("*.json"))) == 1
    assert em.ledger.summary_counts()["total"] == 2
    assert em.ledger.summary_counts()["by_state"].get("REQUESTED") == 1
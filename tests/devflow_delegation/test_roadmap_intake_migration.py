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

import pytest

FIXTURE_ROADMAP = """# Simplification roadmap (fixture)

## P1

| ID | Item | Priority | Reversibility | Traces to |
|----|------|----------|---------------|-----------|
| SR-901 | **Collapse duplicate probe helpers.** | **P1** | Revert the split. | probe_helper_a |
| SR-902 | Retired example. **DONE 2026-07-01:** shipped | P2 | n/a | n/a |
"""


def load_intake_module():
    from pathlib import Path

    # Load from the REAL deployed path, not get_default_hermes_root(): the
    # agent-src test harness points HERMES_HOME at a hermetic temp root, so the
    # script lives beside this test tree, not under the patched root. __file__ is
    # agent-src/tests/devflow_delegation/<this>; parents[3] == ~/.hermes.
    path = (
        Path(__file__).resolve().parents[3]
        / "profiles" / "main" / "scripts" / "roadmap_devflow_intake.py"
    )
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


def test_emit_twice_dedups(intake, fixture_env, hermes_root, capsys):
    argv = ["--roadmap", str(fixture_env),
            "--mailbox", str(hermes_root / "mailbox" / "devflow"),
            "--emit", "--no-canvas"]
    intake.main(argv)
    intake.main(argv)
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    assert len(list(inbox.glob("*.json"))) == 1

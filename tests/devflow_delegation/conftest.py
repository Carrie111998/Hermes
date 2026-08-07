import json

import pytest

SAMPLE_ALLOWLIST = {
    "version": "1",
    "targets": {
        "hermes": {
            "repo": "hermes",
            "checkout_path": "~/.hermes",
            "default_branch": "main",
            "allowed_globs": ["agent-src/**", "scripts/**", "profiles/**", "skills/**", "docs/**"],
            "denied_globs": ["profiles/main/cron/jobs.json", "**/.env", "**/secrets/**"],
            "live_gateway_imports": True,
            "notify_route": "devflow_firehose",
        }
    },
}


@pytest.fixture
def hermes_root(tmp_path, monkeypatch):
    """Redirect ALL canonical path resolution into tmp_path. devflow_delegation
    components must resolve paths lazily (at call time) for this to work."""
    monkeypatch.setattr("events.paths.get_default_hermes_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def allowlist_file(hermes_root):
    ddp = hermes_root / "devflow"
    ddp.mkdir(parents=True, exist_ok=True)
    p = ddp / "allowlist.json"
    p.write_text(json.dumps(SAMPLE_ALLOWLIST), encoding="utf-8")
    return p


@pytest.fixture
def emitter(hermes_root, allowlist_file):
    from devflow_delegation.emitter import DelegationEmitter

    return DelegationEmitter()


def make_delegate_kwargs(**over):
    kw = dict(
        source={"agent": "critic", "kind": "critic", "finding_id": "F-1"},
        kind="bug",
        title="Restore bounded gateway health query",
        problem_statement="The health query scans all sessions without a LIMIT.",
        evidence=[{"kind": "test_failure", "ref": "tests/test_health.py", "summary": "timeout at 30s"}],
        acceptance_criteria=["Health query returns within 3s on a 10k-row state.db"],
        target={"repo": "hermes", "subsystem": "gateway-health"},
        severity="high",
        priority="P1",
        confidence=0.94,
        proposed_approach="Add a bounded read with a 3s budget.",
        safety_notes=("Do not restart the live gateway",),
    )
    kw.update(over)
    return kw

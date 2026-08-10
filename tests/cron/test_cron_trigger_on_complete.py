"""Tests for cron job reactive chaining via trigger_on_complete (issue #15831).

A job with ``trigger_on_complete=True`` is fired when any job in its
``context_from`` completes, gated by ``trigger_status`` (ok/error/any).
Cycles across the context_from+trigger graph are rejected at create/update.
"""

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


# ---------------------------------------------------------------------------
# Field storage / normalization
# ---------------------------------------------------------------------------

class TestTriggerFieldsStorage:
    def test_defaults_off(self, cron_env):
        from cron.jobs import create_job, get_job

        job = create_job(prompt="Hello", schedule="every 1h")
        assert job["trigger_on_complete"] is False
        assert job["trigger_status"] == "ok"
        loaded = get_job(job["id"])
        assert loaded["trigger_on_complete"] is False
        assert loaded["trigger_status"] == "ok"

    def test_create_with_trigger_on_complete(self, cron_env):
        from cron.jobs import create_job, get_job

        parent = create_job(prompt="Collect", schedule="every 1h")
        child = create_job(
            prompt="Process",
            schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
        )
        assert child["trigger_on_complete"] is True
        assert child["trigger_status"] == "ok"
        assert get_job(child["id"])["trigger_on_complete"] is True

    def test_trigger_status_normalized_to_lowercase(self, cron_env):
        from cron.jobs import create_job

        parent = create_job(prompt="P", schedule="every 1h")
        child = create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="ANY",
        )
        assert child["trigger_status"] == "any"

    def test_trigger_status_invalid_rejected(self, cron_env):
        from cron.jobs import create_job

        parent = create_job(prompt="P", schedule="every 1h")
        with pytest.raises(ValueError, match="trigger_status"):
            create_job(
                prompt="C", schedule="every 1h",
                context_from=parent["id"],
                trigger_on_complete=True,
                trigger_status="bogus",
            )

    def test_trigger_on_complete_rejects_truthy_strings(self, cron_env):
        """String 'false' must not silently become boolean True."""
        from cron.jobs import create_job

        parent = create_job(prompt="P", schedule="every 1h")
        truthy_string: Any = "false"
        with pytest.raises(ValueError, match="must be a boolean"):
            create_job(
                prompt="C", schedule="every 1h",
                context_from=parent["id"],
                trigger_on_complete=truthy_string,
            )

    def test_trigger_on_complete_requires_context_from(self, cron_env):
        from cron.jobs import create_job

        with pytest.raises(ValueError, match="context_from"):
            create_job(
                prompt="C", schedule="every 1h",
                trigger_on_complete=True,
            )

    def test_legacy_empty_context_from_cannot_enable_trigger(self, cron_env):
        """A hand-edited legacy empty string is still treated as no parent."""
        from cron.jobs import create_job, load_jobs, save_jobs, update_job

        child = create_job(prompt="C", schedule="every 1h")
        jobs = load_jobs()
        jobs[0]["context_from"] = ""
        save_jobs(jobs)

        with pytest.raises(ValueError, match="context_from"):
            update_job(child["id"], {"trigger_on_complete": True})


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def test_self_cycle_rejected(self, cron_env):
        from cron.jobs import create_job, update_job

        # Two-node cycle A->B->A. Build B reactive on A, then try to make A
        # reactive on B — that closes the loop and must be rejected.
        job_a = create_job(prompt="A", schedule="every 1h", trigger_on_complete=False)
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        with pytest.raises(ValueError, match="cycle"):
            update_job(job_a["id"], {
                "context_from": job_b["id"],
                "trigger_on_complete": True,
            })

    def test_no_cycle_when_only_one_side_reactive(self, cron_env):
        from cron.jobs import create_job, update_job

        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        # job_a also reads from job_b but is NOT reactive — no cycle.
        updated = update_job(job_a["id"], {"context_from": job_b["id"]})
        assert updated["context_from"] == [job_b["id"]]
        assert updated["trigger_on_complete"] is False

    def test_update_clearing_trigger_allows_readding(self, cron_env):
        from cron.jobs import create_job, update_job

        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        # Turn off, then the reverse direction becomes safe.
        update_job(job_b["id"], {"trigger_on_complete": False})
        updated = update_job(job_a["id"], {
            "context_from": job_b["id"],
            "trigger_on_complete": True,
        })
        assert updated["trigger_on_complete"] is True

    def test_self_cycle_rejected_via_update(self, cron_env):
        from cron.jobs import create_job, update_job

        job_a = create_job(prompt="A", schedule="every 1h")
        # A reacting to its own completion is the trivial self-loop.
        with pytest.raises(ValueError, match="cycle"):
            update_job(job_a["id"], {
                "context_from": job_a["id"],
                "trigger_on_complete": True,
            })

    def test_three_node_chain_creation_succeeds(self, cron_env):
        from cron.jobs import create_job

        # A -> B -> C is a legitimate linear chain; no false cycle.
        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        job_c = create_job(
            prompt="C", schedule="every 1h",
            context_from=job_b["id"],
            trigger_on_complete=True,
        )
        assert job_c["trigger_on_complete"] is True
        assert job_c["context_from"] == [job_b["id"]]

    def test_three_node_cycle_rejected(self, cron_env):
        from cron.jobs import create_job, update_job

        # A -> B -> C reactive chain; closing C -> A must be rejected.
        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        job_c = create_job(
            prompt="C", schedule="every 1h",
            context_from=job_b["id"],
            trigger_on_complete=True,
        )
        with pytest.raises(ValueError, match="cycle"):
            update_job(job_a["id"], {
                "context_from": job_c["id"],
                "trigger_on_complete": True,
            })

    def test_transitive_chain_still_rejected(self, cron_env):
        from cron.jobs import create_job, update_job

        # A 4-node reactive chain; closing the gap at the far end (A reacts to
        # D) must be rejected even though A is not D's direct parent — the
        # existing path A->B->C->D plus the new D->A edge loops forever.
        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        job_c = create_job(
            prompt="C", schedule="every 1h",
            context_from=job_b["id"],
            trigger_on_complete=True,
        )
        job_d = create_job(
            prompt="D", schedule="every 1h",
            context_from=job_c["id"],
            trigger_on_complete=True,
        )
        with pytest.raises(ValueError, match="cycle"):
            update_job(job_a["id"], {
                "context_from": job_d["id"],
                "trigger_on_complete": True,
            })

    def test_context_from_only_update_cycle_rejected(self, cron_env):
        from cron.jobs import create_job, update_job

        # B already fires reactively off X, and B->C->A is a reactive path.
        # Repointing B's context_from at A — WITHOUT touching trigger fields —
        # closes A->B->C->A and must be rejected: context_from alone is half of
        # every reactive edge and can introduce a cycle on its own.
        job_x = create_job(prompt="X", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_x["id"],
            trigger_on_complete=True,
        )
        job_c = create_job(
            prompt="C", schedule="every 1h",
            context_from=job_b["id"],
            trigger_on_complete=True,
        )
        job_a = create_job(
            prompt="A", schedule="every 1h",
            context_from=job_c["id"],
            trigger_on_complete=True,
        )
        with pytest.raises(ValueError, match="cycle"):
            update_job(job_b["id"], {"context_from": job_a["id"]})

    def test_non_reactive_job_breaks_cycle(self, cron_env):
        from cron.jobs import create_job, update_job

        # B consumes A's output but never fires off it (trigger off), while
        # B fires C and C fires A. B's passive edge breaks the loop, so the
        # A->C edge is safe...
        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=False,
        )
        job_c = create_job(
            prompt="C", schedule="every 1h",
            context_from=job_b["id"],
            trigger_on_complete=True,
        )
        updated = update_job(job_a["id"], {
            "context_from": job_c["id"],
            "trigger_on_complete": True,
        })
        assert updated["trigger_on_complete"] is True

        # ...but flipping B reactive closes A->B->C->A and must be rejected.
        with pytest.raises(ValueError, match="cycle"):
            update_job(job_b["id"], {"trigger_on_complete": True})


# ---------------------------------------------------------------------------
# Dependency lookup + scheduler fan-out
# ---------------------------------------------------------------------------

class TestGetDependentJobs:
    def test_finds_reactive_children(self, cron_env):
        from cron.jobs import create_job, get_dependent_jobs

        parent = create_job(prompt="P", schedule="every 1h")
        child = create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
        )
        unrelated = create_job(prompt="U", schedule="every 1h")
        deps = get_dependent_jobs(parent["id"])
        ids = [d["id"] for d in deps]
        assert child["id"] in ids
        assert unrelated["id"] not in ids

    def test_excludes_non_reactive_context_from_jobs(self, cron_env):
        from cron.jobs import create_job, get_dependent_jobs

        parent = create_job(prompt="P", schedule="every 1h")
        # Passive consumer: context_from but no trigger_on_complete.
        create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=False,
        )
        assert get_dependent_jobs(parent["id"]) == []

    def test_excludes_disabled_children(self, cron_env):
        from cron.jobs import create_job, get_dependent_jobs, pause_job

        parent = create_job(prompt="P", schedule="every 1h")
        child = create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
        )
        pause_job(child["id"])
        assert get_dependent_jobs(parent["id"]) == []

    def test_atomic_trigger_does_not_resurrect_paused_child(self, cron_env):
        """A pause racing reactive discovery wins over the automatic fire."""
        from cron.jobs import (
            create_job,
            get_job,
            pause_job,
            trigger_job_if_runnable,
        )

        child = create_job(prompt="C", schedule="every 1h")
        pause_job(child["id"])

        assert trigger_job_if_runnable(child["id"]) is None
        loaded = get_job(child["id"])
        assert loaded is not None
        assert loaded["enabled"] is False
        assert loaded["state"] == "paused"


class TestFireDependentJobs:
    """The scheduler's _fire_dependent_jobs kicks children via trigger_job_if_runnable."""

    def test_fires_child_on_parent_success(self, cron_env, monkeypatch):
        from cron.jobs import create_job
        from cron import scheduler as sched_mod

        fired = []
        monkeypatch.setattr(
            sched_mod, "trigger_job_if_runnable",
            lambda jid: fired.append(jid),
        )

        parent = create_job(prompt="P", schedule="every 1h")
        child = create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="ok",
        )
        sched_mod._fire_dependent_jobs(parent["id"], parent_success=True)
        assert child["id"] in fired

    def test_notifies_external_provider_once_per_fanout(self, cron_env, monkeypatch):
        """Reactive mutations must be provisioned by non-built-in schedulers."""
        from cron.jobs import create_job
        from cron import scheduler as sched_mod

        parent = create_job(prompt="P", schedule="every 1h")
        children = [
            create_job(
                prompt=f"C{i}", schedule="every 1h",
                context_from=parent["id"],
                trigger_on_complete=True,
            )
            for i in range(2)
        ]
        fired = []
        notifications = []

        def fake_trigger(job_id):
            fired.append(job_id)
            return {"id": job_id}

        monkeypatch.setattr(sched_mod, "trigger_job_if_runnable", fake_trigger)
        monkeypatch.setattr(
            sched_mod, "_notify_provider_jobs_changed",
            lambda: notifications.append("notified"),
        )

        sched_mod._fire_dependent_jobs(parent["id"], parent_success=True)

        assert set(fired) == {child["id"] for child in children}
        assert notifications == ["notified"]

    def test_does_not_notify_provider_when_no_child_is_scheduled(
        self, cron_env, monkeypatch
    ):
        """A status-gated no-op must not churn external scheduler state."""
        from cron.jobs import create_job
        from cron import scheduler as sched_mod

        parent = create_job(prompt="P", schedule="every 1h")
        create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="error",
        )
        notifications = []
        monkeypatch.setattr(
            sched_mod, "_notify_provider_jobs_changed",
            lambda: notifications.append("notified"),
        )

        sched_mod._fire_dependent_jobs(parent["id"], parent_success=True)

        assert notifications == []

    def test_invalid_persisted_status_fails_closed(self, monkeypatch):
        """Tampered/legacy invalid status must not behave like 'any'."""
        from cron import scheduler as sched_mod

        fired = []
        monkeypatch.setattr(
            sched_mod,
            "get_dependent_jobs",
            lambda _parent: [{"id": "child", "trigger_status": "bogus"}],
        )
        monkeypatch.setattr(sched_mod, "trigger_job_if_runnable", lambda jid: fired.append(jid))

        sched_mod._fire_dependent_jobs("parent", parent_success=True)

        assert fired == []

    def test_skips_child_when_status_gate_mismatches(self, cron_env, monkeypatch):
        from cron.jobs import create_job
        from cron import scheduler as sched_mod

        fired = []
        monkeypatch.setattr(sched_mod, "trigger_job_if_runnable", lambda jid: fired.append(jid))

        parent = create_job(prompt="P", schedule="every 1h")
        child_ok = create_job(
            prompt="Cok", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="ok",
        )
        child_err = create_job(
            prompt="Cerr", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="error",
        )
        child_any = create_job(
            prompt="Cany", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="any",
        )

        # Parent failed: only error+any children fire.
        sched_mod._fire_dependent_jobs(parent["id"], parent_success=False)
        assert child_err["id"] in fired
        assert child_any["id"] in fired
        assert child_ok["id"] not in fired

    def test_failure_to_trigger_one_child_does_not_block_siblings(
        self, cron_env, monkeypatch
    ):
        from cron.jobs import create_job
        from cron import scheduler as sched_mod

        calls = []

        parent = create_job(prompt="P", schedule="every 1h")
        child1 = create_job(
            prompt="C1", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
        )
        child2 = create_job(
            prompt="C2", schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
        )

        def selective(jid):
            calls.append(jid)
            if jid == child1["id"]:
                raise RuntimeError("boom")
            return {"id": jid}

        monkeypatch.setattr(sched_mod, "trigger_job_if_runnable", selective)
        monkeypatch.setattr(sched_mod, "_notify_provider_jobs_changed", lambda: None)
        sched_mod._fire_dependent_jobs(parent["id"], parent_success=True)

        # child2 still gets scheduled even though child1 failed.
        assert child1["id"] in calls
        assert child2["id"] in calls


# ---------------------------------------------------------------------------
# Tool surface (cronjob action=create/update)
# ---------------------------------------------------------------------------

class TestCronjobToolTrigger:
    def test_create_via_tool(self, cron_env):
        from cron.jobs import create_job, get_job
        from tools.cronjob_tools import cronjob
        import json

        parent = create_job(prompt="P", schedule="every 1h")
        result = json.loads(cronjob(
            action="create",
            prompt="C",
            schedule="every 1h",
            context_from=parent["id"],
            trigger_on_complete=True,
            trigger_status="any",
        ))
        assert result["success"] is True
        loaded = get_job(result["job_id"])
        assert loaded["trigger_on_complete"] is True
        assert loaded["trigger_status"] == "any"

    def test_update_via_tool(self, cron_env):
        from cron.jobs import create_job, get_job
        from tools.cronjob_tools import cronjob
        import json

        parent = create_job(prompt="P", schedule="every 1h")
        child = create_job(
            prompt="C", schedule="every 1h",
            context_from=parent["id"],
        )
        assert child["trigger_on_complete"] is False

        result = json.loads(cronjob(
            action="update",
            job_id=child["id"],
            trigger_on_complete=True,
            trigger_status="error",
        ))
        assert result["success"] is True
        loaded = get_job(child["id"])
        assert loaded["trigger_on_complete"] is True
        assert loaded["trigger_status"] == "error"

    def test_update_rejects_cycle_via_tool(self, cron_env):
        from cron.jobs import create_job
        from tools.cronjob_tools import cronjob
        import json

        job_a = create_job(prompt="A", schedule="every 1h")
        job_b = create_job(
            prompt="B", schedule="every 1h",
            context_from=job_a["id"],
            trigger_on_complete=True,
        )
        result = json.loads(cronjob(
            action="update",
            job_id=job_a["id"],
            context_from=job_b["id"],
            trigger_on_complete=True,
        ))
        assert result["success"] is False
        assert "cycle" in result.get("error", "").lower()

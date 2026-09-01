"""Per-job max_turns override: store contract + scheduler precedence.

A cron job may carry its own agent turn budget, independent of the global
``agent.max_turns``. Long multi-phase jobs (browse → plan → generate → QA)
legitimately need more turns than the shared default, and raising the global
value to suit them would spend the same budget on every other job.

Contract under test:

- Job store (cron/jobs.py): validated at the storage choke point — a positive
  int is stored, 0/empty clears, garbage raises and nothing persists. An
  absent field keeps the record byte-identical to pre-feature behavior.
- Scheduler resolution (cron/scheduler.py::_resolve_job_turn_limit): the
  job's value wins over the config-resolved limit; an absent field yields
  the config limit unchanged; a garbage value in a hand-edited store warns
  and follows config instead of killing the tick.
"""

import pytest

from cron.jobs import create_job, load_jobs, update_job


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate the cron store (same pattern as tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path / "cron"


def _create(**kw):
    kw.setdefault("prompt", "say hi")
    kw.setdefault("schedule", "every 1h")
    return create_job(**kw)


class TestJobStoreMaxTurns:
    def test_absent_field_keeps_the_record_shape(self, tmp_cron_dir):
        job = _create()
        assert "max_turns" not in job
        assert "max_turns" not in load_jobs()[0]

    @pytest.mark.parametrize("value, expected", [(160, 160), ("160", 160), (1, 1)])
    def test_positive_values_stored_as_int(self, tmp_cron_dir, value, expected):
        job = _create(max_turns=value)
        assert job["max_turns"] == expected
        assert load_jobs()[0]["max_turns"] == expected

    @pytest.mark.parametrize("empty", [None, "", "  ", 0, "0"])
    def test_empty_or_zero_means_follow_config(self, tmp_cron_dir, empty):
        job = _create(max_turns=empty)
        assert "max_turns" not in job

    @pytest.mark.parametrize("garbage", ["lots", "12.5", -1, "-3", [160]])
    def test_garbage_rejected_nothing_persisted(self, tmp_cron_dir, garbage):
        with pytest.raises(ValueError, match="max_turns"):
            _create(max_turns=garbage)
        assert load_jobs() == []

    def test_update_sets_field(self, tmp_cron_dir):
        job = _create()
        updated = update_job(job["id"], {"max_turns": "200"})
        assert updated["max_turns"] == 200
        assert load_jobs()[0]["max_turns"] == 200

    def test_update_zero_clears(self, tmp_cron_dir):
        job = _create(max_turns=160)
        updated = update_job(job["id"], {"max_turns": 0})
        assert updated["max_turns"] is None
        assert load_jobs()[0]["max_turns"] is None

    def test_update_garbage_rejected_stored_value_untouched(self, tmp_cron_dir):
        job = _create(max_turns=160)
        with pytest.raises(ValueError, match="max_turns"):
            update_job(job["id"], {"max_turns": "many"})
        assert load_jobs()[0]["max_turns"] == 160


class TestSchedulerTurnLimit:
    def _resolve(self, job, config_limit=60):
        from cron.scheduler import _resolve_job_turn_limit

        return _resolve_job_turn_limit(job, config_limit)

    def test_job_value_wins_over_config(self):
        assert self._resolve({"name": "plan", "max_turns": 160}) == 160

    def test_numeric_string_from_a_hand_edited_store_is_honored(self):
        assert self._resolve({"name": "plan", "max_turns": "160"}) == 160

    @pytest.mark.parametrize("absent", [{}, {"max_turns": None}, {"max_turns": ""}])
    def test_absent_field_follows_config(self, absent):
        assert self._resolve({"name": "plan", **absent}, config_limit=60) == 60

    @pytest.mark.parametrize("garbage", ["many", 0, -5, "-5", 12.0 * 0])
    def test_garbage_warns_and_follows_config(self, garbage, caplog):
        with caplog.at_level("WARNING", logger="cron.scheduler"):
            assert self._resolve({"name": "plan", "max_turns": garbage}, config_limit=60) == 60
        assert "ignoring invalid max_turns" in caplog.text

    def test_the_override_is_logged_so_operators_can_verify_it_applied(self, caplog):
        with caplog.at_level("INFO", logger="cron.scheduler"):
            self._resolve({"name": "plan", "max_turns": 160})
        assert "per-job max_turns override -> 160" in caplog.text

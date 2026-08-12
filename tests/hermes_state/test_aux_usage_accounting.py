"""Tests for auxiliary usage accounting (issue #23270).

Auxiliary LLM calls (vision, compression, title_generation, ...) record
their token usage into session_model_usage with a ``task`` dimension via
the ambient accounting context (agent/aux_accounting.py), making aux model
spend visible in analytics.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _mk_response(model="aux-model", prompt=100, completion=20):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        ),
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
    )


def _usage_rows(db, session_id):
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM session_model_usage WHERE session_id = ? ORDER BY task",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


class TestRecordAuxiliaryUsage:
    def test_records_task_row(self, db):
        db.create_session("s1", source="cli")
        db.record_auxiliary_usage(
            "s1", "vision", model="gemini-3-flash",
            billing_provider="gemini", input_tokens=500, output_tokens=50,
        )
        rows = _usage_rows(db, "s1")
        assert len(rows) == 1
        r = rows[0]
        assert r["task"] == "vision"
        assert r["model"] == "gemini-3-flash"
        assert r["billing_provider"] == "gemini"
        assert r["input_tokens"] == 500
        assert r["output_tokens"] == 50
        assert r["api_call_count"] == 1

    def test_accumulates_same_task_and_model(self, db):
        db.create_session("s1", source="cli")
        for _ in range(3):
            db.record_auxiliary_usage(
                "s1", "compression", model="glm-5", input_tokens=1000, output_tokens=100,
            )
        rows = _usage_rows(db, "s1")
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["api_call_count"] == 3



    def test_main_loop_and_aux_rows_coexist(self, db):
        db.create_session("s1", source="cli")
        db.update_token_counts(
            "s1", input_tokens=100, output_tokens=10,
            model="main-model", billing_provider="nous", api_call_count=1,
        )
        db.record_auxiliary_usage(
            "s1", "title_generation", model="main-model",
            billing_provider="nous", input_tokens=40, output_tokens=8,
        )
        rows = _usage_rows(db, "s1")
        tasks = sorted(r["task"] for r in rows)
        assert tasks == ["", "title_generation"]




class TestSchemaMigrationV22:
    def test_v21_db_migrates_with_existing_rows(self, tmp_path):
        """A legacy DB with pre-task rows migrates: rows preserved, task=''."""
        import sqlite3 as _sq
        db = SessionDB(tmp_path / "state.db")
        db.create_session("legacy", source="cli")
        db.update_token_counts(
            "legacy", input_tokens=42, model="old-model",
            billing_provider="openrouter", api_call_count=1,
        )
        db.close()

        # Rebuild the legacy (v21) table shape: task column absent.
        conn = _sq.connect(tmp_path / "state.db")
        conn.executescript("""
            CREATE TABLE smu_old AS SELECT session_id, model, billing_provider,
                billing_base_url, billing_mode, api_call_count, input_tokens,
                output_tokens, cache_read_tokens, cache_write_tokens,
                reasoning_tokens, estimated_cost_usd, actual_cost_usd,
                cost_status, cost_source, first_seen, last_seen
                FROM session_model_usage;
            DROP TABLE session_model_usage;
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                billing_provider TEXT NOT NULL DEFAULT '',
                billing_base_url TEXT NOT NULL DEFAULT '',
                billing_mode TEXT NOT NULL DEFAULT '',
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                actual_cost_usd REAL NOT NULL DEFAULT 0,
                cost_status TEXT,
                cost_source TEXT,
                first_seen REAL,
                last_seen REAL,
                PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode)
            );
            INSERT INTO session_model_usage SELECT * FROM smu_old;
            DROP TABLE smu_old;
            UPDATE schema_version SET version = 21;
        """)
        conn.commit()
        conn.close()

        # Reopen: v22 migration rebuilds the table with task in the PK.
        db2 = SessionDB(tmp_path / "state.db")
        with db2._lock:
            pk_cols = [
                r[1] for r in db2._conn.execute(
                    "SELECT * FROM pragma_table_info('session_model_usage') WHERE pk > 0"
                ).fetchall()
            ]
            row = db2._conn.execute(
                "SELECT task, input_tokens FROM session_model_usage WHERE session_id = 'legacy'"
            ).fetchone()
        assert "task" in pk_cols
        assert row is not None
        assert row[0] == ""  # legacy rows are main-loop accounting
        assert row[1] == 42
        # And the new dimension works post-migration.
        db2.record_auxiliary_usage("legacy", "vision", model="v", input_tokens=1)
        db2.close()


class TestAmbientAccountingContext:
    def test_record_aux_usage_writes_through_context(self, db):
        from agent.aux_accounting import (
            record_aux_usage,
            reset_accounting_context,
            set_accounting_context,
        )

        db.create_session("s1", source="cli")
        token = set_accounting_context(db, "s1")
        try:
            record_aux_usage(_mk_response(model="aux-m"), "vision", provider="gemini")
        finally:
            reset_accounting_context(token)
        rows = _usage_rows(db, "s1")
        assert len(rows) == 1
        assert rows[0]["task"] == "vision"
        assert rows[0]["model"] == "aux-m"
        assert rows[0]["input_tokens"] == 100
        assert rows[0]["output_tokens"] == 20


    def test_moa_tasks_excluded(self, db):
        """MoA advisor usage is already folded into the main-loop delta by
        conversation_loop — recording it here would double-count."""
        from agent.aux_accounting import (
            record_aux_usage,
            reset_accounting_context,
            set_accounting_context,
        )

        db.create_session("s1", source="cli")
        token = set_accounting_context(db, "s1")
        try:
            record_aux_usage(_mk_response(), "moa_reference")
            record_aux_usage(_mk_response(), "moa_aggregator")
        finally:
            reset_accounting_context(token)
        assert _usage_rows(db, "s1") == []



    def test_validate_llm_response_records(self, db):
        """The aux client's validation chokepoint feeds the recorder."""
        from agent.aux_accounting import (
            reset_accounting_context,
            set_accounting_context,
        )
        from agent.auxiliary_client import _validate_llm_response

        db.create_session("s1", source="cli")
        token = set_accounting_context(db, "s1")
        try:
            out = _validate_llm_response(_mk_response(), "web_extract", provider="openrouter")
        finally:
            reset_accounting_context(token)
        assert out is not None
        rows = _usage_rows(db, "s1")
        assert len(rows) == 1
        assert rows[0]["task"] == "web_extract"
        assert rows[0]["billing_provider"] == "openrouter"



class TestAnalyticsAuxRows:
    def test_aux_usage_rows_and_merge(self, db):
        from hermes_cli.web_server import (
            _aux_task_summary,
            _aux_usage_rows,
            _merge_aux_into_by_model,
        )

        db.create_session("s1", source="cli")
        db.update_token_counts(
            "s1", input_tokens=1000, output_tokens=100,
            model="main-model", billing_provider="nous", api_call_count=1,
        )
        db.record_auxiliary_usage(
            "s1", "vision", model="vision-model",
            billing_provider="gemini", input_tokens=300, output_tokens=30,
        )
        db.record_auxiliary_usage(
            "s1", "compression", model="main-model",
            billing_provider="nous", input_tokens=200, output_tokens=20,
        )

        aux = _aux_usage_rows(db, cutoff=0)
        assert {r["task"] for r in aux} == {"vision", "compression"}

        by_model = [{
            "model": "main-model", "input_tokens": 1000, "output_tokens": 100,
            "estimated_cost": 0, "sessions": 1, "api_calls": 1,
        }]
        merged = _merge_aux_into_by_model(by_model, aux)
        by_name = {r["model"]: r for r in merged}
        # vision-only model surfaces as its own entry
        assert "vision-model" in by_name
        assert by_name["vision-model"]["input_tokens"] == 300
        # compression folded into the main model's totals
        assert by_name["main-model"]["input_tokens"] == 1200
        assert by_name["main-model"]["api_calls"] == 2

        tasks = _aux_task_summary(aux)
        assert {t["task"] for t in tasks} == {"vision", "compression"}


class TestInsightsAuxTotals:
    def test_overview_totals_include_aux_usage(self, db):
        """`hermes insights` overview must count aux tokens, not just the
        sessions counters (issues #58592, #9979)."""
        from agent.insights import InsightsEngine

        db.create_session("s1", source="cli")
        db.update_token_counts(
            "s1", input_tokens=1000, output_tokens=100,
            model="main-model", billing_provider="nous", api_call_count=1,
        )
        db.record_auxiliary_usage(
            "s1", "compression", model="glm-5",
            billing_provider="openrouter", input_tokens=5000, output_tokens=500,
        )
        report = InsightsEngine(db).generate(days=30)
        ov = report["overview"]
        assert ov["total_input_tokens"] == 6000
        assert ov["total_output_tokens"] == 600
        models = {m["model"] for m in report["models"]}
        assert {"main-model", "glm-5"} <= models

    def test_aux_usage_reconciles_overview_models_daily_and_cost_buckets(self, db):
        """Every Usage view derives from one main-plus-aux accounting population."""
        from agent.insights import InsightsEngine

        db.create_session("s1", source="cli")
        db.update_token_counts(
            "s1",
            model="main-model",
            billing_provider="custom",
            input_tokens=100,
            output_tokens=10,
            estimated_cost_usd=1.25,
            actual_cost_usd=0.0,
            cost_status="estimated",
            cost_source="provider",
            api_call_count=1,
        )
        db.record_auxiliary_usage(
            "s1",
            "compression",
            model="aux-model",
            billing_provider="custom",
            input_tokens=50,
            output_tokens=5,
            estimated_cost_usd=2.5,
        )
        db.flush_token_counts()
        with db._lock:
            db._conn.execute(
                "UPDATE session_model_usage SET cost_status='estimated', "
                "cost_source='provider' WHERE session_id='s1' AND task='compression'"
            )
            db._conn.commit()

        report = InsightsEngine(db).generate(days=30)
        overview = report["overview"]
        models = report["models"]
        daily = report["daily_series"]
        buckets = overview["cost_buckets"]

        assert overview["total_input_tokens"] == sum(m["input_tokens"] for m in models) == 150
        assert overview["total_output_tokens"] == sum(m["output_tokens"] for m in models) == 15
        assert overview["total_input_tokens"] == sum(day["input_tokens"] for day in daily)
        assert overview["total_output_tokens"] == sum(day["output_tokens"] for day in daily)
        assert overview["estimated_cost"] == pytest.approx(sum(m["cost"] for m in models))
        assert overview["estimated_cost"] == pytest.approx(
            sum(day["estimated_cost_usd"] for day in daily)
        )
        assert overview["estimated_cost"] == pytest.approx(
            sum(bucket["cost_usd"] for bucket in buckets.values())
        )
        assert overview["actual_cost"] == pytest.approx(sum(m["actual_cost"] for m in models))
        assert sum(bucket["sessions"] for bucket in buckets.values()) == overview["total_sessions"] == 1
        assert sum(m["api_calls"] for m in models) == 2

    def test_mixed_included_and_estimated_session_keeps_complete_market_value(self, db):
        """A priced aux row must not hide the included main route's list value."""
        from agent.insights import InsightsEngine

        db.create_session("mixed", source="cli", model="gpt-5.6-sol")
        db.update_token_counts(
            "mixed",
            model="gpt-5.6-sol",
            billing_provider="openai-codex",
            billing_mode="subscription_included",
            input_tokens=1_000_000,
            output_tokens=500_000,
            estimated_cost_usd=0.0,
            cost_status="included",
            api_call_count=1,
        )
        db.record_auxiliary_usage(
            "mixed",
            "compression",
            model="aux-model",
            billing_provider="custom",
            input_tokens=50,
            output_tokens=5,
            estimated_cost_usd=2.5,
        )
        db.flush_token_counts()
        with db._lock:
            db._conn.execute(
                "UPDATE session_model_usage SET cost_status='estimated', "
                "cost_source='provider' WHERE session_id='mixed' AND task='compression'"
            )
            db._conn.commit()

        overview = InsightsEngine(db).generate(days=30)["overview"]
        estimated = overview["cost_buckets"]["estimated"]

        assert estimated["sessions"] == 1
        assert estimated["cost_usd"] == pytest.approx(2.5)
        assert estimated["at_market_cost_usd"] == pytest.approx(22.5)

    def test_mixed_session_market_value_is_unknown_when_included_sku_has_no_public_price(self, db):
        """Known aux spend cannot make an unresolved included comparison partial."""
        from agent.insights import InsightsEngine

        db.create_session("mixed-unknown", source="cli", model="codex-plan-only-model")
        db.update_token_counts(
            "mixed-unknown",
            model="codex-plan-only-model",
            billing_provider="openai-codex",
            billing_mode="subscription_included",
            input_tokens=1_000,
            output_tokens=500,
            estimated_cost_usd=0.0,
            cost_status="included",
            api_call_count=1,
        )
        db.record_auxiliary_usage(
            "mixed-unknown",
            "compression",
            model="aux-model",
            billing_provider="custom",
            input_tokens=50,
            output_tokens=5,
            estimated_cost_usd=2.5,
        )
        db.flush_token_counts()
        with db._lock:
            db._conn.execute(
                "UPDATE session_model_usage SET cost_status='estimated', "
                "cost_source='provider' WHERE session_id='mixed-unknown' AND task='compression'"
            )
            db._conn.commit()

        estimated = InsightsEngine(db).generate(days=30)["overview"]["cost_buckets"]["estimated"]

        assert estimated["sessions"] == 1
        assert estimated["cost_usd"] == pytest.approx(2.5)
        assert estimated["at_market_cost_usd"] is None

    def test_aux_usage_does_not_inherit_main_loop_billing_route(self, db):
        """An underspecified auxiliary row remains unknown, not plan-included."""
        from agent.insights import InsightsEngine

        db.create_session("s1", source="cli", model="gpt-5.5")
        db.update_token_counts(
            "s1",
            model="gpt-5.5",
            billing_provider="openai-codex",
            billing_mode="subscription_included",
            input_tokens=100,
            output_tokens=10,
            api_call_count=1,
        )
        db.record_auxiliary_usage(
            "s1",
            "compression",
            model="aux-model",
            input_tokens=50,
            output_tokens=5,
        )
        db.flush_token_counts()

        report = InsightsEngine(db).generate(days=30)
        models = {model["model"]: model for model in report["models"]}

        assert models["gpt-5.5"]["cost_status"] == "included"
        assert models["aux-model"]["cost_status"] == "unknown"
        assert report["overview"]["cost_buckets"]["unknown"]["sessions"] == 1

    def test_actual_cost_availability_is_not_borrowed_by_auxiliary_models(self, db):
        """A provider actual on the main route does not authorize an aux zero."""
        from agent.insights import InsightsEngine

        db.create_session("s1", source="cli")
        db.update_token_counts(
            "s1",
            model="main-model",
            billing_provider="custom",
            input_tokens=100,
            output_tokens=10,
            actual_cost_usd=1.25,
            api_call_count=1,
        )
        db.record_auxiliary_usage(
            "s1",
            "compression",
            model="aux-model",
            billing_provider="custom",
            input_tokens=50,
            output_tokens=5,
        )
        db.flush_token_counts()

        report = InsightsEngine(db).generate(days=30)
        models = {model["model"]: model for model in report["models"]}

        assert models["main-model"]["actual_cost_available"] is True
        assert models["aux-model"]["actual_cost_available"] is False


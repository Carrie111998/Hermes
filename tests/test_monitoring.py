"""Tests for Monitoring & Observability (v0.27.0).

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg():
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"mon_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    conn.commit()
    from hermes_cli.postgres_authority import init_schema
    init_schema(conn)
    yield conn
    conn.close()
    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestMetrics:
    def test_record_and_query(self, pg):
        from hermes_cli.postgres_authority import (
            record_metric, query_metrics, DEFAULT_TENANT_ID,
        )

        record_metric(
            pg, tenant_id=DEFAULT_TENANT_ID,
            metric_name="task.claims.total", metric_type="counter",
            value=1.0, labels={"org": "acme"},
        )
        record_metric(
            pg, tenant_id=DEFAULT_TENANT_ID,
            metric_name="task.claims.total", metric_type="counter",
            value=1.0, labels={"org": "acme"},
        )

        results = query_metrics(
            pg, tenant_id=DEFAULT_TENANT_ID,
            metric_name="task.claims.total", since_seconds=60,
        )
        assert len(results) == 2

    def test_tenant_isolation(self, pg):
        from hermes_cli.postgres_authority import record_metric, query_metrics

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        record_metric(
            pg, tenant_id=tenant_a,
            metric_name="test.metric", metric_type="gauge", value=42.0,
        )

        results_b = query_metrics(
            pg, tenant_id=tenant_b,
            metric_name="test.metric", since_seconds=60,
        )
        assert len(results_b) == 0


class TestHealthChecks:
    def test_record_and_get(self, pg):
        from hermes_cli.postgres_authority import (
            record_health_check, get_latest_health,
        )

        record_health_check(
            pg, service_name="authority-store",
            status="healthy",
            details={"latency_ms": 5, "connections": 3},
        )

        health = get_latest_health(pg, service_name="authority-store")
        assert len(health) == 1
        assert health[0]["status"] == "healthy"

    def test_latest_only(self, pg):
        from hermes_cli.postgres_authority import (
            record_health_check, get_latest_health,
        )

        record_health_check(pg, service_name="api", status="healthy")
        record_health_check(pg, service_name="api", status="degraded")

        health = get_latest_health(pg, service_name="api")
        assert len(health) == 1
        assert health[0]["status"] == "degraded"

    def test_all_services(self, pg):
        from hermes_cli.postgres_authority import (
            record_health_check, get_latest_health,
        )

        record_health_check(pg, service_name="api", status="healthy")
        record_health_check(pg, service_name="worker", status="healthy")
        record_health_check(pg, service_name="postgres", status="healthy")

        health = get_latest_health(pg)
        assert len(health) == 3


class TestAlertRules:
    def test_create_and_list(self, pg):
        from hermes_cli.postgres_authority import (
            create_alert_rule, list_alert_rules, DEFAULT_TENANT_ID,
        )

        rule = create_alert_rule(
            pg, tenant_id=DEFAULT_TENANT_ID,
            name="high-claim-rate", metric_name="task.claims.total",
            condition="rate_per_minute > threshold",
            threshold=100.0, window_seconds=60,
            notify_channel="slack:#ops",
        )
        assert rule["name"] == "high-claim-rate"

        rules = list_alert_rules(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(rules) == 1

    def test_disable_rule(self, pg):
        from hermes_cli.postgres_authority import (
            create_alert_rule, disable_alert_rule, list_alert_rules,
            DEFAULT_TENANT_ID,
        )

        create_alert_rule(
            pg, tenant_id=DEFAULT_TENANT_ID,
            name="disk-full", metric_name="disk.usage",
            condition="value > threshold", threshold=90.0,
        )
        disable_alert_rule(pg, tenant_id=DEFAULT_TENANT_ID, name="disk-full")

        rules = list_alert_rules(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(rules) == 0

    def test_upsert_rule(self, pg):
        from hermes_cli.postgres_authority import (
            create_alert_rule, list_alert_rules, DEFAULT_TENANT_ID,
        )

        create_alert_rule(
            pg, tenant_id=DEFAULT_TENANT_ID,
            name="latency", metric_name="request.latency",
            condition="p99 > threshold", threshold=500.0,
        )
        create_alert_rule(
            pg, tenant_id=DEFAULT_TENANT_ID,
            name="latency", metric_name="request.latency",
            condition="p99 > threshold", threshold=1000.0,
        )
        rules = list_alert_rules(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(rules) == 1
        assert rules[0]["threshold"] == 1000.0

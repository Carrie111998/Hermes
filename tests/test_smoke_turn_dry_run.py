from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli.cost import ledger, turns_schema
from hermes_cli.programme import init as programme_init
from hermes_cli.programme import ingress
from hermes_cli.routing import bootstrap, schema as routing_schema
from hermes_cli.smoke.mocks import MockLLMCall
from hermes_cli.smoke.roundtrip import command, run_smoke_turn
from hermes_cli.verdict import schema as verdict_schema


@pytest.fixture(scope="module")
def smoke_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cs19-smoke-template") / "kanban.db"
    kanban_db.init_db(path)
    programme_init.migrate(path)
    ingress.migrate(path)
    bootstrap.bootstrap_if_needed(path)
    routing_schema.ensure_migrated(path)
    verdict_schema.ensure_migrated(path)
    ledger.ensure_migrated(path)
    turns_schema.ensure_migrated(path)
    return path


@pytest.fixture
def smoke_db(smoke_template: Path, tmp_path: Path) -> Path:
    path = tmp_path / "kanban.db"
    shutil.copy2(smoke_template, path)
    return path


def _args(db_path: Path, *, dry_run: bool, commit: bool):
    return argparse.Namespace(
        scenario="success",
        lane="default",
        commit=commit,
        dry_run=dry_run,
        json=True,
        cleanup=False,
        force=False,
        db_path=str(db_path),
    )


def test_dry_run_suppresses_production_database_writes(smoke_db):
    before = smoke_db.read_bytes()
    assert command(_args(smoke_db, dry_run=True, commit=False)) == 0
    assert smoke_db.read_bytes() == before


def test_dry_run_reports_doctrine_routing(smoke_db):
    result = run_smoke_turn(
        scenario="success",
        lane="default",
        db_path=smoke_db,
        commit=False,
    )
    try:
        stage = next(
            item for item in result.stages if item.name == "route_for_turn"
        )
        assert stage.details["used_doctrine_reader"] is True
    finally:
        Path(result.working_db_path).unlink(missing_ok=True)


def test_dry_run_uses_no_real_llm(smoke_db, monkeypatch):
    called = []

    class ObservedMock(MockLLMCall):
        def __call__(self, *args, **kwargs):
            called.append(True)
            return super().__call__(*args, **kwargs)

    result = run_smoke_turn(
        scenario="success",
        lane="default",
        db_path=smoke_db,
        commit=False,
        llm_factory=ObservedMock,
    )
    try:
        assert result.overall == "PASS"
        assert called
    finally:
        Path(result.working_db_path).unlink(missing_ok=True)


def test_dry_run_follows_dry_run_harness_isolation_pattern(smoke_db):
    result = run_smoke_turn(
        scenario="success",
        lane="default",
        db_path=smoke_db,
        commit=False,
    )
    try:
        working = Path(result.working_db_path)
        assert result.source_db_path == str(smoke_db)
        assert working != smoke_db
        assert working.parent == Path("/tmp")
        assert result.commit is False
    finally:
        Path(result.working_db_path).unlink(missing_ok=True)


def test_without_dry_run_flag_commit_mode_is_unchanged(smoke_db):
    result = run_smoke_turn(
        scenario="success",
        lane="default",
        db_path=smoke_db,
        commit=True,
    )
    assert result.commit is True
    assert result.working_db_path == str(smoke_db)


def test_dry_run_exits_zero_when_healthy(smoke_db):
    assert command(_args(smoke_db, dry_run=True, commit=False)) == 0

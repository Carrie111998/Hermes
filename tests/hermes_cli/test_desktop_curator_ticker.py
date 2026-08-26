"""Desktop curator ticker: a Desktop-only backend still runs the curator.

A Desktop install (Electron app) has no CLI chat session and never runs
``hermes gateway run``, so the only curator call sites — the CLI session
startup and the gateway housekeeping loop — never fire. That left a
Desktop-only user without weekly skill maintenance (issue #95441). This test
drives the Desktop backend's ``_curator_ticker_loop`` directly and asserts
that both ``maybe_run_curator`` and ``maybe_pull_skills`` actually get called
on a short cadence.
"""

import asyncio

import pytest

import hermes_cli.web_server as ws


@pytest.mark.asyncio
async def test_curator_ticker_ticks_curator_and_skill_sync(monkeypatch):
    """The desktop curator loop must call curator + skill-sync, never raise."""

    called = []

    def _curator(**kwargs):
        called.append("curator")

    def _skills():
        called.append("skills")

    # The loop imports these names inside _tick via function-local imports, so
    # patch the source modules; the loop re-reads them every iteration.
    import agent.curator as curator_mod
    import tools.skills_sync_client as sync_mod

    monkeypatch.setattr(curator_mod, "maybe_run_curator", _curator)
    monkeypatch.setattr(sync_mod, "maybe_pull_skills", _skills)

    task = asyncio.create_task(
        ws._curator_ticker_loop(interval_s=0.05, initial_delay_s=0)
    )

    try:
        for _ in range(200):
            if "curator" in called and "skills" in called:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert "curator" in called, "maybe_run_curator was never called by the ticker"
    assert "skills" in called, "maybe_pull_skills was never called by the ticker"

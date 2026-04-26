"""End-to-end verification of the agent_failure_cluster pipeline.

Roadmap gate (Phase 3.1): synthetic 3× Scout failure must produce a
Critic retro file within 60s.

Pipeline under test:
  CronEventEmitter.on_job_completed (3× failure with same error)
    → FailureClusterDetector reports cluster
    → bus.emit(AGENT_FAILURE_CLUSTER)
    → CriticSubscriber.poll picks up the event
    → subprocess.Popen invokes critic_retro.py (a stub here)
    → stub writes a retro file
    → assertion: retro file exists

This is an integration test (real subprocess, real SQLite bus); marked so
it runs only when explicitly requested.
"""

import time
from pathlib import Path
from textwrap import dedent

import pytest

from events.bus import EventBus
from events.producers.cron_emitter import CronEventEmitter
from events.subscribers.critic_trigger import CriticSubscriber

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_critic_script(tmp_path):
    """A real, executable critic_retro.py stub that records its invocations
    by writing a retro file to a known directory.
    """
    retros_dir = tmp_path / "retros"
    retros_dir.mkdir()
    script = tmp_path / "critic_retro.py"
    script.write_text(dedent(f'''\
        """Stub critic_retro.py for E2E test."""
        import sys
        from datetime import datetime
        from pathlib import Path

        retros = Path(r"{retros_dir}")
        retros.mkdir(parents=True, exist_ok=True)
        cluster_arg = ""
        if "--cluster" in sys.argv:
            cluster_arg = sys.argv[sys.argv.index("--cluster") + 1]
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        out = retros / f"retro-{{ts}}.md"
        out.write_text(f"# E2E retro\\ncluster={{cluster_arg}}\\n")
    '''))
    return script, retros_dir


def test_three_scout_failures_produce_critic_retro_within_60s(
    tmp_path, monkeypatch, stub_critic_script,
):
    """Synthetic 3× Scout failure should trigger Critic retro file within 60s."""
    script, retros_dir = stub_critic_script

    # Isolate detector state to tmp_path
    state_path = tmp_path / "events" / "failure_cluster_state.json"
    monkeypatch.setattr(
        "events.producers.cron_emitter.failure_cluster_state_path",
        lambda: state_path,
    )

    # Real EventBus on tmp SQLite
    bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")

    emitter = CronEventEmitter(bus)
    subscriber = CriticSubscriber(
        bus, critic_script_path=script, debounce_seconds=0,
    )

    # Three same-type Scout failures
    for i in range(1, 4):
        emitter.on_job_completed(
            job_id="scout",
            job_name="scout",
            success=False,
            duration=1.0,
            error="Bailing: CAPTCHA detected",
            consecutive_errors=i,
        )

    # Drive the subscriber poll loop manually with a 60s budget
    deadline = time.monotonic() + 60
    retro_files: list[Path] = []
    while time.monotonic() < deadline:
        subscriber.poll()
        retro_files = list(retros_dir.glob("retro-*.md"))
        if retro_files:
            break
        time.sleep(0.5)

    assert retro_files, (
        "No Critic retro file appeared within 60s — pipeline broken"
    )
    content = retro_files[0].read_text()
    assert "agent=scout" in content
    assert "type=captcha" in content


def test_three_mailbox_errors_produce_critic_retro_within_60s(
    tmp_path, monkeypatch, stub_critic_script,
):
    """Same gate, but exercising the *mailbox* path instead of the cron-emitter
    path.  Closes the MailboxTranslator ERROR-branch gap: agents that report
    failures via structured ERROR mailbox messages (without a non-zero cron
    exit code) must still trigger the cluster signal and Critic retro.

    Pipeline under test:
      MAILBOX_MESSAGE event (3× ERROR for same agent + same failure type)
        → MailboxTranslator.handle:
            - emit AGENT_ERROR (existing)
            - record into FailureClusterDetector (new wiring)
            - emit AGENT_FAILURE_CLUSTER on threshold (new wiring)
        → CriticSubscriber.poll picks up the cluster
        → subprocess.Popen → stub critic_retro.py → retro file
    """
    from events.bus import EventBus
    from events.schema import EventType
    from events.subscribers.mailbox_translator import MailboxTranslator

    script, retros_dir = stub_critic_script

    # Isolate detector state to tmp_path — note this targets MailboxTranslator's
    # import site, not cron_emitter's.
    state_path = tmp_path / "events" / "failure_cluster_state.json"
    monkeypatch.setattr(
        "events.subscribers.mailbox_translator.failure_cluster_state_path",
        lambda: state_path,
    )

    bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
    translator = MailboxTranslator(bus)
    subscriber = CriticSubscriber(
        bus, critic_script_path=script, debounce_seconds=0,
    )

    for _ in range(3):
        bus.emit(
            event_type=EventType.MAILBOX_MESSAGE,
            source="test",
            payload={
                "message_type": "ERROR",
                "from": "scout",
                "to": "main",
                "file": "fake_error_scout.json",
                "summary": "",
                "inner_payload": {
                    "message": "Bailing: CAPTCHA detected on login page",
                    "source_agent": "scout",
                },
            },
        )

    # Drive translator + subscriber polls with a 60s budget
    deadline = time.monotonic() + 60
    retro_files: list[Path] = []
    while time.monotonic() < deadline:
        translator.poll()
        subscriber.poll()
        retro_files = list(retros_dir.glob("retro-*.md"))
        if retro_files:
            break
        time.sleep(0.5)

    assert retro_files, (
        "No Critic retro file appeared within 60s for mailbox-ERROR path "
        "— MailboxTranslator → cluster wiring broken"
    )
    content = retro_files[0].read_text()
    assert "agent=scout" in content
    assert "type=captcha" in content

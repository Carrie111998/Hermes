"""Contract test for scripts/register-ai-usage-collector-task.ps1.

The scheduled task this registers runs unattended, so three settings are
load-bearing and pinned here: the 15-minute repetition interval, the 6-minute
execution limit, and -MultipleInstances IgnoreNew.

IgnoreNew -- not the interval/limit relation -- is what keeps runs from
stacking, so the interval and the limit move independently. The interval was
5 minutes until 2026-08-26; it went to 15 to cut cold interpreter starts from
288 to 96/day, because terminations (TaskScheduler event 329) were
dose-responsive to host memory pressure rather than to any tunable. A 3-minute
execution limit was tried and is explicitly rejected: it killed collector runs
mid-write.
"""

from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "register-ai-usage-collector-task.ps1"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_ai_usage_collector_registrar_contract():
    script = _script_text()

    assert "-RepetitionInterval (New-TimeSpan -Minutes 15)" in script
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 6)" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 3)" not in script


def test_registrar_does_not_hardcode_a_developer_home():
    """The runner path must be derived, not embedded.

    No other tracked script in ``scripts/`` embeds a developer's home
    directory; this one previously did, which is part of why it went
    uncommitted for so long.
    """
    script = _script_text()

    assert "$env:USERPROFILE" in script
    assert "C:\\Users\\diego" not in script

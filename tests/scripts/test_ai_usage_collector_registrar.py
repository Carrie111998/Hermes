"""Contract test for scripts/register-ai-usage-collector-task.ps1.

The scheduled task this registers runs unattended, so the two settings that
keep it from stacking -- a 5-minute repetition interval against a 6-minute
execution limit, plus -MultipleInstances IgnoreNew -- are load-bearing. A
3-minute limit was tried and is explicitly rejected: it killed collector runs
mid-write.
"""

from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "register-ai-usage-collector-task.ps1"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_ai_usage_collector_registrar_contract():
    script = _script_text()

    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in script
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

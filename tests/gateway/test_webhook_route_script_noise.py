"""#54833: webhook route-script stderr must not carry the benign malloc line."""

import logging
from types import SimpleNamespace

import pytest

_MALLOC_NOISE = (
    "python(16414) MallocStackLogging: can't turn off malloc stack logging "
    "because it was not enabled."
)


@pytest.fixture
def _scripts_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib

    import hermes_constants

    importlib.reload(hermes_constants)
    yield home
    importlib.reload(hermes_constants)


@pytest.fixture
def _darwin(monkeypatch):
    from hermes_cli import subprocess_noise

    monkeypatch.setattr(subprocess_noise, "sys", SimpleNamespace(platform="darwin"))


def test_failing_route_script_log_drops_noise_keeps_real_stderr(
    _scripts_home, _darwin, caplog
):
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    script = _scripts_home / "scripts" / "boom.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({(_MALLOC_NOISE + chr(10))!r})\n"
        "sys.stderr.write('real script broke\\n')\n"
        "sys.exit(9)\n",
        encoding="utf-8",
    )
    proc = WebhookRouteProcessor()

    with caplog.at_level(logging.INFO, logger="gateway.platforms.webhook_filters"):
        should_continue, _ = proc.run_route_script("boom.py", {"a": 1})

    assert should_continue is False
    logged = "\n".join(r.message for r in caplog.records)
    assert "real script broke" in logged
    assert "MallocStackLogging" not in logged


def test_noise_only_failure_still_logs_cleanly(_scripts_home, _darwin, caplog):
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    script = _scripts_home / "scripts" / "quiet.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({(_MALLOC_NOISE + chr(10))!r})\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    proc = WebhookRouteProcessor()

    with caplog.at_level(logging.INFO, logger="gateway.platforms.webhook_filters"):
        should_continue, _ = proc.run_route_script("quiet.py", {})

    assert should_continue is False  # exit code decides, not the emptied stderr
    assert "code=2" in "\n".join(r.message for r in caplog.records)

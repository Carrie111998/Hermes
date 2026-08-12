"""The direct-multi-file-invocation warning must fire exactly when it should.

Test isolation in this repo lives in the RUNNER (``scripts/run_tests_parallel.py``
spawns one pytest subprocess per file), not in ``tests/conftest.py``. So a plain
``pytest tests/<dir>`` still runs — it just shares one interpreter across every
file, where module-level state leaks. On ``tests/hermes_cli`` that produced 141
failures on a clean ``main``, none reproducible per-file and none of which CI
can ever see (SCA-4692).

The warning is what stops the next person reading that as a real red. These
tests pin its three cases, plus the constant the runner and conftest must agree
on — if they drift, the marker never arrives and every runner subprocess starts
printing the warning it is supposed to suppress.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import PER_FILE_ISOLATION_ENV, _warn_on_direct_multi_file_run

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_runner():
    """Import ``scripts/run_tests_parallel.py`` (not an installed module)."""
    path = REPO_ROOT / "scripts" / "run_tests_parallel.py"
    spec = importlib.util.spec_from_file_location("_sca4692_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Reporter:
    def __init__(self):
        self.lines = []

    def write_sep(self, *args, **kwargs):
        self.lines.append("SEP")

    def write_line(self, line):
        self.lines.append(line)


def _config(reporter, args=("tests/hermes_cli",)):
    return SimpleNamespace(
        args=list(args),
        pluginmanager=SimpleNamespace(getplugin=lambda name: reporter),
    )


def _items(*files):
    return [SimpleNamespace(location=(f, 1, "test_x")) for f in files]


def _warned(reporter):
    return any("share ONE interpreter" in line for line in reporter.lines)


def test_warns_on_multiple_files(monkeypatch):
    monkeypatch.delenv(PER_FILE_ISOLATION_ENV, raising=False)
    reporter = _Reporter()
    _warn_on_direct_multi_file_run(_config(reporter), _items("a.py", "b.py"))
    assert _warned(reporter)


def test_silent_on_single_file(monkeypatch):
    """``pytest tests/foo.py`` matches the runner's own boundary — no warning."""
    monkeypatch.delenv(PER_FILE_ISOLATION_ENV, raising=False)
    reporter = _Reporter()
    _warn_on_direct_multi_file_run(_config(reporter), _items("a.py", "a.py"))
    assert not _warned(reporter)


def test_silent_under_the_canonical_runner(monkeypatch):
    """The runner sets the marker, so its subprocesses must stay quiet."""
    monkeypatch.setenv(PER_FILE_ISOLATION_ENV, "1")
    reporter = _Reporter()
    _warn_on_direct_multi_file_run(_config(reporter), _items("a.py", "b.py"))
    assert not _warned(reporter)


def test_names_the_canonical_runner_in_the_message(monkeypatch):
    """A warning that doesn't say what to run instead just adds noise."""
    monkeypatch.delenv(PER_FILE_ISOLATION_ENV, raising=False)
    reporter = _Reporter()
    _warn_on_direct_multi_file_run(_config(reporter), _items("a.py", "b.py"))
    assert any("scripts/run_tests.sh" in line for line in reporter.lines)


def test_survives_a_missing_terminal_reporter(monkeypatch):
    """``-p no:terminal`` / embedding harnesses must not crash collection."""
    monkeypatch.delenv(PER_FILE_ISOLATION_ENV, raising=False)
    config = SimpleNamespace(
        args=["tests/"],
        pluginmanager=SimpleNamespace(getplugin=lambda name: None),
    )
    _warn_on_direct_multi_file_run(config, _items("a.py", "b.py"))


def test_runner_and_conftest_agree_on_the_marker():
    """Drift here silently re-enables the warning inside every runner child."""
    assert _load_runner().PER_FILE_ISOLATION_ENV == PER_FILE_ISOLATION_ENV


def test_runner_exports_the_marker_to_children():
    """The marker must actually reach the subprocess env, not just exist."""
    source = (REPO_ROOT / "scripts" / "run_tests_parallel.py").read_text()
    assert "env={**os.environ, PER_FILE_ISOLATION_ENV: \"1\"}" in source


@pytest.mark.parametrize("count", [2, 5, 57])
def test_reports_the_file_count(monkeypatch, count):
    monkeypatch.delenv(PER_FILE_ISOLATION_ENV, raising=False)
    reporter = _Reporter()
    files = [f"f{i}.py" for i in range(count)]
    _warn_on_direct_multi_file_run(_config(reporter), _items(*files))
    assert any(f"{count} test files" in line for line in reporter.lines)

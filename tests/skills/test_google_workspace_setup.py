"""Security-floor tests for the Google Workspace runtime installer."""

from __future__ import annotations

import importlib.util
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest


SETUP_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)


@pytest.fixture()
def setup_module():
    spec = importlib.util.spec_from_file_location(
        "test_google_workspace_setup_module",
        SETUP_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stale_google_transitives_are_reported_missing(setup_module, monkeypatch):
    installed = {
        "google-api-python-client": "2.194.0",
        "google-auth": "2.55.0",
        "google-auth-oauthlib": "1.3.1",
        "google-auth-httplib2": "0.3.1",
        "httplib2": "0.31.2",
        "pyasn1": "0.6.3",
    }

    def fake_version(name):
        try:
            return installed[name]
        except KeyError:
            raise PackageNotFoundError(name) from None

    monkeypatch.setattr(setup_module, "_distribution_version", fake_version)

    assert setup_module._missing_required_packages() == [
        "google-auth==2.55.1",
        "httplib2==0.32.0",
        "pyasn1==0.6.4",
    ]


def test_installer_repairs_stale_transitives(setup_module, monkeypatch):
    states = iter(
        [
            [
                "google-auth==2.55.1",
                "httplib2==0.32.0",
                "pyasn1==0.6.4",
            ],
            [],
        ]
    )
    monkeypatch.setattr(
        setup_module,
        "_missing_required_packages",
        lambda: next(states),
    )
    monkeypatch.setattr(
        setup_module,
        "_ensure_pip_floor",
        lambda *args, **kwargs: (True, ""),
    )
    calls = []
    monkeypatch.setattr(
        setup_module.subprocess,
        "check_call",
        lambda argv, **kwargs: calls.append(argv),
    )

    assert setup_module.install_deps() is True
    assert calls == [
        [
            setup_module.sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "google-auth==2.55.1",
            "httplib2==0.32.0",
            "pyasn1==0.6.4",
        ]
    ]


def test_standalone_pip_install_is_bounded_and_timeout_is_failure(
    setup_module, monkeypatch
):
    states = iter([["google-auth==2.55.1"]])
    monkeypatch.setattr(setup_module, "_missing_required_packages", lambda: next(states))
    monkeypatch.setattr(
        setup_module,
        "_ensure_pip_floor",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(setup_module.shutil, "which", lambda _name: None)
    calls = []

    def timed_out(argv, **kwargs):
        calls.append((argv, kwargs))
        raise setup_module.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(setup_module.subprocess, "check_call", timed_out)

    assert setup_module.install_deps() is False
    assert calls[0][1]["timeout"] == 300


def test_standalone_copy_uses_bundled_pip_floor_guard(setup_module, monkeypatch):
    """A copied skill must still verify pip before its direct install."""
    states = iter(
        [
            ["google-auth==2.55.1"],
            [],
        ]
    )
    monkeypatch.setattr(setup_module, "_ensure_pip_floor", None)
    monkeypatch.setattr(
        setup_module,
        "_missing_required_packages",
        lambda: next(states),
    )
    floor_probes = []
    installs = []

    def fake_run(argv, **kwargs):
        floor_probes.append((argv, kwargs))
        return type(
            "RunResult",
            (),
            {
                "returncode": 0,
                "stdout": "pip 26.1.2 from /standalone/site-packages/pip",
                "stderr": "",
            },
        )()

    monkeypatch.setattr(setup_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        setup_module.subprocess,
        "check_call",
        lambda argv, **_kwargs: installs.append(argv),
    )

    assert setup_module.install_deps() is True
    assert floor_probes and floor_probes[0][0][-1] == "--version"
    assert installs == [
        [
            setup_module.sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "google-auth==2.55.1",
        ]
    ]


def test_standalone_copy_fails_closed_when_pip_probe_errors(setup_module):
    result = setup_module._bundled_ensure_pip_floor(
        ["python", "-m", "pip"],
        runner=lambda *_args, **_kwargs: type(
            "RunResult",
            (),
            {"returncode": 1, "stdout": "", "stderr": "pip unavailable"},
        )(),
    )
    assert result[0] is False
    assert "probe failed" in result[1]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "WARNING: pip 99.0 is available\n"
            "pip 26.1.1 from /standalone/site-packages/pip",
            False,
        ),
        ("WARNING: pip 99.0 is available", False),
        ("pip 26.1.2 from    ", False),
        ("pip 26.1.2 from", False),
        ("pip 26.1.2, from /standalone/pip", False),
        ("pip 26.1.2) from /standalone/pip", False),
        (
            "pip 26.1.2 from /standalone/site-packages/pip [unexpected extra]",
            False,
        ),
        (
            "pip 26.1.2 from /standalone/site-packages/pip trailing",
            False,
        ),
        (
            "pip 26.1.2 from /standalone/site-packages/pip (python 3.13)",
            True,
        ),
        (
            "pip 26.1.2 from /srv/Hermes Agent/venv/site-packages/pip "
            "(python 3.13)",
            True,
        ),
        (
            r"pip 26.1.2 from C:\Program Files\Hermes Agent\venv\Lib"
            r"\site-packages\pip (python 3.13)",
            True,
        ),
        (
            "pip 26.1.2 from /home/user/[work]/'venv'/site-packages/pip",
            True,
        ),
        (
            "pip 26.1.2 from '/srv/Hermes Agent/venv/site-packages/pip' "
            "(python 3.13)",
            False,
        ),
        (
            "WARNING: pip 99.0 is available\n"
            "pip 26.1.2 from /standalone/site-packages/pip",
            True,
        ),
        (
            "pip 26.1.2 from /standalone/site-packages/pip\n"
            "pip 26.1.2 from /other/site-packages/pip",
            False,
        ),
    ],
)
def test_bundled_pip_floor_requires_one_canonical_record(
    setup_module, output, expected
):
    assert setup_module._bundled_pip_version_meets_floor(output) is expected


def test_shallow_standalone_copy_repo_search_has_no_fixed_parent_index(
    setup_module,
):
    """A shallow copied script must fall back instead of indexing parents."""
    assert setup_module._find_repo_root(Path("/setup.py")) is None


def test_standalone_copy_ignores_fake_hermes_ancestor(setup_module, tmp_path):
    """A fake sibling package must not become the trusted floor helper."""
    fake_root = tmp_path / "fake-hermes"
    (fake_root / "hermes_cli").mkdir(parents=True)
    (fake_root / "hermes_cli" / "_pip_security.py").write_text(
        "def ensure_pip_floor(*args, **kwargs): raise AssertionError('fake')\n",
        encoding="utf-8",
    )
    (fake_root / "pyproject.toml").write_text(
        '[project]\nname = "unrelated-project"\n',
        encoding="utf-8",
    )

    copied_script = fake_root / "copied" / "setup.py"
    assert setup_module._find_repo_root(copied_script) is None

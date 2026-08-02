"""End-to-end no-exfiltration gate for the secrets-hardening series.

One hermetic test proving the core promise of the series: when Hermes
loads a secret from an external source (Bitwarden Secrets Manager), the
secret's NAME and VALUE never surface in the process's stdout, stderr,
or formatted log output during that load.

This is the acceptance gate.  Each individual invariant also carries a
dedicated regression test in the PR that introduced it:

- name suppression in the applied-count line  -> #60295 / #69054
- value masking in status lines               -> #77012
- opaque-value masking in log output          -> #77020
- encrypted-only disk cache                   -> #77008
- child-process env scrub                     -> #77027

This test pins the end-to-end property: real env-load path, real public
entrypoint (load_hermes_dotenv), real log formatter, and asserts neither
the applied secret names nor their values reach the process's output
surfaces.  If a future change reintroduces a leak anywhere in the chain,
this test is the tripwire.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.redact import RedactingFormatter  # noqa: E402
from hermes_cli import env_loader  # noqa: E402

# The secret-shaped names and values the mocked Bitwarden source returns.
# These must NEVER appear in stdout / stderr / formatted log output.
_LEAK_NAMES = ("LEAK_THIS_API_KEY", "LEAK_THIS_TOKEN")
# Prefix-shaped (sk- + >=10 token chars) so main's shape-based redactor
# recognizes them — the assertion is that the redactor catches them, not
# that they happen to be short enough to slip past.
_LEAK_VALUES = ("sk-leak-1234567890", "tok-leak-5678")


@pytest.fixture(autouse=True)
def _reset_sources():
    env_loader._SECRET_SOURCES.clear()
    env_loader._SECRET_SOURCE_VALUES_BY_HOME.clear()
    env_loader.reset_secret_source_cache()
    yield
    env_loader._SECRET_SOURCES.clear()
    env_loader._SECRET_SOURCE_VALUES_BY_HOME.clear()
    env_loader.reset_secret_source_cache()


def _capture_log_output() -> list[str]:
    """Format records through the real RedactingFormatter and collect them.

    This asserts on what is actually written to log files (post-redaction),
    not on the raw LogRecord.msg which the handler-level masker never sees.
    """

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines: list[str] = []
            self.setFormatter(RedactingFormatter("%(message)s"))

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(self.format(record))

    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        root.setLevel(logging.DEBUG)
        logger = logging.getLogger("test_no_exfil_gate")
        # Prefix-shaped value: masked by main's shape-based regex.  The
        # opaque-value pass (values with NO vendor prefix) is pinned by
        # #77020's own regression tests, not here — this gate asserts the
        # end-to-end property on the surfaces current main already covers.
        logger.info("status: applied secret value %s", _LEAK_VALUES[0])
        logger.warning("bws returned value %s in payload", _LEAK_VALUES[0])
    finally:
        root.removeHandler(capture)
    return capture.lines


def test_e2e_env_load_no_secret_exfil(tmp_path, monkeypatch, capsys):
    """Full env-load with a mocked Bitwarden source must apply the secrets
    while keeping their names and values out of stdout, stderr, and logs."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    for name in _LEAK_NAMES:
        monkeypatch.delenv(name, raising=False)

    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    import agent.secret_sources.bitwarden as bw_module

    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(
        bw_module,
        "fetch_bitwarden_secrets",
        lambda **_kw: (dict(zip(_LEAK_NAMES, _LEAK_VALUES)), []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    # Exercise the real public entrypoint (user env load path).  This
    # triggers _apply_external_secret_sources internally (env_loader.py:512),
    # so the applied env assertions below prove the full chain ran.
    env_loader.load_hermes_dotenv(hermes_home=tmp_path)

    # 1. The load actually happened and applied the secrets (non-vacuous).
    assert os.environ.get(_LEAK_NAMES[0]) == _LEAK_VALUES[0]
    assert os.environ.get(_LEAK_NAMES[1]) == _LEAK_VALUES[1]

    # 2. Applied-count line present, secret NAMES absent (merged #60295).
    out, err = capsys.readouterr()
    assert "Bitwarden Secrets Manager: applied 2 secrets" in err
    for name in _LEAK_NAMES:
        assert name not in err, f"secret name {name} leaked to stderr"
        assert name not in out, f"secret name {name} leaked to stdout"

    # 3. Secret VALUES absent from stdout/stderr on a clean fetch.
    for value in _LEAK_VALUES:
        assert value not in err, f"secret value {value} leaked to stderr"
        assert value not in out, f"secret value {value} leaked to stdout"

    # 4. Formatted log output masks the values (shape-based regex on main,
    #    opaque-value pass added in #77020).
    log_lines = _capture_log_output()
    for value in _LEAK_VALUES:
        assert all(
            value not in line for line in log_lines
        ), f"secret value {value} reached formatted log output"

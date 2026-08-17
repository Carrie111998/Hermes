"""The ``node --version`` probe in ``check_whatsapp_requirements`` must not
mistake host load for a missing Node.js install.

Observed 2026-08-11 23:23:47 on the live gateway: ``connect()`` logged
"Connecting to whatsapp" at 23:23:40.678 and "Node.js not found" at
23:23:47.102 — a 6.4s gap.  A genuine resolution miss returns in ~0.01s
(``find_node_executable`` is a path lookup), so the probe had in fact run and
blown its ``timeout=5`` budget; ``except Exception: return False`` turned that
into "not installed", and the caller stamped a ``whatsapp_node_missing`` fatal
with ``retryable=False``.  WhatsApp stayed down until the next gateway boot on
a box where Node was fine (v24.14.0).

Same spawn-cost trap as ``_kill_port_process``/``netstat`` (bff71e7ed) and
``gateway.status.pid_exists`` (e467da742): a 5s budget is not a bound on
process spawn under load on this host.

Both tests assert on a FLAG — the kwarg actually passed, and the branch
actually taken — never on elapsed wall-clock, which would pass on the broken
code whenever the box happened to be idle.
"""

import subprocess


from plugins.platforms.whatsapp import adapter as wa


def test_node_version_probe_budget_is_not_five_seconds(monkeypatch):
    """The probe's timeout must be generous enough to survive a loaded box."""
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="v24.14.0", stderr="")

    monkeypatch.setattr(wa, "find_node_executable", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(wa.subprocess, "run", _fake_run)

    assert wa.check_whatsapp_requirements() is True
    assert seen.get("timeout", 0) >= 60, (
        "node --version ran under a %r-second budget; process spawn on this "
        "host was measured at 8.2s, 9.6s and 21.3s while idle" % seen.get("timeout")
    )


def test_probe_timeout_is_not_reported_as_node_missing(monkeypatch):
    """A timed-out probe means a loaded host, not an absent runtime.

    ``find_node_executable`` already resolved an executable on disk; that is
    the installation check.  Reporting "not found" here is what makes the
    caller's fatal non-retryable, so the timeout branch must stay distinct
    from the resolution-miss branch below.
    """
    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))

    monkeypatch.setattr(wa, "find_node_executable", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(wa.subprocess, "run", _timeout)

    assert wa.check_whatsapp_requirements() is True


def test_unresolvable_node_still_reports_missing(monkeypatch):
    """The real "not installed" case must keep returning False."""
    monkeypatch.setattr(wa, "find_node_executable", lambda _name: None)

    assert wa.check_whatsapp_requirements() is False


def test_nonzero_exit_still_reports_missing(monkeypatch):
    """A node that runs and fails is broken, and that is still a hard no."""
    monkeypatch.setattr(wa, "find_node_executable", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(
        wa.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )

    assert wa.check_whatsapp_requirements() is False


# ── The enablement path must never reach the probe above ────────────────────
#
# That 60s budget is DOUBLE pyproject's global ``--timeout=30`` pytest cap, and
# ``--timeout-method=thread`` does not fail the test — ``pytest_timeout`` calls
# ``os._exit(1)``, killing the interpreter and destroying the summary line, so
# the runner parses the whole file as "no tests ran" (exactly the failure mode
# that hid all 469 ``tests/hermes_cli/test_web_server.py`` tests until
# 427bfa9b6f).  Ordinary unit tests reach it through
# ``load_gateway_config()`` -> ``_apply_env_overrides()`` ->
# ``deps_probe = entry.deps_available_fn or entry.check_fn`` — and
# ``tests/conftest.py``'s live-system guard does not stop it: that guard blocks
# package installs, remote-installer pipes and ``hermes update``, not a node
# version probe.  So the platform supplies a ``deps_available_fn`` that answers
# from the filesystem, and the spawning ``check_fn`` is left to ``connect()``.


def test_deps_probe_spawns_nothing(monkeypatch):
    """The enablement probe must not create a process, ever."""
    def _boom(*args, **kwargs):
        raise AssertionError(
            "whatsapp_deps_available spawned a subprocess: %r" % (args,)
        )

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    assert wa.whatsapp_deps_available() in (True, False)


def test_registration_prefers_the_spawn_free_deps_probe():
    """``deps_available_fn`` is what config-load calls; it must be the cheap one.

    ``gateway/config.py`` falls back to ``check_fn`` only when
    ``deps_available_fn`` is None, so dropping this kwarg silently reinstates
    the 60s ``node --version`` spawn on every ``load_gateway_config()``.
    """
    captured = {}

    class _Ctx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    wa.register(_Ctx())

    assert captured["deps_available_fn"] is wa.whatsapp_deps_available
    assert captured["deps_available_fn"] is not wa.check_whatsapp_requirements
    assert captured["check_fn"] is wa.check_whatsapp_requirements

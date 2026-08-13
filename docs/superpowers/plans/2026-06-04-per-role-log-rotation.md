# Per-Role Catch-All Log Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the catch-all logs rotate to completion on Windows while the gateway + dashboard + proxy + devflow-bridge daemons all run, by giving each daemon its own per-role catch-all file instead of a shared `agent.log`.

**Architecture:** `setup_logging()` gains a `role` parameter; when set (or when `mode="gateway"`), the root catch-all handlers write to `agent-<role>.log` / `errors-<role>.log` instead of the shared `agent.log` / `errors.log`. A pure `infer_daemon_role(argv)` helper derives the role from the process subcommand and is wired only at the production import-time call sites. The Windows-safe `_ManagedRotatingFileHandler` is untouched — it simply operates on private, single-holder files now, so its rename never hits the cross-process lock.

**Tech Stack:** Python stdlib `logging` / `logging.handlers.RotatingFileHandler`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-04-per-role-log-rotation-design.md`

**Repo note:** agent-src is local-only. Commit to `main`, do NOT push. The
`docs/superpowers/{specs,plans}` paths are gitignored in this repo — do not
force-add them; they are local working docs. Commit only code + tests.
Auto-commit hook quirks apply: scope every `git add`/`git commit` to explicit
pathspecs, never `git add -A`. Author commits as Diego
(`--author="Diego <diegodearagao@gmail.com>"`).

**Run tests with the worktree on PYTHONPATH** (editable-install finder
hardcodes `~/.hermes/agent-src`; from a worktree you must prefix):
`PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py -p no:cacheprovider -q`
From the canonical checkout (`~/.hermes/agent-src`) the prefix is harmless.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `hermes_logging.py` | role inference (pure) + role→filename routing in `setup_logging` | Modify |
| `hermes_cli/main.py` | `hermes` CLI import-time logging init | Modify (1 line) |
| `cli.py` | standalone agent CLI import-time logging init | Modify (1 line) |
| `hermes_cli/logs.py` | `hermes logs` known-file map | Modify (4 entries) |
| `tests/test_hermes_logging.py` | unit + end-to-end tests | Modify (add classes) |

`gateway/run.py` is intentionally NOT modified — `mode="gateway"` defaults the role to `"gateway"`.

---

## Task 1: `infer_daemon_role()` — pure argv→role helper

**Files:**
- Modify: `hermes_logging.py` (add helper + a module constant near the other internal helpers, e.g. after `COMPONENT_PREFIXES` around line 159)
- Test: `tests/test_hermes_logging.py` (new class `TestInferDaemonRole`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_hermes_logging.py`:

```python
class TestInferDaemonRole:
    """infer_daemon_role() maps a process's argv to a daemon role or None."""

    def test_gateway_subcommand(self):
        assert hermes_logging.infer_daemon_role(["hermes", "gateway", "run"]) == "gateway"

    def test_dashboard_subcommand(self):
        assert hermes_logging.infer_daemon_role(["hermes", "dashboard"]) == "dashboard"

    def test_proxy_subcommand(self):
        assert hermes_logging.infer_daemon_role(
            ["hermes", "proxy", "start", "--provider", "nous"]
        ) == "proxy"

    def test_global_flags_before_subcommand(self):
        # Leading global flags must be skipped; the first positional wins.
        assert hermes_logging.infer_daemon_role(
            ["hermes", "--profile", "main", "gateway", "run"]
        ) == "gateway"

    def test_devflow_bridge_runner_by_argv0(self):
        assert hermes_logging.infer_daemon_role(
            ["/x/profiles/main/scripts/devflow_bridge_runner.py"]
        ) == "devflow-bridge"

    def test_transient_chat_is_none(self):
        assert hermes_logging.infer_daemon_role(["hermes", "chat"]) is None

    def test_logs_gateway_is_not_gateway_daemon(self):
        # `hermes logs gateway` tails the gateway log — it is NOT the daemon.
        assert hermes_logging.infer_daemon_role(["hermes", "logs", "gateway"]) is None

    def test_empty_argv_is_none(self):
        assert hermes_logging.infer_daemon_role([]) is None

    def test_defaults_to_sys_argv(self, monkeypatch):
        monkeypatch.setattr(hermes_logging.sys, "argv", ["hermes", "dashboard"])
        assert hermes_logging.infer_daemon_role() == "dashboard"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestInferDaemonRole -p no:cacheprovider -q`
Expected: FAIL — `AttributeError: module 'hermes_logging' has no attribute 'infer_daemon_role'` (and `sys` if not imported).

- [ ] **Step 3: Implement the helper**

In `hermes_logging.py`, add `import sys` to the imports block (line 35-41 area; `os` and `time` are already imported). Then add, just after the `COMPONENT_PREFIXES` dict (around line 159):

```python
# ---------------------------------------------------------------------------
# Daemon role inference
# ---------------------------------------------------------------------------

# Long-lived singleton daemons that each hold the catch-all log open for their
# whole lifetime. On Windows that shared handle blocks log rotation, so each
# gets its own per-role catch-all file (see setup_logging ``role``). Keyed by
# the process *subcommand* (first positional argv token) so that tailing
# commands like ``hermes logs gateway`` are NOT misclassified as the daemon.
_DAEMON_SUBCOMMAND_ROLES = {
    "gateway": "gateway",
    "dashboard": "dashboard",
    "proxy": "proxy",
}


def infer_daemon_role(argv: Optional[Sequence[str]] = None) -> Optional[str]:
    """Best-effort daemon role from a process's argv, else ``None``.

    Pure (argv-only) so it is deterministic and unit-testable; production
    callers pass the result to ``setup_logging(role=...)``. Returns ``None``
    for transient ``cli``/``cron`` processes, which keep the shared
    ``agent.log``.
    """
    argv = list(sys.argv if argv is None else argv)
    if argv:
        prog = os.path.basename(argv[0])
        if prog.startswith("devflow_bridge_runner"):
            return "devflow-bridge"
    # First positional token after argv[0] is the subcommand; skip flags.
    subcommand = next((tok for tok in argv[1:] if not tok.startswith("-")), None)
    return _DAEMON_SUBCOMMAND_ROLES.get(subcommand)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestInferDaemonRole -p no:cacheprovider -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add hermes_logging.py tests/test_hermes_logging.py
git -c user.name=Diego -c user.email=diegodearagao@gmail.com \
  commit --author="Diego <diegodearagao@gmail.com>" \
  -m "feat(logging): infer_daemon_role() argv->role helper" \
  -- hermes_logging.py tests/test_hermes_logging.py
```

---

## Task 2: `role` param routes catch-all files in `setup_logging`

**Files:**
- Modify: `hermes_logging.py` — `setup_logging()` signature + the agent.log/errors.log path construction (lines 166-249 area)
- Test: `tests/test_hermes_logging.py` (new class `TestRoleScopedCatchAll`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_hermes_logging.py`:

```python
class TestRoleScopedCatchAll:
    """role= routes the catch-all logs to per-role filenames."""

    def _rotating(self, name_substr):
        return [
            h for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == name_substr
        ]

    def test_transient_uses_shared_agent_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)  # role=None
        assert len(self._rotating("agent.log")) == 1
        assert len(self._rotating("errors.log")) == 1

    def test_role_uses_per_role_files(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, role="dashboard")
        assert len(self._rotating("agent-dashboard.log")) == 1
        assert len(self._rotating("errors-dashboard.log")) == 1
        # The shared catch-all is NOT attached for a daemon process.
        assert len(self._rotating("agent.log")) == 0
        assert len(self._rotating("errors.log")) == 0

    def test_gateway_mode_defaults_role_to_gateway(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        assert len(self._rotating("agent-gateway.log")) == 1
        assert len(self._rotating("errors-gateway.log")) == 1
        assert len(self._rotating("agent.log")) == 0
        # gateway.log + forensics still attach (unchanged behaviour).
        assert len(self._rotating("gateway.log")) == 1

    def test_explicit_role_overrides_gateway_mode_default(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(
            hermes_home=hermes_home, mode="gateway", role="gateway"
        )
        assert len(self._rotating("agent-gateway.log")) == 1

    def test_role_catch_all_actually_writes(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, role="proxy")
        logging.getLogger("test.proxy_role").info("proxy role line")
        for h in logging.getLogger().handlers:
            h.flush()
        agent_proxy = hermes_home / "logs" / "agent-proxy.log"
        assert agent_proxy.exists()
        assert "proxy role line" in agent_proxy.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestRoleScopedCatchAll -p no:cacheprovider -q`
Expected: FAIL — `setup_logging() got an unexpected keyword argument 'role'`.

- [ ] **Step 3: Implement the role routing**

In `hermes_logging.py`:

(a) Add the `role` parameter to the signature (after `mode`, before `force`, around line 172):

```python
    mode: Optional[str] = None,
    role: Optional[str] = None,
    force: bool = False,
) -> Path:
```

(b) Just after `log_dir = home / "logs"` (around line 209), resolve the
effective role and the catch-all paths:

```python
    # Daemon processes route their catch-all logs to per-role files so each
    # is the sole long-lived holder and rotation is not blocked by a sibling
    # process's open handle (Windows). mode="gateway" implies the gateway
    # role unless an explicit role was passed.
    if role is None and mode == "gateway":
        role = "gateway"
    _role_suffix = f"-{role}" if role else ""
    agent_log_path = log_dir / f"agent{_role_suffix}.log"
    errors_log_path = log_dir / f"errors{_role_suffix}.log"
```

(c) Replace the two hard-coded paths in the global-handlers block. Change
`log_dir / "agent.log"` (line 234) to `agent_log_path` and
`log_dir / "errors.log"` (line 244) to `errors_log_path`:

```python
        # --- agent.log (INFO+) — the main activity log ---------------------
        _add_rotating_handler(
            root,
            agent_log_path,
            level=level,
            max_bytes=max_bytes,
            backup_count=backups,
            formatter=RedactingFormatter(_LOG_FORMAT),
        )

        # --- errors.log (WARNING+) — quick triage log ----------------------
        _add_rotating_handler(
            root,
            errors_log_path,
            level=logging.WARNING,
            max_bytes=2 * 1024 * 1024,
            backup_count=2,
            formatter=RedactingFormatter(_LOG_FORMAT),
        )
```

(d) Update the `setup_logging` docstring `role` entry (add under the `mode`
parameter docs, around line 196):

```python
    role
        Per-role catch-all routing. When set, ``agent.log``/``errors.log``
        become ``agent-<role>.log``/``errors-<role>.log`` so a long-lived
        daemon owns its own files and Windows rotation is never blocked by a
        sibling's open handle. ``None`` (transient cli/cron) keeps the shared
        files. ``mode="gateway"`` defaults this to ``"gateway"``.
```

- [ ] **Step 4: Run the new tests AND the full existing suite to verify no regression**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py -p no:cacheprovider -q`
Expected: PASS — all tests, including the previously-existing `TestSetupLogging`, `TestGatewayMode`, `TestGatewayForensicsLog`, `TestWindowsSafeRollover`. (The existing tests call `setup_logging()` with no `role`, so they exercise the shared `agent.log`/`errors.log` path unchanged.)

- [ ] **Step 5: Commit**

```bash
git add hermes_logging.py tests/test_hermes_logging.py
git -c user.name=Diego -c user.email=diegodearagao@gmail.com \
  commit --author="Diego <diegodearagao@gmail.com>" \
  -m "feat(logging): role= routes catch-all logs to per-role files" \
  -- hermes_logging.py tests/test_hermes_logging.py
```

---

## Task 3: Wire role inference at production import sites

**Files:**
- Modify: `hermes_cli/main.py:347`
- Modify: `cli.py:678`

This is config glue (runs at module import); it is verified by reading +
the already-tested `infer_daemon_role`, not a new unit test.

- [ ] **Step 1: Edit `hermes_cli/main.py`**

Change line 347 from:

```python
    _setup_logging(mode="cli")
```

to:

```python
    from hermes_logging import infer_daemon_role as _infer_daemon_role
    _setup_logging(mode="cli", role=_infer_daemon_role())
```

- [ ] **Step 2: Edit `cli.py`**

Change line 678 from:

```python
    setup_logging(mode="cli")
```

to:

```python
    from hermes_logging import infer_daemon_role
    setup_logging(mode="cli", role=infer_daemon_role())
```

- [ ] **Step 3: Verify the wiring imports cleanly**

Run: `PYTHONPATH=$(pwd) python -c "import hermes_logging; print(hermes_logging.infer_daemon_role(['hermes','dashboard']))"`
Expected: prints `dashboard`.

Run (smoke — must not raise at import): `PYTHONPATH=$(pwd) python -c "import sys; sys.argv=['hermes','dashboard']; import hermes_cli.main" 2>&1 | tail -3`
Expected: no traceback from the logging-init block (any unrelated downstream import noise is fine — the `try/except` around `_setup_logging` already guards it).

- [ ] **Step 4: Commit**

```bash
git add hermes_cli/main.py cli.py
git -c user.name=Diego -c user.email=diegodearagao@gmail.com \
  commit --author="Diego <diegodearagao@gmail.com>" \
  -m "feat(logging): wire daemon-role inference at CLI import sites" \
  -- hermes_cli/main.py cli.py
```

---

## Task 4: Expose per-role files to `hermes logs`

**Files:**
- Modify: `hermes_cli/logs.py:30-34` (`LOG_FILES`)
- Test: `tests/test_hermes_logging.py` (new class `TestLogsKnownFiles`) — or extend an existing logs test if one exists. Self-contained test below imports the map directly.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hermes_logging.py`:

```python
class TestLogsKnownFiles:
    """hermes_cli.logs.LOG_FILES exposes the per-role daemon catch-all files."""

    def test_role_files_registered(self):
        from hermes_cli.logs import LOG_FILES
        assert LOG_FILES["agent-gateway"] == "agent-gateway.log"
        assert LOG_FILES["agent-dashboard"] == "agent-dashboard.log"
        assert LOG_FILES["agent-proxy"] == "agent-proxy.log"
        assert LOG_FILES["agent-devflow-bridge"] == "agent-devflow-bridge.log"

    def test_default_agent_still_present(self):
        from hermes_cli.logs import LOG_FILES
        assert LOG_FILES["agent"] == "agent.log"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestLogsKnownFiles -p no:cacheprovider -q`
Expected: FAIL — `KeyError: 'agent-gateway'`.

- [ ] **Step 3: Extend the map**

In `hermes_cli/logs.py`, replace the `LOG_FILES` dict (lines 30-34) with:

```python
# Known log files (name → filename)
LOG_FILES = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
    # Per-role daemon catch-all files (see hermes_logging.infer_daemon_role):
    # each long-lived daemon owns its own catch-all so Windows rotation is not
    # blocked by a sibling's open handle.
    "agent-gateway": "agent-gateway.log",
    "agent-dashboard": "agent-dashboard.log",
    "agent-proxy": "agent-proxy.log",
    "agent-devflow-bridge": "agent-devflow-bridge.log",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestLogsKnownFiles -p no:cacheprovider -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/logs.py tests/test_hermes_logging.py
git -c user.name=Diego -c user.email=diegodearagao@gmail.com \
  commit --author="Diego <diegodearagao@gmail.com>" \
  -m "feat(logging): register per-role catch-all files in hermes logs" \
  -- hermes_cli/logs.py tests/test_hermes_logging.py
```

---

## Task 5: End-to-end isolation proof (acceptance test)

This is the test that proves the fix: a per-role catch-all rotates to
completion EVEN WHILE the shared `agent.log` is pinned open by another
handle (the exact condition that defers rotation today).

**Files:**
- Test: `tests/test_hermes_logging.py` (new class `TestPerRoleRotationIsolation`)

- [ ] **Step 1: Write the test**

Add to `tests/test_hermes_logging.py`:

```python
class TestPerRoleRotationIsolation:
    """A daemon's per-role catch-all rotates even while the shared agent.log
    is held open by another process — the regression the per-role split fixes.
    """

    def _role_handler(self, tmp_path, monkeypatch, role):
        monkeypatch.setattr(hermes_logging, "_ROLLOVER_RETRY_DELAY_SEC", 0)
        base = tmp_path / f"agent-{role}.log"
        handler = hermes_logging._ManagedRotatingFileHandler(
            str(base), maxBytes=200, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return base, handler

    def test_role_file_rotates_crossplatform(self, tmp_path, monkeypatch):
        """Sanity: with no lock at all, the per-role file rotates normally."""
        base, handler = self._role_handler(tmp_path, monkeypatch, "dashboard")
        try:
            (tmp_path / "agent-dashboard.log.1").write_text("OLD-1", encoding="utf-8")
            handler.emit(logging.makeLogRecord({"msg": "current"}))
            handler.doRollover()
            assert "current" in (tmp_path / "agent-dashboard.log.1").read_text(encoding="utf-8")
            assert (tmp_path / "agent-dashboard.log.2").read_text(encoding="utf-8") == "OLD-1"
            assert base.exists()
            assert handler._rollover_blocked_until == 0.0
        finally:
            handler.close()

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only file-lock semantics")
    def test_role_file_rotates_while_shared_agent_log_is_pinned(self, tmp_path, monkeypatch):
        """The acceptance scenario: another process pins the SHARED agent.log;
        the daemon's private agent-<role>.log must still rotate cleanly
        (different file => no cross-process lock).
        """
        base, handler = self._role_handler(tmp_path, monkeypatch, "dashboard")
        shared = tmp_path / "agent.log"
        shared.write_text("shared-held-open", encoding="utf-8")
        try:
            handler.emit(logging.makeLogRecord({"msg": "role-line"}))
            # Sibling process holds the SHARED agent.log open the whole time.
            with open(shared, "a", encoding="utf-8"):
                handler.doRollover()
                # Rotation of the PRIVATE file completed despite the shared lock.
                assert (tmp_path / "agent-dashboard.log.1").read_text(encoding="utf-8") == "role-line\n" or \
                       "role-line" in (tmp_path / "agent-dashboard.log.1").read_text(encoding="utf-8")
                assert base.exists()
                assert handler.stream is not None
                assert handler._rollover_blocked_until == 0.0  # NOT deferred
        finally:
            handler.close()
```

- [ ] **Step 2: Run the test**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestPerRoleRotationIsolation -p no:cacheprovider -q`
Expected (on Windows): PASS (2 passed). On non-Windows: 1 passed, 1 skipped.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hermes_logging.py
git -c user.name=Diego -c user.email=diegodearagao@gmail.com \
  commit --author="Diego <diegodearagao@gmail.com>" \
  -m "test(logging): per-role rotation isolation while shared agent.log pinned" \
  -- tests/test_hermes_logging.py
```

---

## Task 6: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire logging test module**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py -p no:cacheprovider -v`
Expected: all PASS (on non-Windows, the one real-Windows-lock test in
`TestWindowsSafeRollover` and the one in `TestPerRoleRotationIsolation` show
as SKIPPED). Zero failures.

- [ ] **Step 2: Confirm the existing Windows-safe rollover tests are untouched/green**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/test_hermes_logging.py::TestWindowsSafeRollover -p no:cacheprovider -v`
Expected: PASS / SKIPPED only — no failures.

- [ ] **Step 3: Verify no stray `agent.log` reference broke**

Run: `PYTHONPATH=$(pwd) python -c "import hermes_logging, hermes_cli.logs, hermes_cli.main, cli" 2>&1 | tail -3`
Expected: imports succeed (no `AttributeError`/`ImportError` from the edits).

- [ ] **Step 4: Final summary commit is unnecessary** — all changes were
committed per-task. Confirm a clean tree for the touched files:

Run: `git status --porcelain hermes_logging.py hermes_cli/main.py hermes_cli/logs.py cli.py tests/test_hermes_logging.py`
Expected: empty output (all committed).

---

## Post-implementation (USER actions — do NOT do these automatically)

- **Restart the gateway** to load the new code (editable install). The
  watchdog respawns a killed gateway in ~1 min; or the user restarts it.
  After restart, the new daemons write `agent-gateway.log` etc.; the legacy
  `agent.log` stops growing from daemon writes and rotates on its next trip.
- **Verify live:** after the fleet has run a while, confirm
  `agent-gateway.log` stays at/under `max_size_mb` with `agent-gateway.log.1`
  backups present, and no rotation-deferred warnings in the logs.

## Self-Review (completed during planning)

- **Spec coverage:** role param (Task 2) ✓, infer helper (Task 1) ✓, wiring
  (Task 3) ✓, errors.log split (Task 2) ✓, tooling map (Task 4) ✓, isolation
  test (Task 5) ✓, existing tests green (Task 2/6) ✓, no reaper (by design,
  nothing to build) ✓, gateway extras unchanged (no gateway/run.py edit) ✓.
- **Placeholders:** none — every code/edit step shows full content.
- **Type/name consistency:** `infer_daemon_role`, `role`, `_DAEMON_SUBCOMMAND_ROLES`,
  `agent-<role>.log`/`errors-<role>.log`, `LOG_FILES["agent-<role>"]` used
  identically across tasks.

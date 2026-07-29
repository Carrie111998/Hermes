# Multi-repo CODE_DRIFT Detection Implementation Plan

> ## ⛔ SUPERSEDED — DO NOT EXECUTE (2026-07-28)
>
> This plan was **never run**. The feature it describes was implemented independently
> on `main` as **`a719fc7dd`** (deployed 2026-07-28 15:42:50), and the `events_doctor`
> unification as its follow-up. Working the checkboxes below would re-land shipped code
> under a **different naming scheme** and regress it.
>
> **Naming in this document is stale.** It says `hermes-home`,
> `code_drift_state_hermes_home.json`, `alertable`, `key`/`label`,
> `DriftSample.main`, `build_monitors()`. What exists is `hermes`,
> `code_drift_state.hermes.json`, `alerts`, `name`, `DriftSample.trunk`, and
> construction inline in `gateway_integration.startup()`. The planned
> `trunk_missing`/transient-no-op contract was restored by the corrective pass;
> see the AS-BUILT table and corrective contract in the spec for the full map.
>
> **Retained deliberately** for the reasoning the shipped code does not carry: the
> empirical pathspec verification (Global Constraints), the ruff/PLW1514 gate note, the
> Windows/PS 5.1 commit-message trap, and the per-task test rationale. Read it as a
> design record and a verification recipe book, not as a build script.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend CODE_DRIFT detection to watch `~/.hermes` (trunk `master`) alongside `~/.hermes/agent-src` (trunk `main`), and make an unresolvable trunk ref fail LOUD instead of silently reporting clean forever.

**Architecture:** The trunk ref becomes per-repo data (`WatchedRepo`) instead of a hardcoded `refs/heads/main`. `CodeDriftMonitor` stays single-repo and single-episode — a thin `build_monitors()` factory constructs one per watched repo, each with its own state file and probe gate — so the pure `evaluate()` edge core is untouched. `~/.hermes` alerting is gated on whether the executed dirs (`scripts/`, `ops/`, `profiles/*/scripts/`) actually differ, because a branch 62 commits behind may still have byte-identical scripts.

**Tech Stack:** Python 3.11, pytest, stdlib `subprocess` for read-only git, existing `events` bus/state/paths modules.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-07-28-multi-repo-code-drift-design.md`. Read it before Task 1.
- **The monitor NEVER mutates a repo.** Read-only git only. No fast-forward, no checkout, no fetch. Remediation stays a deliberate operator action.
- **Bounded subprocess:** every git call goes through the existing `_git()` helper with its 15 s timeout.
- **Executed-dir pathspecs are exactly** `":(glob)scripts/**"`, `":(glob)ops/**"`, `":(glob)profiles/*/scripts/**"`. **Verified empirically 2026-07-28 against the live `~/.hermes` repo.** A bare `profiles/*/scripts` matches **ZERO** files and fails silently — do not "simplify" to it. The `:(glob)` magic prefix and the trailing `/**` are both load-bearing.
- **`~/.hermes` path resolution uses `hermes_constants.get_default_hermes_root()`**, never `get_hermes_home()` (which resolves profile-scoped). This is what keeps `tests/events` hermetic under pytest's tempdir. No new env var.
- **Zero state migration.** `agent-src` keeps `code_drift_state.json` and its existing flat schema byte-untouched.
- **Lint gate is `ruff` with `select = ["PLW1514"]` only** (`pyproject.toml:412`). PLW1514 means **every** `open()` / `read_text()` / `write_text()` in text mode MUST pass an explicit `encoding=` — on Windows the default is the locale codepage. All test file writes in this plan already pass `encoding="utf-8"`; keep it that way. No other rule family is enforced, so do NOT add `# noqa` for codes that are not selected (an unused directive is just clutter). Run `ruff check --no-cache <changed files>` before each commit — per repo history, ruff hides "Invalid # noqa directive" warnings on cache hits.
- **Windows/PS 5.1:** run pytest from PowerShell. If a `git commit -m` message contains double quotes, use a temp file + `git commit -F` (PS 5.1 splits on embedded quotes).
- **Commit inside this worktree, on this worktree's branch.** Do not switch branches; do not `git add` files you did not create.
- **Test hermeticity is already handled — do not add your own env fixtures.** `tests/conftest.py:380` redirects both `HERMES_HOME` and `HOME` to per-test tempdirs. That means `_agent_src_root()` (via `Path.home()`), `_hermes_home_root()` (via `get_default_hermes_root()`, which returns an out-of-tree `HERMES_HOME` as-is), and `notifications_home()` all resolve inside the tempdir. Verified 2026-07-28: calling `build_monitors(bus)` in a test touches **no** live `~/.hermes` state.

---

### Task 1: `WatchedRepo` registry and per-repo state paths

**Files:**
- Modify: `events/producers/code_drift_monitor.py` (add after `_agent_src_root`, ~line 55)
- Modify: `events/paths.py:109-117` (`code_drift_state_path`)
- Test: `tests/events/producers/test_code_drift_monitor.py`
- Test: `tests/events/test_paths.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `WatchedRepo(key: str, path: Path, trunk_ref: str, label: str, executed_dirs: Tuple[str, ...] = ())` — frozen dataclass
  - `watched_repos() -> List[WatchedRepo]`
  - `HERMES_HOME_EXECUTED_DIRS: Tuple[str, ...]`
  - `code_drift_state_path(key: Optional[str] = None) -> Path`

- [ ] **Step 1: Write the failing registry test**

Append to `tests/events/producers/test_code_drift_monitor.py`:

```python
class TestWatchedRepos:
    def test_registry_has_both_repos_with_distinct_trunks(self):
        from events.producers.code_drift_monitor import watched_repos
        repos = {r.key: r for r in watched_repos()}
        assert set(repos) == {"agent-src", "hermes-home"}
        assert repos["agent-src"].trunk_ref == "refs/heads/main"
        assert repos["hermes-home"].trunk_ref == "refs/heads/master"

    def test_only_hermes_home_is_executed_dir_gated(self):
        from events.producers.code_drift_monitor import watched_repos
        repos = {r.key: r for r in watched_repos()}
        assert repos["agent-src"].executed_dirs == ()
        assert repos["hermes-home"].executed_dirs != ()

    def test_executed_dirs_use_glob_magic_not_bare_wildcard(self):
        """A bare `profiles/*/scripts` matches ZERO files in git (verified
        2026-07-28). The :(glob) prefix and trailing /** are load-bearing —
        losing them silently re-creates the fail-silent blind spot."""
        from events.producers.code_drift_monitor import HERMES_HOME_EXECUTED_DIRS
        assert HERMES_HOME_EXECUTED_DIRS == (
            ":(glob)scripts/**",
            ":(glob)ops/**",
            ":(glob)profiles/*/scripts/**",
        )
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py::TestWatchedRepos -v`
Expected: FAIL — `ImportError: cannot import name 'watched_repos'`

- [ ] **Step 3: Implement the registry**

In `events/producers/code_drift_monitor.py`, add `get_default_hermes_root` to the imports:

```python
from hermes_constants import get_default_hermes_root
```

Then add after `_agent_src_root()` (~line 55):

```python
# Executed surface of the ~/.hermes working tree: cron script-slot jobs and
# Windows Scheduled Tasks resolve absolute paths under these dirs, so a diff
# here means something stale is genuinely RUNNING.
#
# The `:(glob)` magic prefix and trailing `/**` are REQUIRED. Verified against
# the live repo 2026-07-28: a bare `profiles/*/scripts` pathspec matches ZERO
# files (git's default wildcard lets `*` cross `/`, so the trailing literal
# never aligns) and would silently report "nothing changed" forever.
HERMES_HOME_EXECUTED_DIRS = (
    ":(glob)scripts/**",
    ":(glob)ops/**",
    ":(glob)profiles/*/scripts/**",
)


@dataclass(frozen=True)
class WatchedRepo:
    """A checkout whose drift from its own trunk is worth alerting on."""

    key: str                              # state-file identity + payload field
    path: Path
    trunk_ref: str                        # "refs/heads/main" | "refs/heads/master"
    label: str                            # human-facing path
    executed_dirs: Tuple[str, ...] = ()   # git pathspecs; empty = no gating


def _hermes_home_root() -> Path:
    """The ~/.hermes root — NOT the profile-scoped home.

    get_default_hermes_root() returns an HERMES_HOME pointing outside
    ~/.hermes as-is, which is what keeps tests/events hermetic.
    """
    return Path(get_default_hermes_root())


def watched_repos() -> List[WatchedRepo]:
    """Every checkout the monitor watches.

    ~/.hermes is production: its working tree IS the deployment surface for
    cron script-slots and Scheduled Tasks. Its trunk is `master` and it has
    no `main` branch at all, which is why trunk_ref must be per-repo data.
    """
    return [
        WatchedRepo(
            key="agent-src",
            path=_agent_src_root(),
            trunk_ref="refs/heads/main",
            label="~/.hermes/agent-src",
        ),
        WatchedRepo(
            key="hermes-home",
            path=_hermes_home_root(),
            trunk_ref="refs/heads/master",
            label="~/.hermes",
            executed_dirs=HERMES_HOME_EXECUTED_DIRS,
        ),
    ]
```

- [ ] **Step 4: Run the registry tests to verify they pass**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py::TestWatchedRepos -v`
Expected: 3 passed

- [ ] **Step 5: Write the failing state-path test**

Append to `tests/events/test_paths.py`:

```python
def test_code_drift_state_path_legacy_key_is_unchanged():
    """agent-src keeps the original filename — zero migration."""
    from events.paths import code_drift_state_path
    assert code_drift_state_path().name == "code_drift_state.json"
    assert code_drift_state_path("agent-src").name == "code_drift_state.json"


def test_code_drift_state_path_new_key_gets_its_own_file():
    from events.paths import code_drift_state_path
    p = code_drift_state_path("hermes-home")
    assert p.name == "code_drift_state_hermes_home.json"
    assert p.parent == code_drift_state_path().parent
```

- [ ] **Step 6: Run it to make sure it fails**

Run: `python -m pytest tests/events/test_paths.py -k code_drift -v`
Expected: FAIL — `TypeError: code_drift_state_path() takes 0 positional arguments`

- [ ] **Step 7: Implement the keyed state path**

Replace `events/paths.py:109-117` with:

```python
def code_drift_state_path(key: Optional[str] = None) -> Path:
    """CodeDriftMonitor episode persistence (2026-07-21; keyed 2026-07-28).

    Holds {"alerting", "last_emit_wall", "last_shape"} so the falling-edge
    "resolved" event survives the common remediation path (FF the checkout,
    then restart the gateway). Wall-clock timestamps — same lesson as the
    notifier batch-age persistence. Cross-profile, so canonical root.

    `key` is a WatchedRepo.key. The default/"agent-src" case returns the
    original filename so the live episode needs no migration; every other
    repo gets its own sibling file.
    """
    if key is None or key == "agent-src":
        return notifications_home() / "code_drift_state.json"
    return notifications_home() / f"code_drift_state_{key.replace('-', '_')}.json"
```

If `Optional` is not already imported in `events/paths.py`, add `from typing import Optional` beside the `pathlib` import.

- [ ] **Step 8: Run the state-path tests to verify they pass**

Run: `python -m pytest tests/events/test_paths.py -k code_drift -v`
Expected: 2 passed

- [ ] **Step 9: Confirm the existing edge-core suite is untouched**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: all pre-existing tests still pass (20 original + 3 new = 23 passed)

- [ ] **Step 10: Commit**

```bash
git add events/producers/code_drift_monitor.py events/paths.py tests/events/producers/test_code_drift_monitor.py tests/events/test_paths.py
git commit -m "feat(events): add WatchedRepo registry and keyed drift state paths"
```

---

### Task 2: Sampler takes a trunk ref and fails LOUD when it is unresolvable

**Files:**
- Modify: `events/producers/code_drift_monitor.py:58-75` (`DriftSample`), `:89-145` (`sample_code_drift`)
- Test: `tests/events/producers/test_code_drift_monitor.py`

**Interfaces:**
- Consumes: `WatchedRepo` from Task 1
- Produces:
  - `DriftSample` gains `trunk_ref: str = "refs/heads/main"` and `branch: str = ""`
  - `DriftSample.state` gains the value `"trunk_missing"`
  - `sample_code_drift(repo=None, *, trunk_ref="refs/heads/main", executed_dirs=()) -> Optional[DriftSample]`

**This is the core of the fix.** Two probe failures that look alike must not behave alike:

| Probe result | Meaning | Behavior |
|---|---|---|
| `HEAD` unresolvable | git broken, empty repo, transient | `None` → no-op |
| `HEAD` ok, trunk unresolvable | wrong trunk name, branch deleted | `state="trunk_missing"` → alerts |

- [ ] **Step 1: Write the failing sampler tests**

Append to `tests/events/producers/test_code_drift_monitor.py`:

```python
@pytest.fixture
def master_repo(tmp_path):
    """A repo whose trunk is `master` and which has NO `main` — the ~/.hermes
    topology. HEAD sits on a feature branch 2 commits behind master."""
    repo = tmp_path / "hermes-home"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    (repo / "a.txt").write_text("one", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "feat/manifest-router")
    (repo / "a.txt").write_text("two", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "master moves on")
    (repo / "a.txt").write_text("three", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "master moves again")
    _git(repo, "checkout", "feat/manifest-router")
    return repo


class TestTrunkRefIsConfigurable:
    def test_master_trunk_detects_behind(self, master_repo):
        s = sample_code_drift(master_repo, trunk_ref="refs/heads/master")
        assert s.state == "behind"
        assert s.behind_count == 2

    def test_default_main_trunk_on_a_master_repo_is_loud_not_silent(
            self, master_repo):
        """THE BUG. Before this change the hardcoded refs/heads/main resolved
        rc != 0 and returned None — reporting clean forever."""
        s = sample_code_drift(master_repo)
        assert s is not None
        assert s.state == "trunk_missing"
        assert s.trunk_ref == "refs/heads/main"

    def test_unresolvable_head_is_still_a_noop(self, tmp_path):
        """git init with no commits: HEAD does not resolve. This is a genuine
        transient/broken case and must stay a no-op so the poll loop never
        fabricates drift or recovery."""
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        assert sample_code_drift(repo) is None

    def test_branch_name_is_reported_when_attached(self, master_repo):
        s = sample_code_drift(master_repo, trunk_ref="refs/heads/master")
        assert s.branch == "feat/manifest-router"

    def test_branch_is_HEAD_when_detached(self, repo):
        s = sample_code_drift(repo)
        assert s.branch == "HEAD"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py::TestTrunkRefIsConfigurable -v`
Expected: FAIL — `TypeError: sample_code_drift() got an unexpected keyword argument 'trunk_ref'`

- [ ] **Step 3: Add the new `DriftSample` fields**

In `events/producers/code_drift_monitor.py`, replace the `DriftSample` field block (lines 60-68) with:

```python
    """Point-in-time relationship of the checkout's HEAD to its trunk ref."""

    state: str  # "in_sync"|"behind"|"ahead"|"diverged"|"trunk_missing"
    head: str
    main: str
    behind_count: int = 0
    ahead_count: int = 0
    dirty: bool = False
    missed_subjects: Tuple[str, ...] = ()
    trunk_ref: str = "refs/heads/main"
    branch: str = ""            # "HEAD" when detached
```

Leave the `shape` property exactly as-is.

- [ ] **Step 4: Rewrite the sampler**

Replace `sample_code_drift` (lines 89-145) with:

```python
def sample_code_drift(
    repo: Optional[Path] = None,
    *,
    trunk_ref: str = "refs/heads/main",
) -> Optional[DriftSample]:
    """Read-only git probe of HEAD vs ``trunk_ref``.

    Returns None ONLY for genuine transients — no checkout, or a HEAD that
    will not resolve (git broken, repo with no commits). The caller treats
    None as a no-op so the poll loop never crashes and never fabricates a
    drift or a recovery.

    An unresolvable TRUNK is NOT a transient: if HEAD resolved, the repo and
    the git binary are demonstrably fine, so a missing trunk is a
    configuration/topology error. It returns state="trunk_missing" and
    ALERTS. Returning None here is what made ~/.hermes (trunk `master`, no
    `main` branch at all) report clean forever.
    """
    repo = Path(repo) if repo is not None else _agent_src_root()
    # .git is a directory in a normal checkout and a file in a worktree.
    if not (repo / ".git").exists():
        return None

    rc_head, head = _git(repo, "rev-parse", "--verify", "HEAD")
    if rc_head != 0:
        return None
    head = head.strip()
    dirty = bool(_git(repo, "status", "--porcelain")[1].strip())
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()

    rc_trunk, trunk = _git(repo, "rev-parse", "--verify", trunk_ref)
    if rc_trunk != 0:
        return DriftSample(
            state="trunk_missing", head=head, main="",
            trunk_ref=trunk_ref, branch=branch, dirty=dirty,
        )
    trunk = trunk.strip()

    common = dict(trunk_ref=trunk_ref, branch=branch, dirty=dirty)
    if head == trunk:
        return DriftSample(state="in_sync", head=head, main=trunk, **common)

    def _count(rev_range: str) -> int:
        out = _git(repo, "rev-list", "--count", rev_range)[1].strip()
        try:
            return int(out)
        except ValueError:
            return 0

    head_behind = _git(repo, "merge-base", "--is-ancestor",
                       "HEAD", trunk_ref)[0] == 0
    head_ahead = _git(repo, "merge-base", "--is-ancestor",
                      trunk_ref, "HEAD")[0] == 0

    if head_behind:
        subjects = tuple(
            line.strip() for line in
            _git(repo, "log", "--format=%h %s", f"-{MISSED_SUBJECTS_CAP}",
                 f"HEAD..{trunk_ref}")[1].splitlines()
            if line.strip()
        )
        return DriftSample(
            state="behind", head=head, main=trunk,
            behind_count=_count(f"HEAD..{trunk_ref}"),
            missed_subjects=subjects, **common,
        )
    if head_ahead:
        return DriftSample(
            state="ahead", head=head, main=trunk,
            ahead_count=_count(f"{trunk_ref}..HEAD"), **common,
        )
    return DriftSample(
        state="diverged", head=head, main=trunk,
        behind_count=_count(f"HEAD..{trunk_ref}"),
        ahead_count=_count(f"{trunk_ref}..HEAD"), **common,
    )
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py::TestTrunkRefIsConfigurable -v`
Expected: 5 passed

- [ ] **Step 6: Run the whole file — the original suite must be green**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: 25 passed (**17** pre-existing + 3 from Task 1 + 5 new). If any of the 17 pre-existing tests fail, the sampler rewrite changed behavior it must not have — fix before committing.

- [ ] **Step 7: Commit**

```bash
git add events/producers/code_drift_monitor.py tests/events/producers/test_code_drift_monitor.py
git commit -m "feat(events): make drift trunk ref configurable and fail loud when unresolvable"
```

---

### Task 3: Executed-dir gating via the `alertable` property

**Files:**
- Modify: `events/producers/code_drift_monitor.py` (`DriftSample`, `sample_code_drift`, `evaluate`)
- Test: `tests/events/producers/test_code_drift_monitor.py`

**Interfaces:**
- Consumes: `DriftSample`, `sample_code_drift(..., trunk_ref=...)` from Task 2
- Produces:
  - `DriftSample.executed_changed: Tuple[str, ...] = ()`
  - `DriftSample.executed_gated: bool = False`
  - `DriftSample.alertable -> bool` (property)
  - `sample_code_drift(..., executed_dirs: Tuple[str, ...] = ())`

**Why:** `~/.hermes` may sit on a long-lived feature branch by design, and a branch 62 commits behind may still have byte-identical scripts. Alerting is gated on whether the deployment surface actually differs.

- [ ] **Step 1: Write the failing gating tests**

Append to `tests/events/producers/test_code_drift_monitor.py`:

```python
EXEC_DIRS = (":(glob)scripts/**", ":(glob)ops/**",
             ":(glob)profiles/*/scripts/**")


class TestAlertable:
    def test_trunk_missing_alerts_even_when_gated_with_no_changed_files(self):
        """LOAD-BEARING. Without the trunk_missing early return, a gated repo
        has an empty changed-file set, falls through to False, and silently
        re-creates the exact blind spot this work exists to close."""
        s = DriftSample(state="trunk_missing", head="a" * 9, main="",
                        executed_gated=True, executed_changed=())
        assert s.alertable is True

    def test_gated_drift_with_untouched_executed_dirs_is_silent(self):
        s = DriftSample(state="behind", head="a" * 9, main="b" * 9,
                        behind_count=62, executed_gated=True,
                        executed_changed=())
        assert s.alertable is False

    def test_gated_drift_touching_executed_dirs_alerts(self):
        s = DriftSample(state="behind", head="a" * 9, main="b" * 9,
                        behind_count=3, executed_gated=True,
                        executed_changed=("scripts/gateway_watchdog.py",))
        assert s.alertable is True

    def test_ungated_drift_always_alerts(self):
        assert behind(3).alertable is True

    def test_in_sync_never_alerts(self):
        assert in_sync().alertable is False


class TestExecutedDirSampling:
    def test_changed_executed_file_is_listed(self, master_repo):
        _git(master_repo, "checkout", "master")
        (master_repo / "scripts").mkdir()
        (master_repo / "scripts" / "run.py").write_text("x", encoding="utf-8")
        _git(master_repo, "add", "-A")
        _git(master_repo, "commit", "-m", "add a script")
        _git(master_repo, "checkout", "feat/manifest-router")

        s = sample_code_drift(master_repo, trunk_ref="refs/heads/master",
                              executed_dirs=EXEC_DIRS)
        assert s.executed_gated is True
        assert "scripts/run.py" in s.executed_changed
        assert s.alertable is True

    def test_drift_outside_executed_dirs_is_gated_silent(self, master_repo):
        """master is 2 commits ahead but only touched a.txt — the deployment
        surface is byte-identical, so this must not reach the phone."""
        s = sample_code_drift(master_repo, trunk_ref="refs/heads/master",
                              executed_dirs=EXEC_DIRS)
        assert s.state == "behind"
        assert s.behind_count == 2     # context still reported
        assert s.executed_changed == ()
        assert s.alertable is False

    def test_profiles_glob_matches_nested_profile_scripts(self, master_repo):
        """Regression guard: a bare `profiles/*/scripts` pathspec matches
        ZERO files. Verified against the live repo 2026-07-28."""
        _git(master_repo, "checkout", "master")
        d = master_repo / "profiles" / "main" / "scripts"
        d.mkdir(parents=True)
        (d / "nightly.py").write_text("x", encoding="utf-8")
        _git(master_repo, "add", "-A")
        _git(master_repo, "commit", "-m", "add profile script")
        _git(master_repo, "checkout", "feat/manifest-router")

        s = sample_code_drift(master_repo, trunk_ref="refs/heads/master",
                              executed_dirs=EXEC_DIRS)
        assert "profiles/main/scripts/nightly.py" in s.executed_changed

    def test_ungated_repo_reports_no_executed_data(self, repo):
        s = sample_code_drift(repo)
        assert s.executed_gated is False
        assert s.executed_changed == ()


class TestGatedEpisodeTransitions:
    def test_gated_silent_then_alerting_fires_a_rising_edge(self, bus, tmp_path):
        silent = DriftSample(state="behind", head="a" * 9, main="b" * 9,
                             behind_count=62, executed_gated=True,
                             executed_changed=())
        loud = DriftSample(state="behind", head="a" * 9, main="b" * 9,
                           behind_count=63, executed_gated=True,
                           executed_changed=("ops/homeops.ps1",))
        m = make_monitor(bus, tmp_path)
        assert m.evaluate(silent, now=0.0) is None
        assert _drift_events(bus) == []
        assert m.evaluate(loud, now=60.0) is not None
        assert len(_drift_events(bus)) == 1
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -k "Alertable or ExecutedDir or GatedEpisode" -v`
Expected: FAIL — `TypeError: DriftSample.__init__() got an unexpected keyword argument 'executed_gated'`

- [ ] **Step 3: Add the fields and the `alertable` property**

In `DriftSample`, add after `branch`:

```python
    executed_changed: Tuple[str, ...] = ()
    executed_gated: bool = False
```

Then add this property beside `shape`:

```python
    @property
    def alertable(self) -> bool:
        """Whether this sample should drive an alert episode.

        Gating exists because ~/.hermes may sit on a long-lived feature
        branch by design: a branch 62 commits behind can still have
        byte-identical scripts, and alerting on that gets tuned out.

        The trunk_missing clause is LOAD-BEARING. Without it a gated repo
        with an unresolvable trunk has an empty executed_changed, falls
        through to False, and goes silent — which is the exact blind spot
        this monitor exists to close. Do not "simplify" it away.
        """
        if self.state == "in_sync":
            return False
        if self.state == "trunk_missing":
            return True
        if self.executed_gated and not self.executed_changed:
            return False
        return True
```

- [ ] **Step 4: Sample the executed dirs**

Add the `executed_dirs` parameter to `sample_code_drift`'s signature:

```python
def sample_code_drift(
    repo: Optional[Path] = None,
    *,
    trunk_ref: str = "refs/heads/main",
    executed_dirs: Tuple[str, ...] = (),
) -> Optional[DriftSample]:
```

Add this module-level helper next to `_git`:

```python
def _changed_executed(repo: Path, trunk_ref: str,
                      executed_dirs: Tuple[str, ...]) -> Tuple[str, ...]:
    """Files differing between HEAD and trunk within the executed pathspecs.

    Empty executed_dirs means the repo is not gated; returns () without
    running git.
    """
    if not executed_dirs:
        return ()
    out = _git(repo, "diff", "--name-only", f"HEAD..{trunk_ref}",
               "--", *executed_dirs)[1]
    return tuple(line.strip() for line in out.splitlines() if line.strip())
```

In the `trunk_missing` early return, add `executed_gated=bool(executed_dirs)`:

```python
    if rc_trunk != 0:
        return DriftSample(
            state="trunk_missing", head=head, main="",
            trunk_ref=trunk_ref, branch=branch, dirty=dirty,
            executed_gated=bool(executed_dirs),
        )
```

Then extend the shared `common` dict so every remaining return carries the data:

```python
    common = dict(
        trunk_ref=trunk_ref, branch=branch, dirty=dirty,
        executed_gated=bool(executed_dirs),
        executed_changed=_changed_executed(repo, trunk_ref, executed_dirs),
    )
```

- [ ] **Step 5: Switch `evaluate()` to `alertable` — one line**

In `evaluate()` (~line 211), change:

```python
        if sample.state == "in_sync":
```

to:

```python
        if not sample.alertable:
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -k "Alertable or ExecutedDir or GatedEpisode" -v`
Expected: 10 passed (5 Alertable + 4 ExecutedDirSampling + 1 GatedEpisodeTransitions)

- [ ] **Step 7: Run the whole file — original suite must still be green**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: 35 passed. The 17 pre-existing tests must be untouched: `executed_gated` defaults False, so `behind(3).alertable` is True and `in_sync().alertable` is False.

- [ ] **Step 8: Commit**

```bash
git add events/producers/code_drift_monitor.py tests/events/producers/test_code_drift_monitor.py
git commit -m "feat(events): gate hermes-home drift alerts on executed-dir changes"
```

---

### Task 4: `build_monitors()` factory and gateway wiring

**Files:**
- Modify: `events/producers/code_drift_monitor.py` (`CodeDriftMonitor.__init__`, `_repo_str`, `_emit_drift`, `_emit_resolved`)
- Modify: `events/gateway_integration.py:26,95,121,133,345-347,774-778`
- Test: `tests/events/producers/test_code_drift_monitor.py`
- Test: `tests/events/test_gateway_integration.py:396-410` (**rewrite** — asserts on literal source text)

**Interfaces:**
- Consumes: `WatchedRepo`, `watched_repos()`, `code_drift_state_path(key)`, `sample_code_drift(..., trunk_ref=..., executed_dirs=...)`
- Produces:
  - `CodeDriftMonitor(bus, *, repo: Optional[WatchedRepo] = None, repo_path=None, ...)`
  - `build_monitors(bus, **kw) -> List[CodeDriftMonitor]`
  - `gateway_integration.get_code_drift_monitors() -> List[CodeDriftMonitor]`
  - `gateway_integration.get_code_drift_monitor()` retained, returns the agent-src monitor or None

- [ ] **Step 1: Write the failing factory tests**

Append to `tests/events/producers/test_code_drift_monitor.py`:

```python
class TestBuildMonitors:
    def test_builds_one_monitor_per_watched_repo(self, bus):
        from events.producers.code_drift_monitor import build_monitors
        monitors = build_monitors(bus)
        assert len(monitors) == 2
        assert [m.repo_key for m in monitors] == ["agent-src", "hermes-home"]

    def test_each_monitor_gets_its_own_state_file(self, bus):
        from events.producers.code_drift_monitor import build_monitors
        paths = {m.repo_key: m._state_path for m in build_monitors(bus)}
        assert paths["agent-src"].name == "code_drift_state.json"
        assert paths["hermes-home"].name == "code_drift_state_hermes_home.json"

    def test_payload_identifies_the_repo(self, bus, tmp_path):
        from events.producers.code_drift_monitor import WatchedRepo
        m = make_monitor(
            bus, tmp_path,
            repo=WatchedRepo(key="hermes-home", path=tmp_path,
                             trunk_ref="refs/heads/master", label="~/.hermes"),
        )
        # trunk_ref on the sample mirrors what the sampler threads through
        # from the WatchedRepo in real use; the payload must report the ref
        # actually measured against, not the monitor's configured one.
        m.evaluate(
            DriftSample(state="behind", head="a" * 9, main="b" * 9,
                        behind_count=2, trunk_ref="refs/heads/master"),
            now=0.0,
        )
        p = _drift_events(bus)[-1].payload
        assert p["key"] == "hermes-home"
        assert p["repo"] == "~/.hermes"
        assert p["trunk_ref"] == "refs/heads/master"

    def test_trunk_missing_emits_warn_status(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        s = DriftSample(state="trunk_missing", head="a" * 9, main="",
                        trunk_ref="refs/heads/main", branch="master")
        assert m.evaluate(s, now=0.0) is not None
        p = _drift_events(bus)[-1].payload
        assert p["status"] == "warn"
        assert p["state"] == "trunk_missing"
        assert p["trunk_ref"] == "refs/heads/main"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py::TestBuildMonitors -v`
Expected: FAIL — `ImportError: cannot import name 'build_monitors'`

- [ ] **Step 3: Teach `CodeDriftMonitor` about `WatchedRepo`**

Replace the `__init__` signature block and body head (lines 157-176) with:

```python
    def __init__(
        self,
        bus: EventBus,
        *,
        repo: Optional[WatchedRepo] = None,
        repo_path: Optional[Path] = None,
        sampler: Optional[Callable[[], Optional[DriftSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        state_path: Optional[Path] = None,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        self._repo = repo
        self._repo_path = (
            Path(repo_path) if repo_path
            else (repo.path if repo is not None else None)
        )
        if sampler is None:
            if repo is not None:
                def sampler():
                    return sample_code_drift(
                        repo.path, trunk_ref=repo.trunk_ref,
                        executed_dirs=repo.executed_dirs)
            else:
                def sampler():
                    return sample_code_drift(self._repo_path)
        self._sampler = sampler
        # WALL clock, not monotonic: last_emit is persisted across restarts.
        self._clock = clock or time.time
        self._state_path = (
            Path(state_path) if state_path
            else code_drift_state_path(repo.key if repo is not None else None)
        )
```

Leave the remaining `__init__` body (interval fields, state load) unchanged.

Add this property after `__init__`:

```python
    @property
    def repo_key(self) -> str:
        return self._repo.key if self._repo is not None else "agent-src"
```

- [ ] **Step 4: Add `build_monitors` and enrich the payloads**

Add at module scope, after the `CodeDriftMonitor` class:

```python
def build_monitors(bus: EventBus, **kwargs) -> List[CodeDriftMonitor]:
    """One monitor per watched repo, each with its own state file and gate."""
    return [CodeDriftMonitor(bus, repo=r, **kwargs) for r in watched_repos()]
```

Replace `_repo_str` (lines 247-248) with:

```python
    def _repo_str(self) -> str:
        if self._repo is not None:
            return self._repo.label
        return str(self._repo_path or _agent_src_root())
```

In `_emit_drift`, set the status from the state and add the new payload keys:

```python
            payload={
                "status": "warn" if sample.state == "trunk_missing" else "drifting",
                "state": sample.state,
                "key": self.repo_key,
                "branch": sample.branch,
                "trunk_ref": sample.trunk_ref,
                "head": sample.head[:9],
                "main": sample.main[:9],
                "behind_count": sample.behind_count,
                "ahead_count": sample.ahead_count,
                "dirty": sample.dirty,
                "missed_subjects": list(sample.missed_subjects),
                "executed_changed": list(sample.executed_changed),
                "repo": self._repo_str(),
            },
```

In `_emit_resolved`, add `"key": self.repo_key` and `"trunk_ref": sample.trunk_ref` to the payload.

- [ ] **Step 5: Run the factory tests to verify they pass**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py::TestBuildMonitors -v`
Expected: 4 passed

- [ ] **Step 6: Rewrite the gateway wiring**

In `events/gateway_integration.py`:

Line 26 — extend the import:

```python
from events.producers.code_drift_monitor import CodeDriftMonitor, build_monitors
```

Line 95 — replace the singular global:

```python
_code_drift_monitors: List[CodeDriftMonitor] = []
```

Line 121 — in the `global` statement, rename `_code_drift_monitor` to `_code_drift_monitors`.

Line 133 — replace construction:

```python
    _code_drift_monitors = build_monitors(_bus)
```

Lines 345-347 — replace the accessor with both forms:

```python
def get_code_drift_monitors() -> List[CodeDriftMonitor]:
    """All code-drift monitors (one per watched repo)."""
    return _code_drift_monitors


def get_code_drift_monitor() -> Optional[CodeDriftMonitor]:
    """The agent-src code-drift monitor (back-compat accessor)."""
    return _code_drift_monitors[0] if _code_drift_monitors else None
```

Lines 774-778 — replace the poll-loop probe:

```python
            # Code-drift probe — each watched checkout vs its own trunk ref
            # (agent-src/main, ~/.hermes/master). Each monitor self-gates to
            # one read-only git sample per 15 min, so the per-tick call is a
            # clock comparison. Per-repo try/except: one broken checkout must
            # not stop the others from being probed.
            for _cdm in _code_drift_monitors:
                try:
                    _cdm.check()
                except Exception:
                    logger.exception("Code drift check failed (%s)",
                                     _cdm.repo_key)
```

Ensure `List` is in the `typing` import at the top of the file.

**Do NOT add a `shutdown()` reset.** Verified 2026-07-28: `shutdown()` (line 223) does not null `_health_monitor` or `_resource_monitor` either. Follow the existing pattern — `startup()` reassigns the global, and adding a lone reset for this one monitor would be an inconsistency, not a fix.

- [ ] **Step 7: Rewrite the 3 source-text wiring tests**

Replace `tests/events/test_gateway_integration.py:396-410` with:

```python
class TestCodeDriftMonitorWiring:
    """CodeDriftMonitor (2026-07-21 stale-checkout remediation; multi-repo
    2026-07-28) must be constructed at startup and probed in the poll loop —
    one monitor per watched repo."""

    def test_accessors_agree(self):
        assert gi.get_code_drift_monitors() is gi._code_drift_monitors
        if gi._code_drift_monitors:
            assert gi.get_code_drift_monitor() is gi._code_drift_monitors[0]

    def test_startup_constructs_all_watched_repos(self):
        src = inspect.getsource(gi)
        assert "_code_drift_monitors = build_monitors(_bus)" in src

    def test_poll_loop_probes_every_monitor(self):
        src = inspect.getsource(gi)
        assert "for _cdm in _code_drift_monitors:" in src
        assert "_cdm.check()" in src
```

- [ ] **Step 8: Run the wiring and producer suites**

Run these as TWO separate commands — a single `-k CodeDrift` applies across both file arguments and would silently select only a handful of the producer tests:

```bash
python -m pytest tests/events/test_gateway_integration.py -k CodeDrift -v
```

```bash
python -m pytest tests/events/producers/test_code_drift_monitor.py -v
```

Expected: 3 wiring tests passed, and 40 producer tests passed (36 after Task 3 and its follow-up fix, + 4 new here).

- [ ] **Step 9: Commit**

```bash
git add events/producers/code_drift_monitor.py events/gateway_integration.py tests/events/producers/test_code_drift_monitor.py tests/events/test_gateway_integration.py
git commit -m "feat(events): watch both repos via build_monitors and per-repo poll"
```

---

### Task 5: Unify `events_doctor` onto the shared sampler

**Files:**
- Modify: `hermes_cli/events_doctor.py:36-119` (delete `_AGENT_SRC_DEFAULT`, `_agent_src_root`, `_git`; rewrite `check_code_drift`)
- Test: `tests/hermes_cli/test_events_doctor.py:203-211` (**invert** — this test asserts the bug)

**Interfaces:**
- Consumes: `sample_code_drift`, `watched_repos`, `WatchedRepo`
- Produces: `check_code_drift(repo_path: Optional[Path] = None) -> int` — signature preserved; 7 of its 8 existing tests must pass unchanged

**IMPORTANT:** `test_missing_main_ref_skips_without_issue` at line 203 asserts `issues == 0` and `"skip" in out`. **That test pins the fail-silent blind spot in place.** It must be inverted, not preserved. Renaming it is part of the fix.

- [ ] **Step 1: Invert the test that encodes the bug**

Replace `tests/hermes_cli/test_events_doctor.py:203-211` with:

```python
    def test_missing_trunk_ref_fails_loudly(self, tmp_path, capsys):
        """Was `test_missing_main_ref_skips_without_issue` — it asserted the
        fail-silent behavior that hid ~/.hermes (trunk `master`, no `main`)
        from every check. An unresolvable trunk is a config error, not a
        transient: HEAD resolved, so git and the repo are demonstrably fine."""
        repo = _make_repo(tmp_path)
        _commit(repo, "a")
        _git(repo, "branch", "-m", "main", "trunk")

        issues = check_code_drift(repo_path=repo)
        out = capsys.readouterr().out
        assert issues == 1
        assert "[WARN]" in out or "[FAIL]" in out
        assert "trunk" in out.lower()
        assert "skip" not in out.lower()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python -m pytest tests/hermes_cli/test_events_doctor.py::TestCodeDrift::test_missing_trunk_ref_fails_loudly -v`
Expected: FAIL — `assert 0 == 1` (the current code skips)

- [ ] **Step 3: Rewrite `check_code_drift` on the shared sampler**

In `hermes_cli/events_doctor.py`, delete lines 36-59 (`_AGENT_SRC_DEFAULT`, `_agent_src_root`, `_git`) and the now-unused `os`/`subprocess`/`Tuple` imports if nothing else uses them. Add:

```python
from events.producers.code_drift_monitor import (
    WatchedRepo, sample_code_drift, watched_repos,
)
```

Replace `check_code_drift` (lines 62-119) with:

```python
def _report_repo_drift(r: WatchedRepo) -> int:
    """Render one watched repo's drift; return 1 if it is an issue."""
    if not (r.path / ".git").exists():
        print(f"[--] code drift [{r.key}] -- skipped "
              f"({r.path} is not a git checkout)")
        return 0

    s = sample_code_drift(r.path, trunk_ref=r.trunk_ref,
                          executed_dirs=r.executed_dirs)
    if s is None:
        print(f"[--] code drift [{r.key}] -- skipped "
              f"(cannot resolve HEAD in {r.path})")
        return 0

    trunk = r.trunk_ref.rsplit("/", 1)[-1]
    if s.state == "trunk_missing":
        print(f"[WARN] code drift [{r.key}]: trunk ref {r.trunk_ref} does NOT "
              f"resolve in {r.path} -- HEAD resolves, so this is a "
              "configuration error, not a transient. Drift for this repo is "
              "UNMEASURABLE until the trunk ref is corrected.")
        return 1

    if s.dirty:
        print(f"[NOTE] code drift [{r.key}]: working tree at {r.path} is DIRTY "
              "(uncommitted changes -- inspect manually, never auto-fixed)")

    if s.state == "in_sync":
        _check(f"code drift [{r.key}] (HEAD vs {trunk})", True,
               f"in sync @ {s.head[:9]}")
        return 0

    where = f"on {s.branch}" if s.branch and s.branch != "HEAD" else "detached"
    if s.state == "behind":
        print(f"[WARN] code drift [{r.key}]: working tree ({where}) LAGS "
              f"{trunk} by {s.behind_count} commit(s) "
              "-- landed fixes are NOT running")
        for subj in s.missed_subjects:
            print(f"  missed: {subj}")
    elif s.state == "ahead":
        print(f"[WARN] code drift [{r.key}]: HEAD ({where}) is AHEAD of "
              f"{trunk} by {s.ahead_count} commit(s) (working tree carries "
              "unlanded state -- land it or move the checkout back)")
        return 1
    else:
        print(f"[WARN] code drift [{r.key}]: HEAD ({where}) has DIVERGED from "
              f"{trunk} (HEAD {s.head[:9]} vs {trunk} {s.main[:9]}, neither "
              "is an ancestor of the other -- reconcile manually)")
        return 1

    if s.executed_gated:
        if s.executed_changed:
            print(f"  executed files differing ({len(s.executed_changed)}):")
            for f in s.executed_changed[:10]:
                print(f"    {f}")
        else:
            print("  NOTE: no executed-dir files differ -- the deployment "
                  "surface is byte-identical, so this is reported here but "
                  "deliberately does NOT raise a phone alert.")
    print(f"  remediation: git -C {r.path} merge --ff-only {trunk} "
          "(check for a clean tree first), then restart the gateway")
    return 1


def check_code_drift(repo_path: Optional[Path] = None) -> int:
    """Compare each watched checkout's HEAD against its own trunk ref.

    Read-only -- never mutates a repo. Returns the number of issues found.
    A missing repo degrades to a skip note so the doctor stays usable on
    boxes without the shared checkout; an unresolvable TRUNK does NOT --
    that is a configuration error and is reported as an issue.

    When `repo_path` is given, only that repo is checked; its trunk ref is
    looked up in the registry by path and defaults to refs/heads/main.
    """
    if repo_path is not None:
        repo_path = Path(repo_path)
        match = next((r for r in watched_repos()
                      if r.path == repo_path), None)
        target = match or WatchedRepo(
            key="repo", path=repo_path,
            trunk_ref="refs/heads/main", label=str(repo_path),
        )
        return _report_repo_drift(target)
    return sum(_report_repo_drift(r) for r in watched_repos())
```

- [ ] **Step 4: Run the inverted test to verify it passes**

Run: `python -m pytest tests/hermes_cli/test_events_doctor.py::TestCodeDrift::test_missing_trunk_ref_fails_loudly -v`
Expected: PASS

- [ ] **Step 5: Run the whole doctor suite**

Run: `python -m pytest tests/hermes_cli/test_events_doctor.py -v`
Expected: all pass. The other 7 `TestCodeDrift` tests are unmodified; if any fails, the rendering changed a string they assert on (`"[OK]"`, `"in sync"`, `"LAGS"`, `"1 commit"`, `"merge --ff-only main"`, `"restart the gateway"`, `"AHEAD"`, `"DIVERGED"`, `"DIRTY"`) — restore the exact wording rather than editing those tests.

- [ ] **Step 6: Add a two-repo rendering test**

Append to `class TestCodeDrift`:

```python
    def test_no_repo_path_reports_every_watched_repo(self, capsys, monkeypatch):
        import hermes_cli.events_doctor as ed
        from events.producers.code_drift_monitor import WatchedRepo
        fake = [
            WatchedRepo(key="agent-src", path=Path("/nope/a"),
                        trunk_ref="refs/heads/main", label="a"),
            WatchedRepo(key="hermes-home", path=Path("/nope/b"),
                        trunk_ref="refs/heads/master", label="b"),
        ]
        monkeypatch.setattr(ed, "watched_repos", lambda: fake)
        assert check_code_drift() == 0
        out = capsys.readouterr().out
        assert "[agent-src]" in out and "[hermes-home]" in out
```

Add `from pathlib import Path` to the test file's imports if absent.

- [ ] **Step 7: Run it and commit**

Run: `python -m pytest tests/hermes_cli/test_events_doctor.py -v`
Expected: all pass

```bash
git add hermes_cli/events_doctor.py tests/hermes_cli/test_events_doctor.py
git commit -m "fix(doctor): unify code drift on shared sampler and fail loud on missing trunk"
```

---

### Task 6: Phone-facing formatting for the new states

**Files:**
- Modify: `events/formatting.py:575-611` (`code_drift_body`)
- Test: `tests/events/test_formatting.py` (~line 520, `code_drift_body` tests)

**Interfaces:**
- Consumes: the payload keys added in Task 4 — `key`, `branch`, `trunk_ref`, `executed_changed`, `status="warn"`
- Produces: `code_drift_body(p: dict) -> str` (signature unchanged)

- [ ] **Step 1: Write the failing formatting tests**

Append to the `code_drift_body` test class in `tests/events/test_formatting.py`:

```python
    def test_trunk_missing_body_names_the_ref_and_says_unmeasurable(self):
        from events.formatting import code_drift_body
        body = code_drift_body({
            "status": "warn", "state": "trunk_missing",
            "key": "hermes-home", "repo": "~/.hermes",
            "trunk_ref": "refs/heads/main", "branch": "master",
            "head": "aaaaaaaaa", "main": "",
        })
        assert "refs/heads/main" in body
        assert "~/.hermes" in body
        assert "unmeasurable" in body.lower()

    def test_behind_body_names_the_branch_and_trunk(self):
        from events.formatting import code_drift_body
        body = code_drift_body({
            "status": "drifting", "state": "behind", "key": "hermes-home",
            "repo": "~/.hermes", "trunk_ref": "refs/heads/master",
            "branch": "feat/manifest-router", "behind_count": 62,
            "head": "aaaaaaaaa", "main": "bbbbbbbbb",
            "executed_changed": ["scripts/gateway_watchdog.py"],
        })
        assert "feat/manifest-router" in body
        assert "master" in body
        assert "62" in body
        assert "scripts/gateway_watchdog.py" in body

    def test_executed_files_are_capped(self):
        from events.formatting import code_drift_body
        body = code_drift_body({
            "status": "drifting", "state": "behind", "repo": "~/.hermes",
            "trunk_ref": "refs/heads/master", "branch": "b",
            "behind_count": 9, "head": "a" * 9, "main": "b" * 9,
            "executed_changed": [f"scripts/s{i}.py" for i in range(20)],
        })
        assert body.count("scripts/s") <= 5
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m pytest tests/events/test_formatting.py -k "trunk_missing or names_the_branch or executed_files_are_capped" -v`
Expected: FAIL — `assert 'unmeasurable' in ...`

- [ ] **Step 3: Extend `code_drift_body`**

Replace `events/formatting.py:575-611` with:

```python
def code_drift_body(p: dict) -> str:
    """Plain-language CODE_DRIFT body (2026-07-21; multi-repo 2026-07-28).

    The generic fallback would splat missed_subjects as a raw list; this is
    the operator's phone-facing diagnosis + remediation line.
    """
    p = p or {}
    repo = p.get("repo", "~/.hermes/agent-src")
    trunk_ref = p.get("trunk_ref", "refs/heads/main")
    trunk = trunk_ref.rsplit("/", 1)[-1]
    branch = p.get("branch") or ""
    where = f"on {branch}" if branch and branch != "HEAD" else "detached"

    if p.get("status") == "resolved":
        return f"{repo} back in sync with {trunk} @ {p.get('main', '?')}"

    state = p.get("state", "?")
    if state == "trunk_missing":
        return (
            f"{repo}: trunk ref {trunk_ref} does NOT resolve, but HEAD does — "
            "so this is a configuration error, not a transient git failure.\n"
            f"Drift for this checkout is UNMEASURABLE until it is corrected "
            f"(currently {where}).\n"
            "Fix: point the monitor at the repo's real trunk."
        )

    lines = []
    if state == "behind":
        lines.append(
            f"{repo} ({where}) LAGS {trunk} by {p.get('behind_count', '?')} "
            "commit(s) — landed fixes are NOT running."
        )
        for subj in (p.get("missed_subjects") or [])[:5]:
            lines.append(f"  missed: {subj}")
    elif state == "ahead":
        lines.append(
            f"{repo} ({where}) is AHEAD of {trunk} by "
            f"{p.get('ahead_count', '?')} commit(s) — the working tree "
            "carries unlanded state."
        )
    else:
        lines.append(
            f"{repo} ({where}) has DIVERGED from {trunk} "
            f"(HEAD {p.get('head', '?')} vs {trunk} {p.get('main', '?')})."
        )

    changed = p.get("executed_changed") or []
    if changed:
        lines.append(f"Executed files differing ({len(changed)}):")
        for f in changed[:5]:
            lines.append(f"  {f}")

    if p.get("dirty"):
        lines.append("Working tree is DIRTY (uncommitted changes).")
    if state == "behind":
        lines.append(
            f"Fix: git -C {repo} merge --ff-only {trunk}, "
            "then restart the gateway."
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the formatting tests to verify they pass**

Run: `python -m pytest tests/events/test_formatting.py -k code_drift -v`
Expected: all pass — including the 5 pre-existing ones. Those assert on `"DIRTY"`, `"DIVERGED"`, and the resolved body; the resolved wording changed from `"Deployed checkout back in sync with main @ ..."` to `"{repo} back in sync with {trunk} @ ..."`. If the existing resolved test pins the old literal, update **that one assertion** to match the new wording and note it in the commit message.

- [ ] **Step 5: Run the full events + doctor suites**

Run: `python -m pytest tests/events tests/hermes_cli/test_events_doctor.py -q`
Expected: green. Compare against the pre-change baseline — per the events-suite memory, the full `tests/events` run takes ~8 min and should be ~1256 passed / 1 skipped.

- [ ] **Step 6: Commit**

```bash
git add events/formatting.py tests/events/test_formatting.py
git commit -m "feat(events): render trunk-missing and per-repo drift bodies"
```

---

### Task 7: Live verification against the real repos

**Files:** none modified — this task is verification only.

**Interfaces:**
- Consumes: everything above

**Why:** every prior task tested against throwaway `tmp_path` repos. This confirms the pathspecs and trunk refs are right for the actual `~/.hermes` topology.

- [ ] **Step 1: Probe both real repos read-only**

```bash
python -c "from events.producers.code_drift_monitor import sample_code_drift, watched_repos; [print(r.key, '->', sample_code_drift(r.path, trunk_ref=r.trunk_ref, executed_dirs=r.executed_dirs)) for r in watched_repos()]"
```

Expected: two lines, neither `None`, neither `trunk_missing`. As of 2026-07-28 `~/.hermes` is on `master` and 0 behind, so `hermes-home` should read `state='in_sync'`. `agent-src`'s state depends on the checkout at run time.

- [ ] **Step 2: Confirm the repos were not mutated**

```bash
git -C ~/.hermes status --porcelain | head -5 && git -C ~/.hermes rev-parse --abbrev-ref HEAD
```

Expected: unchanged from before Step 1 — still `master`, same dirty files (`~/.hermes` carries a permanently dirty tree; that is expected and is payload-only).

- [ ] **Step 3: Run the doctor end to end**

```bash
python -m hermes_cli.events_doctor
```

Expected: two `code drift [<key>]` lines, one per repo, neither reporting a skip for a trunk ref.

**Note:** run this from the intended working directory. Per the `python -m` memory, `-m` puts CWD ahead of PYTHONPATH, so running from a worktree can shadow the installed package with a stale copy.

- [ ] **Step 4: Verify the state files**

```bash
ls ~/.hermes/notifications/code_drift_state*.json
```

Expected: `code_drift_state.json` still present and byte-unchanged (zero migration). A `code_drift_state_hermes_home.json` appears only once the gateway has run a `hermes-home` probe — its absence here is not a failure.

- [ ] **Step 5: Commit any incidental fixes**

If Steps 1-4 surfaced a real defect, fix it with a test first, then commit. If everything passed, there is nothing to commit — do not create an empty commit.

---

## Done When

- Both repos are watched, each against its own trunk ref.
- An unresolvable trunk emits `status="warn"` / `state="trunk_missing"` and makes `events_doctor` return non-zero, instead of silently reporting clean.
- A genuinely unresolvable HEAD is still a no-op — the poll loop never fabricates drift or recovery.
- `~/.hermes` drift alerts only when the executed dirs actually differ; `behind_count` is still reported as context.
- `code_drift_state.json` is byte-unchanged; no migration ran.
- `tests/events` and `tests/hermes_cli/test_events_doctor.py` are green.

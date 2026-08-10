# Hermes Testing and Pitfalls Guide

This document is a normative extension of the root [`AGENTS.md`](../../AGENTS.md).
Read it before any implementation, bug fix, refactor, test/CI change, or code
review. The sections below were moved from the root guide so project context
stays below its injection budget; their requirements are unchanged.

## Known Pitfalls

### DO NOT hardcode `~/.hermes` paths
Use `get_hermes_home()` from `hermes_constants` for code paths. Use `display_hermes_home()`
for user-facing print/log messages. Hardcoding `~/.hermes` breaks profiles — each profile
has its own `HERMES_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

### All CLI menu-pickers MUST use curses.
Interactive menus must use `hermes_cli/curses_ui.py`. See `hermes_cli/tools_config.py` for an example.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

### The gateway has TWO message guards — both must bypass approval/control commands
When an agent is running, messages pass through two sequential guards:
(1) **base adapter** (`gateway/platforms/base.py`) queues messages in
`_pending_messages` when `session_key in self._active_sessions`, and
(2) **gateway runner** (`gateway/run.py`) intercepts `/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny` before they reach
`running_agent.interrupt()`. Any new command that must reach the runner
while the agent is blocked (e.g. approval prompts) MUST bypass BOTH
guards and be dispatched inline, not via `_process_message_background()`
(which races session lifecycle).

### Squash merges from stale branches silently revert recent fixes
Before squash-merging a PR, ensure the branch is up to date with `main`
(`git fetch origin main && git reset --hard origin/main` in the worktree,
then re-apply the PR's commits). A stale branch's version of an unrelated
file will silently overwrite recent fixes on main when squashed. Verify
with `git diff HEAD~1..HEAD` after merging — unexpected deletions are a
red flag.

### Don't wire in dead code without E2E validation
Unused code that was never shipped was dead for a reason. Before wiring an
unused module into a live code path, E2E test the real resolution chain
with actual imports (not mocks) against a temp `HERMES_HOME`.

### Tests must not write to `~/.hermes/`
The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.hermes/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_hermes_home()` resolve within the temp dir.
Use the pattern from `tests/hermes_cli/test_profiles.py`:
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

---

## Testing

### Python
**ALWAYS use `scripts/run_tests.sh`** — do not call `pytest` directly. The script enforces
hermetic environment parity with CI (unset credential vars, TZ=UTC, LANG=C.UTF-8,
per-file subprocess isolation via `scripts/run_tests_parallel.py` — no xdist,
worker count auto-scaled from CPU count). Direct `pytest`
on a 16+ core developer machine with API keys set diverges from CI in ways
that have caused multiple "works locally, fails in CI" incidents (and the reverse).

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py -k test_x  # one test (file + -k; the runner is file-granular)
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
```

**Flake policy:** the runner auto-retries a failing test FILE once in a fresh
subprocess (`--file-retries`, default 1; `HERMES_TEST_FILE_RETRIES=0` to
disable). Pass-on-retry counts as green but is printed in a `⚠ FLAKY` summary
section with both attempts' output. A FLAKY report is a bug to fix, not noise
to ignore — timing-sensitive tests must not assume a quiet runner (loose
wall-clock bounds ≥ 2s, event-based sync, no `assert not _wait_until(...)`
negative-timing races).

#### Subprocess-per-test-file isolation

Every test file runs in a freshly-spawned Python subprocess via `run_tests_parallel.py`. This means module-level dicts/sets and
ContextVars from one test file cannot leak into the next.

#### Why the wrapper

|                     | Without wrapper                             | With wrapper                              |
| ------------------- | ------------------------------------------- | ----------------------------------------- |
| Provider API keys   | Whatever is in your env (auto-detects pool) | All env vars except a specific few unset. |
| HOME / `~/.hermes/` | Your real config+auth.json                  | Temp dir per test                         |
| Timezone            | Local TZ (PDT etc.)                         | UTC                                       |
| Locale              | Whatever is set                             | C.UTF-8                                   |

### Where to place what tests

The CI change classifier (`scripts/ci/classify_changes.py`) runs specific jobs based on what files changed. A Python test that asserts
about the contents of `package.json`, `package-lock.json`, `.ts`/`.tsx`
source, or any other JS-side artifact will not run on a PR that only touches
those files. This means a regression can go green on a PR and red on `main` (where the
classifier fails open and runs everything).

Any test that reads or asserts about `package.json`,
`package-lock.json`, `tsconfig.json`, `.ts`/`.tsx`/`.js`/`.mjs`/`.cjs`
source files configuration belongs in the JS (vitest) test suite, not in `tests/*.py`.

### Don't fake the host OS

Hermes supports Linux, macOS and native Windows, and plenty of its behaviour
genuinely differs per host. Those differences are tested by running on the
host, not by patching `sys.platform`.

```python
@pytest.mark.linux_only
@pytest.mark.macos_only
@pytest.mark.windows_only
```

Things that are host-independent can stay unmarked:

- **Pure functions that take a platform as data** —
  `hidden_windows_child_options(opts, is_windows=True)` is input→output, not a
  fake host. (Contrast: setting a module-level `IS_WINDOWS` flag and then
  calling `windows_detach_flags()` *is* a fake.)
- **Declaration/packaging invariants** — "pyproject declares `tzdata` with a
  `sys_platform == 'win32'` marker" asserts about a file, not about runtime.

The line: **if the test needs the interpreter to believe it is on another OS
in order to pass, it belongs on that OS.**
When one test body walks several platforms in sequence, split it.
Keep the host-native arm on the Linux lane and move the other arm into its own marked test.

**Use the marker, never a bare `skipif`.** `scripts/ci/list_os_marked_tests.py`
decides which files the macOS/Windows lanes import by grepping for the marker
*name*, and the lane then filters with `-m <marker>`. A test gated with
`@pytest.mark.skipif(sys.platform != "win32")` therefore skips on Linux AND is
never imported on the Windows lane — it runs on no host at all, silently. The
same trap catches a file-local alias (`windows_only = pytest.mark.skipif(...)`):
the grep matches the name, so the file *is* listed, but `-m windows_only`
deselects every test in it and the lane reports green over zero coverage.
Equally, don't `pytest.skip()` the non-host rows of a `@parametrize` over
platforms — split it into one marked test per OS, or only the host's row ever
executes.

### Don't write change-detector tests

A test is a **change-detector** if it fails whenever data that is **expected
to change** gets updated — model catalogs, config version numbers,
enumeration counts, hardcoded lists of provider models. These tests add no
behavioral coverage; they just guarantee that routine source updates break
CI and cost engineering time to "fix."

**Do not write:**

```python
# catalog snapshot — breaks every model release
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
assert "MiniMax-M2.7" in models

# config version literal — breaks every schema bump
assert DEFAULT_CONFIG["_config_version"] == 21

# enumeration count — breaks every time a skill/provider is added
assert len(_PROVIDER_MODELS["huggingface"]) == 8
```

**Do write:**

```python
# behavior: does the catalog plumbing work at all?
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1

# behavior: does migration bump the user's version to current latest?
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]

# invariant: no plan-only model leaks into the legacy list
assert not (set(moonshot_models) & coding_plan_only_models)

# invariant: every model in the catalog has a context-length entry
for m in _PROVIDER_MODELS["huggingface"]:
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

The rule: if the test reads like a snapshot of current data, delete it. If
it reads like a contract about how two pieces of data must relate, keep it.
When a PR adds a new provider/model and you want a test, make the test
assert the relationship (e.g. "catalog entries all have context lengths"),
not the specific names.

Reviewers should reject new change-detector tests; authors should convert
them into invariants before re-requesting review.

### Never read source code in tests

A test that reads a source file's text is testing *the shape of the
source code*, not its behavior. This is a hard antipattern, banned outright.
Any test that reads a .py, .ts, .tsx, etc., file is suspect.

**Why it's actively harmful, not just weak:**

- It passes when the implementation is subtly broken (the regex matches a
  call site that exists but is wired wrong) and fails when a correct
  refactor changes formatting, variable names, or control flow with
  identical runtime behavior. Both directions of failure are wrong.
- It can't be run against a built/bundled/minified artifact, so it silently
  stops testing anything the moment code moves, gets renamed, or a
  dependency reformats it.
- It actively blocks refactors: reviewers see "keeps a pattern intact" tests
  fail during pure structural cleanup with no behavior change, and either
  hand-wave the failure (dangerous) or waste time updating regexes that add
  nothing (waste).
- It gives false confidence. a green suite full of source-regex tests
  looks like coverage but has never once executed the code path it claims
  to guard.

**Do not write:**

```ts
const source = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')

test('backend spawn hides the Windows console', () => {
  assert.match(source, /spawn\(\s*backend\.command,\s*backend\.args[\s\S]{0,300}hiddenWindowsChildOptions/)
})
```

**Do write — extract the logic into a small pure/DI-testable function and
call it for real:**

```ts
// backend-spawn.ts
export function hiddenWindowsChildOptions(options: SpawnOptionsLike = {}, isWindows = process.platform === 'win32') {
  if (!isWindows || 'windowsHide' in options) return options
  return { ...options, windowsHide: true }
}

// backend-spawn.test.ts
test('windowsHide defaults to true on Windows, is left alone elsewhere', () => {
  assert.equal(hiddenWindowsChildOptions({}, true).windowsHide, true)
  assert.equal(hiddenWindowsChildOptions({}, false).windowsHide, undefined)
  assert.equal(hiddenWindowsChildOptions({ windowsHide: false }, true).windowsHide, false)
})
```

If the logic lives inline in a god-file (`main.ts`, `cli.py`,
`gateway/run.py`) and extracting it feels disruptive: that's the actual
signal to do the extraction, not to regex around it.

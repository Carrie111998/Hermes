# Testing Reference

The CI-parity runner contract, file placement, flake policy, and behavioral test
standards. Read this file before adding or reviewing tests.

## Python

**ALWAYS use `scripts/run_tests.sh`** — do not call `pytest` directly. The script
enforces hermetic environment parity with CI (unset credential vars, TZ=UTC,
LANG=C.UTF-8, per-file subprocess isolation via
`scripts/run_tests_parallel.py` — no xdist, worker count auto-scaled from CPU
count). Direct `pytest` on a developer machine with API keys set diverges from
CI in both directions.

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py -k test_x  # one test (file + -k; runner is file-granular)
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
```

**Flake policy:** the runner auto-retries a failing test FILE once in a fresh
subprocess (`--file-retries`, default 1;
`HERMES_TEST_FILE_RETRIES=0` disables it). Pass-on-retry counts as green but is
printed in a `⚠ FLAKY` summary with both attempts. A FLAKY report is a bug to
fix, not noise: use event-based synchronization, loose wall-clock bounds of at
least two seconds, and avoid negative-timing races such as
`assert not _wait_until(...)`.

### Subprocess-per-test-file isolation

Every test file runs in a freshly spawned Python subprocess through
`scripts/run_tests_parallel.py`. Module-level dicts/sets and ContextVars from
one test file therefore cannot leak into another.

### Why the wrapper

| | Without wrapper | With wrapper |
|---|---|---|
| Provider API keys | Whatever is in the environment | All except a specific few unset |
| HOME / `~/.hermes/` | Real config and auth state | Temp directory per test |
| Timezone | Local timezone | UTC |
| Locale | Host locale | C.UTF-8 |

## Where to place tests

The CI change classifier (`scripts/ci/classify_changes.py`) selects jobs from
changed paths. A Python test asserting against `package.json`, lockfiles,
TypeScript/JavaScript source, or TS config will not run for a frontend-only PR.
Tests for JS-side artifacts belong in the relevant Vitest suite, not
`tests/*.py`.

## Don't write change-detector tests

A change-detector fails whenever data expected to change gets updated — model
catalogs, config-version literals, enumeration counts, or hardcoded provider
lists. It adds no behavioral coverage.

**Do not write:**

```python
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
assert DEFAULT_CONFIG["_config_version"] == 21
assert len(_PROVIDER_MODELS["huggingface"]) == 8
```

**Do write:**

```python
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]
assert not (set(moonshot_models) & coding_plan_only_models)
for model in _PROVIDER_MODELS["huggingface"]:
    assert model.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

If a test reads like a snapshot of current data, delete it. If it asserts the
relationship two pieces of data must preserve, keep it.

## Never read source code in tests

A test that reads a source file's text tests implementation shape, not behavior.
It passes when wiring is wrong but a regex still matches, fails on behaviorally
neutral refactors, cannot test built/minified output, and creates false
confidence. Tests that read `.py`, `.ts`, `.tsx`, or similar source text are a
hard antipattern.

**Do not write:**

```ts
const source = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')

test('backend spawn hides the Windows console', () => {
  assert.match(source, /spawn\(\s*backend\.command[\s\S]*windowsHide/)
})
```

**Extract and execute the behavior instead:**

```ts
export function hiddenWindowsChildOptions(
  options: SpawnOptionsLike = {},
  isWindows = process.platform === 'win32',
) {
  if (!isWindows || 'windowsHide' in options) return options
  return { ...options, windowsHide: true }
}

test('windowsHide defaults only on Windows', () => {
  assert.equal(hiddenWindowsChildOptions({}, true).windowsHide, true)
  assert.equal(hiddenWindowsChildOptions({}, false).windowsHide, undefined)
  assert.equal(hiddenWindowsChildOptions({ windowsHide: false }, true).windowsHide, false)
})
```

If logic lives inline in a god-file and extraction feels disruptive, that is the
signal to extract a small pure/DI-testable function rather than regex the file.

## Profile tests

The `_isolate_hermes_home` autouse fixture redirects `HERMES_HOME`; never write
to the real `~/.hermes`. Tests for profile discovery must also mock
`Path.home()` because `_get_profiles_root()` is intentionally HOME-anchored:

```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

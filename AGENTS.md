# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

**Never give up on the right solution.**

> **Long-form guide:** contribution rubric, architecture, tools, config, testing,
> and pitfalls live in
> [`fork/harness/upstream-development-guide.md`](fork/harness/upstream-development-guide.md)
> (upstream-aligned). Fork-owned catalog:
> [Fork-specific features](fork/harness/upstream-development-guide.md#fork-specific-features-for-ai-agents).
> Keep *this* file short for Cursor / continual-learning.

## What Hermes Is

Hermes is a personal AI agent with one core across CLI, messaging gateway
(~20 platforms), TUI, and Electron desktop. Capability grows via **plugins and
skills**, not by thickening the core.

Two sacred properties:

- **Per-conversation prompt caching is sacred.** Do not mutate past context,
  swap toolsets, or rebuild the system prompt mid-conversation (exception:
  context compression).
- **Narrow waist; capability at the edges.** Prefer: extend existing → CLI +
  skill → service-gated tool (`check_fn`) → plugin → MCP catalog → new core
  tool (last resort).

## Fork Overlay

This checkout keeps fork-only behaviour at extension and ops edges. Before an
upstream merge or fork-owned edit, read [`fork/AGENTS.md`](fork/AGENTS.md) and
the nested guide. Authoritative merge mechanism:
`scripts/merge_tools/` / `scripts/sync_all.py` — upstream base, reapply verified
fork advantages only. Detail catalog:
[Fork-specific features](fork/harness/upstream-development-guide.md#fork-specific-features-for-ai-agents).

| Need | Guide |
|------|--------|
| Merge / overlays | [`fork/harness/AGENTS.md`](fork/harness/AGENTS.md) |
| Hypura `harness_*` / daemon | [`fork/agent-harness/AGENTS.md`](fork/agent-harness/AGENTS.md) |
| Plugins / fork tools | [`fork/extensions/AGENTS.md`](fork/extensions/AGENTS.md) |
| Windows stack / ports | [`fork/operations/AGENTS.md`](fork/operations/AGENTS.md) |
| Root scratch | [`fork/local-workspace/AGENTS.md`](fork/local-workspace/AGENTS.md) |

Secrets in `.env` only; non-secret behaviour in `config.yaml`. No new non-secret
`HERMES_*` env vars. Do not commit `_docs/`, media, release bundles, or
`node_modules/`.

## Root Layout Policy

Match upstream root style: packaging/entry Python (`run_agent.py`, `cli.py`,
`model_tools.py`, …), `scripts/`, `docs/`, `tests/`, apps/UI packages stay at
their official paths — **do not relocate** for “cleanliness” (imports, CI, and
`pyproject` assume them).

| Stay at root (intentional) | Why |
|----------------------------|-----|
| Core `*.py` entry modules, `pyproject.toml`, lockfiles | Official packaging surface |
| `hermes_api_server.py`, `sync_memory.py`, `requirements.txt` | Fork helpers wired by tests/scripts |
| `fork/`, `vendor/`, `brain/`, `SOUL.md` (local) | Fork / identity; not upstream PR material |

| Move / classify | Destination |
|-----------------|-------------|
| One-off probes, `tmp_*.py`, tweet drafts, comparison notes | `tmp/probes/` or `output/reports/` (gitignored) |
| Tracked operator notes (`TASK_SUMMARY`, release tweet drafts) | `fork/local-workspace/notes/` |
| Generated media / logs / `_docs` | `output/*`, `_docs/` — never publish |

See [`fork/local-workspace/README.md`](fork/local-workspace/README.md).

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

## Learned User Preferences

- Hermes系再起動は、デスクトップ再ビルド（`hermes desktop --build-only --force-build`）＋ llama ホットスタンバイ（`-StartLlama`）。この経路では `--skip-build` / worktree 起動を使わない。
- Desktop は canonical の packaged `apps/desktop/release/win-unpacked/Hermes.exe`（または同等の release 成果物）から起動する。`.worktrees\...` 上の `npx electron` / `--source --skip-build` は失敗パターンとして避ける。
- User 環境の `HERMES_DESKTOP_HERMES_ROOT` は canonical リポジトリルートのみ。worktree パスを指させない。
- 公式（upstream）向け PR は King's English。`_docs/` と fork 専用面は含めない。
- main への push では環境変数・秘密ファイルをコミットしない。`_docs/` もリモートに載せない。
- スキン壁紙（`background_image`）を有効にするときは、チャット本文が読めるコントラスト／オーバーレイを必須とする。

## Learned Workspace Facts

- Windows の正本チェックアウトは `C:\Users\downl\Documents\New project\hermes-agent` の `main`。Desktop/llama 再起動の RepoRoot に `.worktrees\main-ci-harness-uv` 等を使わない。
- Remotes: `origin` = zapabob/hermes-agent、`upstream` = NousResearch/hermes-agent。
- Desktop/llama 再起動の運用入口は `fork/operations/AGENTS.md` と `scripts/windows/`（例: `restart-hermes-stack.ps1`）。
- ローカル llama サーバーをカスタムプロバイダとして使う想定。このマシンではコンテキスト長 131072 を希望することがある。

# Claude session notes — uaixo/HTAI-Personal-Assistant

## Standing user instructions (apply to every session and every task)

- "Please do not make any assumptions. Please ask any questions when things
  are not clear or certain." — When a decision point is ambiguous, when
  evidence is missing, or when multiple reasonable interpretations exist,
  stop and ask the user (AskUserQuestion) instead of picking one silently.
  Verify claims against the actual repo/CI/API state before acting on them.
- Autonomy boundary, so the rule above doesn't over-trigger: ASK before
  anything destructive, irreversible, outward-facing, or scope-changing, and
  whenever requirements genuinely allow more than one reading. PROCEED
  without asking for reversible mechanical steps that follow directly from
  the agreed task — running checks/tests, syncing the session branch, and
  the recorded PR → CI → squash-merge flow.

## Branches

- **`NousAI-Assistant` is the active development line**: a snapshot of upstream
  `nousresearch/hermes-agent` plus local additions. Base all work on it.
- `main` is a stale upstream copy with unrelated history — never base work on
  it, never open PRs against it.
- Never open PRs against `nousresearch/hermes-agent`. Upstream flows one way:
  upstream → this repo.

## Merge workflow (user-approved, 2026-07-22)

Land session work fully automatically — the user does not want to touch PRs:

1. **Sync first — before any code change**: `git fetch origin NousAI-Assistant`
   so work never starts from a stale tip.
2. **Fresh branch per change (user-approved 2026-07-22)**: cut a NEW branch
   `claude/nousai-<topic>` from `origin/NousAI-Assistant` for every change.
   Never reuse a branch whose PR has merged — reuse forces non-fast-forward
   pushes, which the permission classifier blocks and the stop hook flags.
   Fresh branches keep every push a plain fast-forward.
3. Push with plain `git push -u origin <branch>` (no force flags), open a PR
   **based on `NousAI-Assistant`** (draft is fine).
4. Watch CI (`ci.yml` runs on `pull_request` only — direct pushes to
   `NousAI-Assistant` run no CI, which is why the PR step exists).
5. When green: mark ready and **squash-merge without asking**. Only pause for
   user input if CI reveals a real problem or the change is risky/destructive.
   After the merge, leave the remote feature branch alone (the user deletes
   merged branches from the GitHub UI); never push more commits to it.

If repo Settings → Pull Requests → "Allow auto-merge" gets enabled, arm
auto-merge (squash) at PR creation instead of watch-and-merge.

## Upstream-sync safety rules

- Keep `NousAI-Assistant` conflict-free against upstream: **add-only files**;
  do not modify upstream-owned files — EXCEPT the Phase 2 brand-pack carve-out
  below (user-approved 2026-07-22). Widening that carve-out needs an explicit
  user request.
- **Phase 2 carve-out** — these upstream-owned files intentionally diverge and
  may conflict on upstream syncs; resolve by keeping upstream's changes and
  re-asserting the NousAI brand values:
  - `assets/banner.png` (NousAI banner)
  - `apps/desktop/assets/icon.{png,ico,icns}` (NousAI icons)
  - `apps/desktop/package.json` (productName/executableName `NousAI`, appId
    `ai.nous.desktop`, artifactName `NousAI-…` — full-brand internals,
    user-approved 2026-07-22; only the `hermes://` protocol scheme and npm
    `name` stay upstream)
  - `apps/desktop/scripts/test-desktop.mjs` + `apps/desktop/e2e/fixtures.ts`
    (packaged-app paths derive from package.json productName/executableName
    instead of hardcoding `Hermes` — required because CI packages the app
    and asserts those paths)
  - `hermes_cli/main.py` (brand-agnostic packaged desktop app lookup on
    macOS — user commit)
  - `apps/desktop/src/components/chat/intro.tsx` +
    `apps/desktop/src/components/chat/intro-copy.jsonl` (empty-session hero:
    WORDMARK `NOUS AI ASSISTANT`, intro copy de-Hermes'd — user-approved
    2026-07-23)
  - `apps/desktop/electron/main.ts` (one line: APP_NAME default `'NousAI'` —
    drives macOS menu "About/Quit X", about panel, app.setName; the
    HERMES_DESKTOP_APP_NAME env override is upstream and unchanged)
  - `apps/desktop/src/components/brand-mark.tsx` (BrandMark renders the added
    `public/nousai-mark.png` instead of `nous-girl.jpg`; upstream's asset file
    stays untouched — user-approved 2026-07-23)
  - "Hermes Desktop" → "NousAI Desktop" everywhere the app displays it
    (user-approved full sweep 2026-07-23, re-swept 2026-07-26 after upstream
    added new onboarding strings and the `ar` locale — **re-check after every
    upstream sync**: `grep -rn "Hermes Desktop" apps/desktop/src apps/desktop/electron`
    and rebrand source + any test that pins those strings, in lockstep):
    `apps/desktop/src/i18n/{en,ja,zh,zh-hant,ar}.ts`,
    `src/components/desktop-install-overlay.test.tsx`,
    `src/store/onboarding.ts`, `src/lib/desktop-{git,fs}.ts`,
    `electron/{remote-lifecycle,hardening}.ts`, and the pinned string in
    `src/i18n/runtime.test.ts`. On upstream-sync conflicts, keep upstream's
    sentence and re-apply the product name.
  - `apps/desktop/index.html` (`<title>NousAI — Hermes</title>` — must keep
    the word `Hermes`: `e2e/boot.spec.ts` asserts it)
  - `apps/desktop/src/themes/presets.ts` (`nousaiTheme`, BUILTIN_THEMES entry,
    `DEFAULT_SKIN_NAME = 'nousai'`)
  - `web/src/themes/presets.ts` (`nousaiTheme` + BUILTIN_THEMES entry)
  - `hermes_cli/web_server.py` (one `nousai` row in `_BUILTIN_DASHBOARD_THEMES`)
- Deliberately NOT forked: `ui-tui/` default theme/content (runtime skin
  already themes the TUI; upstream tests hardcode the Hermes brand there).
- If the `check-attribution` CI job flags unmapped upstream author emails, map
  them with `python3 scripts/add_contributor.py <email> <github-login>` —
  verify the login from the commit's linked author via the GitHub API, don't
  guess.

## NousAI branding (Phase 1 + 2 — done)

`nousai-branding/` holds the config-only rebrand: skin (`skins/nousai.yaml`),
persona (`SOUL.md`), installers for Linux/macOS and Windows. Runtime files
install into the Hermes home directory, not the repo. See its README.

Phase 2 (brand pack) lives in the carve-out files listed above: NousAI
banner/icons, desktop app identity, and first-class `nousai` desktop/web
theme presets (desktop default; web default set via `dashboard.theme` in
config, which the installers write).

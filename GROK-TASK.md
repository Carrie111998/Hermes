You are producing an UPSTREAM-QUALITY fix for NousResearch/hermes-agent (desktop app pane shell). Work entirely inside THIS worktree (branch fix/pane-tab-restore). The live install at ~/.hermes/hermes-agent must not be touched.

BUGS TO FIX (one PR, one theme: "a collapsed pane must always leave a visible way back"):
1. Issue #91223 (see ISSUE-91223.md): double-clicking the Sessions/Bots sidebar tab hides the tab strip with no way to restore it.
2. Same-family repro found live today on v0.20.5 stock: clicking the ACTIVE tab of a docked tool tile (e.g. Bot Mode's CRONJOBS/Routines tile, or any plugin pane docked beside the workspace) collapses the pane AND its tab label vanishes with it — no mouse path to restore. Recovery currently requires the command palette.

REFERENCE: PR-65867-reference.diff is a stalled July PR ("keep tab bar visible when toggling bottom panel panes") — evaluate whether its approach generalizes; your PR may supersede or complement it. Read the pane-shell source first: apps/desktop/src/components/pane-shell/tree/ (store.ts togglePaneVisible/closeToolPane/$hiddenTreePanes, renderer/, tab-selection.ts, and the intent captured in hide-only-strip-tabs.test.ts and tool-pane-toggle.test.ts).

REQUIREMENTS:
- Root-cause in FINDINGS-UPSTREAM.md (file:line).
- Minimal, style-matching fix: collapsing keeps the strip/label visible (per the repo's own hide-only-strip-tabs intent) or an equivalent always-visible restore affordance. No new dependencies. No drive-by refactors.
- Tests: extend/add vitest specs beside the existing pane-shell tests covering both repros. Run the pane-shell test files (npx vitest run src/components/pane-shell --root apps/desktop or the repo's documented test invocation — discover it from package.json) and make them pass; record the exact command + output tail in TEST-RUN.md.
- Commit the changes on this branch with a conventional-commit message referencing #91223.
- Write PR-DESCRIPTION.md: problem, root cause, fix, tests, relation to PR #65867, repro steps.

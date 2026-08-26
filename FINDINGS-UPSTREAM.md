# Findings — collapsed pane must leave a visible way back (#91223)

Worktree: `fix/pane-tab-restore` (this checkout). Live install at
`~/.hermes/hermes-agent` was not touched.

## Invariant

A collapsed or chrome-hidden zone must still expose a mouse-reachable restore
handle (its tab chip / strip). The repo already states this in two places:

- `hide-only-strip-tabs.test.ts` — the last visible Sessions/Bots tab cannot be
  hidden, or the zone becomes a dead strip.
- `strip-visibility.ts` — `"Hide the strip" is a request about chrome, never a
  request to make a surface unreachable.`

Both reported bugs violate that invariant.

## Repro 1 — #91223 (Sessions/Bots double-click)

**Symptom.** Double-clicking the Sessions (or Bots) tab in the left sidebar
header drops the tab strip to zero height. Sessions, Bots, and any session-title
chips disappear. The zone-menu row "Show header" / "Show tab strip" is mounted
on the strip it just removed (`tree-group.tsx:187-199`), so there is nothing
left to right-click. Recovery is ⌘K / the command palette (or ⌘T, which happens
to reset chrome).

**Original cited path (v0.20.4, `8794e5a`).** The issue names
`hideHeaderDoubleTap` → `setTreeGroupHeaderHidden` and
`drag-session.ts` `DOUBLE_TAP_MS = 400`. That gesture is already gone on this
tree: `headerHidden` was retired (`model.ts` `migratePersistedTree`), and
`tab-strip-hide.test.tsx` pins "hiding is a COMMAND now, not a gesture."

**Hole that remained.** The command/menu can still write `tabStrip: 'never'`,
and the resolver did not treat hide-only chrome as stranded:

```
apps/desktop/src/components/pane-shell/tree/renderer/strip-visibility.ts
  stranded()  (pre-fix)
    - closeable `placement: 'main'` tile → force strip
    - lone collapsePane (terminal/logs) → force strip
    - hide-only `placement: 'left'` (sessions / hermes-bots:pane) → NOT stranded
```

So `resolveTabStripVisible({ mode: 'never', shown: [sessions, bots] })` was
`false`. `TreeGroup` then computed `headerVisible = false` and unmounted the
strip, the chips, and the only menu that could bring them back.

The same hole is why a double-click that *looks* like the old hide (or a
persisted `never` from an older build) still traps the user: the resolver
honors `never` for standing chrome.

**Related accidental collapse.** `TreeGroup`'s strip `onPointerDown` called
`startPaneDrag(..., () => minimizable && toggleCollapse())`
(`tree-group.tsx`, pre-fix). The Sessions/Bots zone is minimizable (no
uncloseable workspace in it). In a lone-tab zone the chip sits *in* that
header; a tap on the gutter — visually "the tab bar" — folded the zone.

## Repro 2 — docked tool tile (Cronjobs / any plugin pane beside the workspace)

**Symptom (v0.20.5 stock).** Clicking the *active* tab of a docked tool tile
(Bot Mode Cronjobs, or any plugin pane docked on the workspace's right edge)
collapses the pane. The tab label vanishes with the body. Restore is the
command palette.

**Path.**

1. Cronjobs registers as `placement: 'main'` with
   `dock: { pane: 'workspace', pos: 'right', enforce: true }`
   (`apps/desktop/src/plugins/hermes-bots/plugin.js` around the
   `registerRoutinesPane` block). It lives in its own group, sibling of
   workspace in a **row** split.

2. A lone closeable main tile *is* stranded, so the strip paints one chip.
   The chip does not fill the bar (`PaneTab` `max-w-48`). The rest of the
   header is the strip's `onPointerDown`.

3. Pre-fix, that handler's `onTap` was `toggleCollapse` → `collapseTreePane`
   (`tree-group.tsx` ~401 / ~499). The pane is not a collapse-pane (those are
   terminal/logs), so the store just `setTreeGroupMinimized(group.id, true)`
   (`store.ts` `collapseTreePane` / `setPaneCollapsed`).

4. `parentAxis === 'row'` + minimized ⇒ `verticalCollapse` replaced the
   horizontal strip with a `h-full w-7` rail (`tree-group.tsx` ~347-349).
   The split wrapper sized that child with `flex: 0 0 auto`
   (`tree-split.tsx` ~598) instead of the track model's `MINIMIZED_TRACK`
   (`1.75rem`). The rail's height is `h-full` of an auto-sized parent — a
   circular measure that can collapse to 0px. The chip is gone; there is no
   mouse path back.

Clicking the chip itself is supposed to only `activateTreePane` (the comment
at the tab `onTap` already said overloading it was a lottery). The live repro
is the header tap sitting around the chip, which *did* collapse.

## Why PR #65867 does not fully cover this

`PR-65867-reference.diff` ("keep tab bar visible when toggling bottom panel
panes") does two things:

1. `verticalCollapse && shown.length <= 1` — keep the **horizontal** strip
   when a minimized row zone still has two or more chips (terminal + logs).
2. `setPaneCollapsed`: if a sibling tool pane's store is still open, activate
   it instead of folding the whole zone (`registerPaneOpenGetter`).

(1) is the right renderer exception and is taken here. It does **not** help a
**lone** docked tile (`shown.length === 1`), which is the Cronjobs shape.

(2) needs `paneOpenGetters` and would leave a tool pane's `$open === false`
while its tab stays in an un-minimized strip. Tab `onTap` only calls
`restoreTreePane` (the opener) when the *zone* is minimized — see
`tool-pane-toggle.test.ts` "clickable, mountable terminal tab". Adopting the
sibling-switch without also changing tab activation would resurrect that
empty-tab bug. Not taken.

## Fix (this PR)

| Hole | Change |
|---|---|
| Hide-only chrome not stranded | `stranded()` treats `hideOnly` like a closeable tile (`strip-visibility.ts:59-61`). `never` cannot hide Sessions/Bots. |
| Header tap collapses a lone tile | Strip `onTap` restores when already minimized, and is otherwise a no-op. Collapse is the chevron only (`tree-group.tsx` strip `onPointerDown`). |
| Multi-tab row minimize drops the bar | `verticalCollapse` requires `shown.length <= 1` (PR #65867 renderer exception). |
| 0-size rail | Minimized split children use `flex: 0 0 ${MINIMIZED_TRACK}` (`tree-split.tsx:598`). Rail also gets `min-h-7 min-w-7`. |

Chevron collapse of a row-docked lone tile still uses the vertical rail, but
the rail is forced to 1.75rem on the main axis and still renders the tab chip
as the restore handle.

## Tests

- `strip-visibility.test.ts` — hide-only + `never` stays visible.
- `hide-only-strip-tabs.test.ts` — Sessions/Bots zone with `tabStrip: 'never'`
  still reports a visible strip.
- `renderer/collapse-restore-affordance.test.tsx` — both live repros (double
  tap Sessions/Bots, click active Cronjobs tab / strip gutter) plus chevron
  collapse keeping the chip, plus a stacked terminal+logs row keeping both
  labels.

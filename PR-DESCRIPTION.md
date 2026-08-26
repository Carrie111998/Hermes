# Keep a restore tab when a pane or strip collapses

Fixes #91223.

## Problem

Two desktop pane-shell gestures leave a collapsed surface with **no mouse path
back**. Recovery is the command palette.

1. **Sessions/Bots (#91223).** Double-clicking the Sessions or Bots tab in the
   left sidebar header hides the tab strip. Sessions, Bots, and any
   session-title chips disappear. "Show tab strip" lives on that strip, so
   once it is gone there is nothing to right-click.
2. **Docked tool tile (same family, v0.20.5 stock).** Clicking the **active**
   tab of a pane docked beside the workspace — Bot Mode's Cronjobs tile, or
   any plugin pane in that slot — collapses the pane. The tab label vanishes
   with it.

## Root cause

The shell already has the right invariant (`strip-visibility.ts`: hiding the
strip must never make a surface unreachable; `hide-only-strip-tabs.test.ts`:
the last Sessions/Bots chip cannot be hidden). Two holes still violated it.

- **Hide-only chrome was not stranded.** `stranded()` forced the strip for a
  closeable main tile and a lone tool panel, but not for `hideOnly` Sessions /
  Bots. `tabStrip: 'never'` (the old double-tap hide, the zone menu, a
  persisted layout) unmounted the strip, the chips, and the only menu that
  could restore them.
- **The strip header's tap collapsed the zone.** `startPaneDrag`'s `onTap` on
  the header called `toggleCollapse`. In a lone-tab zone the chip sits in that
  header and does not fill it. A click on the active Cronjobs (or Sessions)
  bar folded the group. For a row-docked tile, `verticalCollapse` swapped the
  horizontal strip for a `h-full` rail inside a `flex: 0 0 auto` wrapper —
  height can circularly collapse to 0, so the restore chip is gone.

The original `hideHeaderDoubleTap` / `DOUBLE_TAP_MS` path named in #91223 is
already gone on this tree (`headerHidden` was retired; hiding is a named
command). The holes above are why the trap still reproduces.

## Fix

- Treat `hideOnly` panes as stranded in `resolveTabStripVisible`. `never`
  cannot hide the Sessions/Bots strip.
- Header tap no longer collapses. The chevron is the collapse affordance; a
  minimized strip still restores on tap (it *is* the handle).
- Minimized row zones with ≥2 chips keep the horizontal tab bar (so you can
  switch/restore without expanding first). A lone row-collapsed tile still
  uses the vertical rail, but the rail is sized to `MINIMIZED_TRACK` (`1.75rem`)
  and still renders the tab chip.

## Tests

- `strip-visibility.test.ts` — hide-only + `never` stays visible.
- `hide-only-strip-tabs.test.ts` — Sessions/Bots zone with `tabStrip: 'never'`
  still reports a visible strip.
- `renderer/collapse-restore-affordance.test.tsx` — both repros, chevron
  collapse keeping the Cronjobs chip, stacked terminal+logs keeping both
  labels.

```bash
npx vitest run src/components/pane-shell --root apps/desktop
# 31 files, 173 tests, all passed
```

## Relation to PR #65867

#65867 ("keep tab bar visible when toggling bottom panel panes") is the same
theme and this change **takes its renderer exception** (`verticalCollapse`
only when `shown.length <= 1`).

It does **not** take the `registerPaneOpenGetter` / "switch to an open sibling
instead of folding" store change. That path leaves a tool pane's `$open`
false while its tab stays in an un-minimized strip; tab activation only calls
the opener when the zone is minimized
(`tool-pane-toggle.test.ts`, "clickable, mountable terminal tab"). Folding
the zone as a unit remains the truthful collapse for a shared tool stack.

#65867 also does not cover a **lone** docked tile (`shown.length === 1`) or
hide-only Sessions/Bots. This PR does; it supersedes #65867 for the
restore-affordance theme.

## Repro steps (fixed)

**Sessions/Bots**

1. Open the desktop app. Left sidebar shows Sessions | Bots.
2. Double-click the Sessions tab (or Bots).
3. Expected: the strip stays; both chips stay; sidebar content does not jump
   flush to the top. (Chevron still minimizes; the chips remain on the rail /
   strip.)

**Docked tool tile**

1. Enable Bot Mode, open a bot chat so Cronjobs docks to the right of the
   workspace (or dock any plugin pane there).
2. Click the active Cronjobs tab / the empty part of that tab bar.
3. Expected: the tile stays expanded; the Cronjobs label stays.
4. Click the minimize chevron. Expected: the tile collapses to a 1.75rem rail
   (or a horizontal strip if it has siblings) **with the Cronjobs chip still
   visible**; click the chip to restore.

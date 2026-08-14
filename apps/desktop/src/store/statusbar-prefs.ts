import { Codecs, persistentAtom } from '@/lib/persisted'
import { readJson, readKey, writeJson, writeKey } from '@/lib/storage'

const STATUSBAR_HIDDEN_STORAGE_KEY = 'hermes.desktop.statusbarHidden'
const STATUSBAR_VISIBLE_STORAGE_KEY = 'hermes.desktop.statusbarVisible'

// Whole-bar visibility, VS Code's `workbench.statusBar.visible`. On by default.
// Hiding it unmounts the bar (its 15s status poll goes with it), so the way back
// is the `view.toggleStatusbar` keybind or the ⌘K row, never the bar itself.
export const $statusbarVisible = persistentAtom(STATUSBAR_VISIBLE_STORAGE_KEY, true, Codecs.bool)

export function toggleStatusbarVisible() {
  $statusbarVisible.set(!$statusbarVisible.get())
}

// Items the bar hides until the user turns them on from its context menu. The
// bar's job is to answer "is the backend healthy, where am I, what's it doing"
// — route shortcuts (cron/webhooks/agents), the terminal toggle, and the
// approval pill are navigation, not status, so they start out of the way. The
// per-turn timers are diagnostics most users don't watch, so they start hidden
// too. Context/account usage stays visible by default: it carries provider plan
// limits (Codex/Kimi/…), not only the session token meter.
export const STATUSBAR_HIDDEN_BY_DEFAULT: readonly string[] = [
  'agents',
  'approval-mode',
  'cron',
  'running-timer',
  'session-timer',
  'terminal',
  'webhooks'
]

// Existing installs already persisted the old default set that included
// context-usage. Drop that id once so coding-plan limits stay reachable — but
// ONLY when the persisted set still exactly matches that legacy default. A
// persisted set is indistinguishable per-entry (we cannot tell "hidden by old
// default" from "hidden deliberately"), so a customized set is treated as
// intentional and left untouched; the exact-match case is the one where the
// user almost certainly never edited the prefs at all. Residual risk: a user
// who explicitly reproduced the default set loses one explicit hide once.
const STATUSBAR_HIDDEN_MIGRATION_KEY = 'hermes.desktop.statusbarHidden.migrate.v2-show-context-usage'
const STATUSBAR_HIDDEN_LEGACY_DEFAULT: readonly string[] = [
  'agents',
  'approval-mode',
  'context-usage',
  'cron',
  'running-timer',
  'session-timer',
  'terminal',
  'webhooks'
]
export function migrateStatusbarHiddenLegacyDefault(): void {
  if (readKey(STATUSBAR_HIDDEN_MIGRATION_KEY) === '1') {
    return
  }
  const parsed = readJson<unknown>(STATUSBAR_HIDDEN_STORAGE_KEY)
  if (Array.isArray(parsed)) {
    const ids = parsed.filter((id): id is string => typeof id === 'string')
    const isUntouchedLegacyDefault =
      ids.length === STATUSBAR_HIDDEN_LEGACY_DEFAULT.length &&
      ids.every(id => STATUSBAR_HIDDEN_LEGACY_DEFAULT.includes(id))
    if (isUntouchedLegacyDefault) {
      writeJson(
        STATUSBAR_HIDDEN_STORAGE_KEY,
        ids.filter(id => id !== 'context-usage')
      )
    }
  }
  writeKey(STATUSBAR_HIDDEN_MIGRATION_KEY, '1')
}
migrateStatusbarHiddenLegacyDefault()

// Stored as the explicit hidden set (not the visible one) so an item added to
// the bar in a later version shows up for existing users instead of silently
// staying off. An empty array is a real value — the user turned everything on —
// so this uses a sanitizing json codec rather than Codecs.stringArray, which
// drops the key when empty and would resurrect the defaults on next launch.
export const $statusbarHiddenIds = persistentAtom<string[]>(
  STATUSBAR_HIDDEN_STORAGE_KEY,
  [...STATUSBAR_HIDDEN_BY_DEFAULT],
  Codecs.json<string[]>(value =>
    Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string' && id.length > 0) : []
  )
)

export function setStatusbarItemVisible(id: string, visible: boolean) {
  const hidden = $statusbarHiddenIds.get()

  if (visible === !hidden.includes(id)) {
    return
  }

  $statusbarHiddenIds.set(visible ? hidden.filter(entry => entry !== id) : [...hidden, id])
}

/** Pure so the menu can derive its reset row's disabled state from the hidden
 *  list it already subscribes to, rather than reading the atom out of band.
 *  Set-compared: order is incidental (items are appended as they're hidden) and
 *  a duplicated id shouldn't read as a customization. */
export function isStatusbarLayoutDefault(hidden: readonly string[]) {
  const ids = new Set(hidden)

  return ids.size === STATUSBAR_HIDDEN_BY_DEFAULT.length && STATUSBAR_HIDDEN_BY_DEFAULT.every(id => ids.has(id))
}

/** Put the show/hide set back to what ships. Only touches item layout — whole-bar
 *  visibility is a separate preference, and resetting from the bar's own menu
 *  shouldn't make the bar the user is right-clicking disappear. */
export function resetStatusbarLayout() {
  $statusbarHiddenIds.set([...STATUSBAR_HIDDEN_BY_DEFAULT])
}

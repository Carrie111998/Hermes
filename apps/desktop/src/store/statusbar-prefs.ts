import { Codecs, persistentAtom } from '@/lib/persisted'

const STATUSBAR_HIDDEN_STORAGE_KEY = 'hermes.desktop.statusbarHidden'
const STATUSBAR_VISIBLE_STORAGE_KEY = 'hermes.desktop.statusbarVisible'

// Run a one-time LocalStorage migration for upgraded users to reset statusbar settings to visible and unhidden.
if (typeof window !== 'undefined') {
  try {
    const isHiddenKey = localStorage.getItem(STATUSBAR_VISIBLE_STORAGE_KEY);
    if (isHiddenKey === 'false' || isHiddenKey === null) {
      localStorage.setItem(STATUSBAR_VISIBLE_STORAGE_KEY, 'true');
    }
    const hiddenVal = localStorage.getItem(STATUSBAR_HIDDEN_STORAGE_KEY);
    if (hiddenVal) {
      const parsed = JSON.parse(hiddenVal);
      if (Array.isArray(parsed) && parsed.includes('agents')) {
        localStorage.removeItem(STATUSBAR_HIDDEN_STORAGE_KEY);
      }
    }
  } catch (e) {
    // Silent fail in environments where localStorage is blocked
  }
}

export const $statusbarVisible = persistentAtom(STATUSBAR_VISIBLE_STORAGE_KEY, true, Codecs.bool)

export function toggleStatusbarVisible() {
  $statusbarVisible.set(!$statusbarVisible.get())
}

// Items the bar hides until the user turns them on from its context menu.
export const STATUSBAR_HIDDEN_BY_DEFAULT: readonly string[] = []

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

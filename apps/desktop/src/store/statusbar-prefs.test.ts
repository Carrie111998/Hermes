import { beforeEach, describe, expect, it, vi } from 'vitest'

// One-time migration coverage for `statusbar-prefs`: the legacy default hidden
// set included `context-usage`; the migration strips it ONLY when the persisted
// set still exactly matches that legacy default (a customized set may hide the
// item deliberately and must survive).
const HIDDEN_KEY = 'hermes.desktop.statusbarHidden'
const MIGRATION_KEY = 'hermes.desktop.statusbarHidden.migrate.v2-show-context-usage'
const LEGACY_DEFAULT = [
  'agents',
  'approval-mode',
  'context-usage',
  'cron',
  'running-timer',
  'session-timer',
  'terminal',
  'webhooks'
]

async function importPrefs() {
  return import('./statusbar-prefs')
}

function hiddenIds(): unknown {
  const raw = window.localStorage.getItem(HIDDEN_KEY)
  return raw === null ? null : JSON.parse(raw)
}

describe('statusbar hidden-set migration (v2-show-context-usage)', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('strips context-usage from an untouched legacy default set', async () => {
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify([...LEGACY_DEFAULT].reverse()))
    await importPrefs()
    expect(hiddenIds()).toEqual(LEGACY_DEFAULT.filter(id => id !== 'context-usage').reverse())
    expect(window.localStorage.getItem(MIGRATION_KEY)).toBe('1')
  })

  it('leaves a customized set containing context-usage untouched', async () => {
    // User removed 'cron' from the default: the set no longer matches the
    // legacy default, so an explicit hide of context-usage is plausible.
    const customized = LEGACY_DEFAULT.filter(id => id !== 'cron')
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify(customized))
    await importPrefs()
    expect(hiddenIds()).toEqual(customized)
    expect(window.localStorage.getItem(MIGRATION_KEY)).toBe('1')
  })

  it('leaves a set with extra user-added ids untouched', async () => {
    const extended = [...LEGACY_DEFAULT, 'gateway-health']
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify(extended))
    await importPrefs()
    expect(hiddenIds()).toEqual(extended)
  })

  it('does not strip a deliberate re-hide after the migration ran', async () => {
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify(LEGACY_DEFAULT))
    await importPrefs()
    expect(hiddenIds()).toEqual(LEGACY_DEFAULT.filter(id => id !== 'context-usage'))

    // User hides the item again, then the module is re-imported (reload).
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify(['context-usage']))
    vi.resetModules()
    await importPrefs()
    expect(hiddenIds()).toEqual(['context-usage'])
  })

  it('marks completion on a fresh install; the atom then persists the new default', async () => {
    await importPrefs()
    // The migration itself writes nothing; the persistentAtom materializes the
    // new default set (without context-usage) on first use.
    expect(hiddenIds()).toEqual(LEGACY_DEFAULT.filter(id => id !== 'context-usage'))
    expect(window.localStorage.getItem(MIGRATION_KEY)).toBe('1')
  })

  it('survives corrupted stored JSON without throwing', async () => {
    window.localStorage.setItem(HIDDEN_KEY, '{not json')
    await expect(importPrefs()).resolves.toBeTruthy()
    expect(window.localStorage.getItem(MIGRATION_KEY)).toBe('1')
    // The atom's sanitizing codec replaces the corrupted value with the new
    // default set (without context-usage).
    expect(hiddenIds()).toEqual(LEGACY_DEFAULT.filter(id => id !== 'context-usage'))
  })
})

import { atom } from 'nanostores'

import type { WidgetApp } from './types.js'

const apps = new Map<string, WidgetApp<never>>()
const revisions = new Map<string, number>()
export const $widgetRegistryRevision = atom(0)

const bumpRevision = (id: string): void => {
  revisions.set(id, (revisions.get(id) ?? 0) + 1)
  $widgetRegistryRevision.set($widgetRegistryRevision.get() + 1)
}

/** Identity helper that pins the state type, then registers. Last writer
 *  wins so a user/plugin app can shadow a built-in of the same id. */
export function defineWidgetApp<S>(app: WidgetApp<S>): WidgetApp<S> {
  apps.set(app.id, app as WidgetApp<never>)
  bumpRevision(app.id)

  return app
}

export const getWidgetApp = (id: string): undefined | WidgetApp<never> => apps.get(id)

/** Monotonic implementation revision. Same-id hot reloads must remount their
 * React render fiber because the new app may use a different Hook layout. */
export const getWidgetAppRevision = (id: string): number => revisions.get(id) ?? 0

/** Unregister (user-widget file deleted). Built-ins never call this. */
export const removeWidgetApp = (id: string): boolean => {
  const removed = apps.delete(id)

  if (removed) {
    bumpRevision(id)
  }

  return removed
}

/** All registered apps, id-sorted — the registry IS the catalog: slash
 *  commands and `/` completions derive from it, nothing is hardcoded. */
export const listWidgetApps = (): WidgetApp<never>[] => [...apps.values()].sort((a, b) => a.id.localeCompare(b.id))

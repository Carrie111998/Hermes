import type { ModelSelection } from '@/types/model-selection'

import type { WiringActions } from './types'

/**
 * Narrow module-level bridge so the plugin SDK can invoke controller-owned
 * actions without reaching into React internals. Same shape as
 * `sessionTileDelegate` in store/session-states.ts: the wiring publishes the
 * live action (stable identity, latest closure) and the SDK reads it on
 * demand. Null until the controller mounts; a plugin calling early gets a
 * clean `false` instead of a crash.
 */
let selectModelAction: WiringActions['selectModel'] | null = null

/** Controller-side: hand the live `selectModel` action to the plugin bridge. */
export function publishPluginActions(next: Pick<WiringActions, 'selectModel'> | null): void {
  selectModelAction = next?.selectModel ?? null
}

/** SDK-side: invoke the controller's model switch if one is attached. */
export function runPluginSelectModel(selection: ModelSelection): Promise<boolean> {
  const action = selectModelAction

  if (!action) {
    return Promise.resolve(false)
  }

  // One promise boundary for sync/async/void handlers: a synchronous throw
  // becomes a rejection, a `void` handler means accepted, and only an
  // explicit `false` is a failure.
  return Promise.resolve()
    .then(() => action(selection) ?? true)
    .then(result => result !== false)
}

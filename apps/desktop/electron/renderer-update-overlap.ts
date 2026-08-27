import {
  type UpdateGateDeps,
  waitForUpdateClearance,
  type WaitForUpdateClearanceOptions
} from './update-gate'

type RendererBootAction = 'continue' | 'relaunch' | 'abort'

interface ExternalUpdateRendererState {
  alreadyQuiescing: boolean
  isQuittingForHandoff: boolean
  markerLive: boolean
  updateInFlight: boolean
}

/**
 * A renderer may load packaged files only from one stable bundle generation.
 * If this process had to wait for an updater, relaunch it after clearance so
 * Chromium cannot retain the old module graph / ASAR view. A live marker that
 * outlasts the bounded wait fails closed instead of loading mutable files.
 */
async function guardRendererBootAcrossUpdate(
  deps: UpdateGateDeps,
  options: WaitForUpdateClearanceOptions
): Promise<RendererBootAction> {
  const outcome = await waitForUpdateClearance(deps, options)

  if (outcome === 'clear') {
    return 'continue'
  }

  if (outcome === 'finished') {
    return 'relaunch'
  }

  return 'abort'
}

/**
 * An update started outside this Desktop process (for example `hermes update`
 * in Terminal) must quiesce its already-live renderer before the bundle swap.
 * The in-app handoff owns its own orderly quit/relaunch and must not race this
 * monitor.
 */
function shouldQuiesceForExternalUpdate(state: ExternalUpdateRendererState): boolean {
  return state.markerLive && !state.updateInFlight && !state.isQuittingForHandoff && !state.alreadyQuiescing
}

export {
  type ExternalUpdateRendererState,
  guardRendererBootAcrossUpdate,
  type RendererBootAction,
  shouldQuiesceForExternalUpdate
}

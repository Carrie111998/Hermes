import { atom, computed } from 'nanostores'

import type { ApprovalReq } from '../types.js'

import type { OverlayState } from './interfaces.js'

const buildOverlayState = (): OverlayState => ({
  agents: false,
  agentsInitialHistoryIndex: 0,
  approvals: [],
  billing: null,
  clarify: null,
  confirm: null,
  ambient: [],
  widget: null,
  journey: false,
  modelPicker: false,
  pager: null,
  petPicker: false,
  pluginsHub: false,
  secret: null,
  sessions: false,
  skillsHub: false,
  subscription: null,
  sudo: null
})

export const $overlayState = atom<OverlayState>(buildOverlayState())

export const $isBlocked = computed(
  $overlayState,
  ({
    agents,
    approvals,
    billing,
    clarify,
    confirm,
    journey,
    modelPicker,
    pager,
    petPicker,
    pluginsHub,
    secret,
    sessions,
    skillsHub,
    subscription,
    sudo,
    widget
  }) =>
    Boolean(
      agents ||
      approvals.length > 0 ||
      billing ||
      clarify ||
      confirm ||
      journey ||
      modelPicker ||
      pager ||
      petPicker ||
      pluginsHub ||
      secret ||
      sessions ||
      skillsHub ||
      subscription ||
      sudo ||
      widget
    )
)

export const getOverlayState = () => $overlayState.get()

export const patchOverlayState = (next: Partial<OverlayState> | ((state: OverlayState) => OverlayState)) =>
  $overlayState.set(typeof next === 'function' ? next($overlayState.get()) : { ...$overlayState.get(), ...next })

/** Full reset — used by session/turn teardown and tests. */
export const resetOverlayState = () => $overlayState.set(buildOverlayState())

/**
 * Soft reset: drop FLOW-scoped overlays (approval / clarify / confirm / sudo
 * / secret / pager) but PRESERVE user-toggled ones — agents dashboard, model
 * picker, skills hub, sessions overlay.  Those are opened deliberately and
 * shouldn't vanish when a turn ends.  Called from turnController.idle() on
 * every turn completion / interrupt; the old "reset everything" behaviour
 * silently closed /agents the moment delegation finished.
 */
export const resetFlowOverlays = () =>
  $overlayState.set({
    ...buildOverlayState(),
    agents: $overlayState.get().agents,
    agentsInitialHistoryIndex: $overlayState.get().agentsInitialHistoryIndex,
    ambient: $overlayState.get().ambient,
    widget: $overlayState.get().widget,
    journey: $overlayState.get().journey,
    modelPicker: $overlayState.get().modelPicker,
    petPicker: $overlayState.get().petPicker,
    pluginsHub: $overlayState.get().pluginsHub,
    sessions: $overlayState.get().sessions,
    skillsHub: $overlayState.get().skillsHub
  })

export const enqueueApproval = (request: ApprovalReq) =>
  patchOverlayState(state => {
    const existingIndex = state.approvals.findIndex(candidate => candidate.approvalId === request.approvalId)

    if (existingIndex === -1) {
      return { ...state, approvals: [...state.approvals, request] }
    }

    const approvals = [...state.approvals]
    approvals[existingIndex] = request

    return { ...state, approvals }
  })

export const clearApproval = (approvalId: string): boolean => {
  const state = getOverlayState()
  const approvals = state.approvals.filter(request => request.approvalId !== approvalId)

  if (approvals.length === state.approvals.length) {
    return false
  }

  patchOverlayState({ approvals })

  return true
}

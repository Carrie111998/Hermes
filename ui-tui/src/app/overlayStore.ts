import { atom, computed } from 'nanostores'

import type { ApprovalReq } from '../types.js'

import type { OverlayState } from './interfaces.js'

const buildOverlayState = (): OverlayState => ({
  agents: false,
  agentsInitialHistoryIndex: 0,
  approval: null,
  approvalQueue: [],
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
    approval,
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
      approval ||
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

const resolvedApprovalIds = new Set<string>()
const MAX_RESOLVED_APPROVAL_IDS = 1000

const rememberResolvedApprovalId = (approvalId?: string) => {
  if (!approvalId) {
    return
  }

  resolvedApprovalIds.delete(approvalId)
  resolvedApprovalIds.add(approvalId)

  while (resolvedApprovalIds.size > MAX_RESOLVED_APPROVAL_IDS) {
    const oldest = resolvedApprovalIds.values().next().value

    if (typeof oldest !== 'string') {
      break
    }

    resolvedApprovalIds.delete(oldest)
  }
}

export const enqueueApprovalRequest = (request: ApprovalReq) =>
  patchOverlayState(state => {
    if (request.approvalId && resolvedApprovalIds.has(request.approvalId)) {
      return state
    }

    if (!request.approvalId) {
      return { ...state, approval: request, approvalQueue: [] }
    }

    const pending = state.approval ? [state.approval, ...state.approvalQueue] : state.approvalQueue
    const index = pending.findIndex(item => item.approvalId === request.approvalId)

    const next =
      index >= 0
        ? pending.map((item, itemIndex) => (itemIndex === index ? request : item))
        : [...pending, request]

    return {
      ...state,
      approval: next[0] ?? null,
      approvalQueue: next.slice(1)
    }
  })

export const completeApprovalRequest = (approvalId?: string) => {
  rememberResolvedApprovalId(approvalId)
  patchOverlayState(state => {
    if (!state.approval) {
      return state
    }

    const pending = [state.approval, ...state.approvalQueue]

    const remaining = approvalId
      ? pending.filter(item => item.approvalId !== approvalId)
      : pending.slice(1)

    if (remaining.length === pending.length) {
      return state
    }

    return {
      ...state,
      approval: remaining[0] ?? null,
      approvalQueue: remaining.slice(1)
    }
  })
}

/** Full reset — used by session/turn teardown and tests. */
export const resetOverlayState = () => {
  resolvedApprovalIds.clear()
  $overlayState.set(buildOverlayState())
}

/**
 * Soft reset: drop FLOW-scoped overlays (approval / clarify / confirm / sudo
 * / secret / pager) but PRESERVE user-toggled ones — agents dashboard, model
 * picker, skills hub, sessions overlay.  Those are opened deliberately and
 * shouldn't vanish when a turn ends.  Called from turnController.idle() on
 * every turn completion / interrupt; the old "reset everything" behaviour
 * silently closed /agents the moment delegation finished.
 */
export const resetFlowOverlays = () => {
  const current = $overlayState.get()

  for (const request of current.approval ? [current.approval, ...current.approvalQueue] : current.approvalQueue) {
    rememberResolvedApprovalId(request.approvalId)
  }

  $overlayState.set({
    ...buildOverlayState(),
    agents: current.agents,
    agentsInitialHistoryIndex: current.agentsInitialHistoryIndex,
    ambient: current.ambient,
    widget: current.widget,
    journey: current.journey,
    modelPicker: current.modelPicker,
    petPicker: current.petPicker,
    pluginsHub: current.pluginsHub,
    sessions: current.sessions,
    skillsHub: current.skillsHub
  })
}

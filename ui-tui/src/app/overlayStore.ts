import { atom, computed } from 'nanostores'

import type { OverlayState } from './interfaces.js'

const buildOverlayState = (): OverlayState => ({
  agents: false,
  agentsInitialHistoryIndex: 0,
  approval: null,
  billing: null,
  clarify: null,
  commandPalette: null,
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

export const hasModalInputOwner = (state: OverlayState) =>
  Boolean(
    state.approval ||
      state.billing ||
      state.clarify ||
      state.confirm ||
      state.secret ||
      state.subscription ||
      state.sudo ||
      state.widget
  )

const enforceInputOwnership = (state: OverlayState): OverlayState =>
  state.commandPalette && hasModalInputOwner(state) ? { ...state, commandPalette: null } : state

const setOverlayState = (state: OverlayState) => $overlayState.set(enforceInputOwnership(state))

export const $isBlocked = computed(
  $overlayState,
  ({
    agents,
    approval,
    billing,
    clarify,
    commandPalette,
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
      commandPalette ||
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
  setOverlayState(typeof next === 'function' ? next($overlayState.get()) : { ...$overlayState.get(), ...next })

/** Full reset — used by session/turn teardown and tests. */
export const resetOverlayState = () => setOverlayState(buildOverlayState())

/**
 * Soft reset: drop FLOW-scoped overlays (approval / clarify / confirm / sudo
 * / secret / pager) but PRESERVE user-toggled ones — agents dashboard, model
 * picker, skills hub, sessions overlay.  Those are opened deliberately and
 * shouldn't vanish when a turn ends.  Called from turnController.idle() on
 * every turn completion / interrupt; the old "reset everything" behaviour
 * silently closed /agents the moment delegation finished.
 */
export const resetFlowOverlays = () =>
  setOverlayState({
    ...buildOverlayState(),
    agents: $overlayState.get().agents,
    agentsInitialHistoryIndex: $overlayState.get().agentsInitialHistoryIndex,
    ambient: $overlayState.get().ambient,
    commandPalette: $overlayState.get().commandPalette,
    widget: $overlayState.get().widget,
    journey: $overlayState.get().journey,
    modelPicker: $overlayState.get().modelPicker,
    petPicker: $overlayState.get().petPicker,
    pluginsHub: $overlayState.get().pluginsHub,
    sessions: $overlayState.get().sessions,
    skillsHub: $overlayState.get().skillsHub
  })

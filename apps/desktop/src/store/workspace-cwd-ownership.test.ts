import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $selectedStoredSessionId,
  $workspaceCwdIsOwned,
  commitWorkspaceCwdForSelectedSession,
  releaseWorkspaceCwdOwner,
  setCurrentCwdTransient,
  setWorkspaceCwdOwner
} from '@/store/session'

// `$workspaceCwdIsOwned` is the single gate behind three surfaces that used to
// answer "is there a workspace" separately: whether the Files/Review panes MOUNT
// (contrib/controller.tsx), whether the Files pane renders a tree or the
// "No project open" hint (right-sidebar/index.tsx), and whether the statusbar
// may label the singleton path (shell/hooks/use-statusbar-items.tsx).
describe('$workspaceCwdIsOwned', () => {
  beforeEach(() => {
    setCurrentCwdTransient('')
    $selectedStoredSessionId.set(null)
    setWorkspaceCwdOwner(null)
  })

  afterEach(() => {
    setCurrentCwdTransient('')
    $selectedStoredSessionId.set(null)
    setWorkspaceCwdOwner(null)
  })

  // The regression. The mount gate asked `Boolean(cwd.trim())` while the pane
  // body asked ownership, so a path the selected conversation does not own
  // mounted the pane AND made it render "No project open" inside itself — a
  // visible, empty Files pane next to a statusbar labelling the same path by
  // project name.
  it('is false while the singleton path belongs to another conversation', () => {
    setCurrentCwdTransient('/previous-project')
    $selectedStoredSessionId.set('sess-switching')
    releaseWorkspaceCwdOwner()

    expect($workspaceCwdIsOwned.get()).toBe(false)
  })

  it('is true once the selected conversation owns the path', () => {
    $selectedStoredSessionId.set('sess-settled')
    commitWorkspaceCwdForSelectedSession('/owned-project')

    expect($workspaceCwdIsOwned.get()).toBe(true)
  })

  // A fresh draft's selection is null and so is its owner — they match, which is
  // exactly why `releaseWorkspaceCwdOwner` uses a sentinel instead of null
  // (ae6eb578bb): releasing to null would hand a leftover path to the draft.
  it('is true for a fresh draft, whose null owner matches its null selection', () => {
    setCurrentCwdTransient('/draft-target')
    $selectedStoredSessionId.set(null)
    setWorkspaceCwdOwner(null)

    expect($workspaceCwdIsOwned.get()).toBe(true)
  })

  it('is false for a detached chat with no path at all', () => {
    $selectedStoredSessionId.set('sess-detached')
    setWorkspaceCwdOwner('sess-detached')
    setCurrentCwdTransient('')

    expect($workspaceCwdIsOwned.get()).toBe(false)
  })

  // Reactive: the switch settles a moment after the selection changes, so the
  // panes must come back on their own rather than wait for another user action.
  it('flips back to true when the resume settles and claims the path', () => {
    setCurrentCwdTransient('/previous-project')
    $selectedStoredSessionId.set('sess-new')
    releaseWorkspaceCwdOwner()
    expect($workspaceCwdIsOwned.get()).toBe(false)

    commitWorkspaceCwdForSelectedSession('/new-project')

    expect($workspaceCwdIsOwned.get()).toBe(true)
  })
})

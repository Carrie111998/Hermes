import { afterEach, describe, expect, it } from 'vitest'

import {
  blurComposerInput,
  getActiveComposer,
  markActiveComposer,
  onComposerFocusRequest,
  releaseActiveComposer,
  requestComposerFocus
} from './focus'
import { RICH_INPUT_SLOT } from './rich-editor'

/**
 * Inactive tabs keep their composer mounted, so an unscoped lookup can blur a
 * background input and leave the one the user is typing in focused.
 */

/** A composer input inside its own pane layer, hidden or not. */
function mountInput(hidden = false) {
  const layer = document.createElement('div')
  const input = document.createElement('div')
  input.dataset.slot = RICH_INPUT_SLOT
  input.tabIndex = 0
  layer.toggleAttribute('data-pane-hidden', hidden)
  layer.append(input)
  document.body.append(layer)

  return input
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('blurComposerInput', () => {
  it('blurs the foreground composer while a hidden tab matches first', () => {
    const background = mountInput(true)
    const foreground = mountInput()

    foreground.focus()
    blurComposerInput()

    expect(document.activeElement).not.toBe(foreground)
    expect(document.activeElement).not.toBe(background)
  })

  it('leaves focus alone when the composer does not hold it', () => {
    const outside = document.createElement('button')
    document.body.append(outside)
    mountInput()

    outside.focus()
    blurComposerInput()

    expect(document.activeElement).toBe(outside)
  })
})

/**
 * `markActiveComposer` has four call sites and, before this, no counterpart:
 * an unmounting composer left `activeTarget` pointing at itself, so every
 * `'active'`-routed request was delivered to a target with no subscriber.
 */
describe('releaseActiveComposer', () => {
  afterEach(() => {
    // `activeTarget` is module-level — a case that leaves a stale claim behind
    // would otherwise decide the next one.
    markActiveComposer('main')
  })

  it('falls back to the main composer when the claimant releases', () => {
    markActiveComposer('edit')
    expect(getActiveComposer()).toBe('edit')

    releaseActiveComposer('edit')

    expect(getActiveComposer()).toBe('main')
  })

  it('leaves the key with the live claimant when a stale composer releases late', () => {
    markActiveComposer('edit')
    markActiveComposer('tile:abc')

    releaseActiveComposer('edit')

    expect(getActiveComposer()).toBe('tile:abc')
  })

  it('routes an active-target request to the main composer once the edit composer closes', async () => {
    // Mirrors the per-composer filter in use-composer-draft / user-edit-composer:
    // a composer ignores any request not addressed to its own target.
    const mainComposerSaw: string[] = []

    const off = onComposerFocusRequest(({ target }) => {
      if (target === 'main') {
        mainComposerSaw.push(target)
      }
    })

    markActiveComposer('edit')
    releaseActiveComposer('edit')
    requestComposerFocus('active')

    // `dispatch` defers to a macrotask so click/keydown handlers settle first.
    await new Promise(resolve => window.setTimeout(resolve, 0))
    off()

    expect(mainComposerSaw).toEqual(['main'])
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Regression: the interacted-zone tracker must NOT switch on pointerdown.
// It used to, and a held press on a sidebar row (pointerdown fires, release
// hasn't happened yet) swapped `$activeTreeGroup` — and through it the
// derived `$focusedStoredSessionId` — to whatever zone the row's DOM sat in.
// With session tiles open, the focused session (statusbar workspace label +
// sidebar row highlight) flickered to another session for the entire hold.
// Tracking on `click` keeps the ⌘W semantics while the press is only
// committed once the button is released.

describe('trackActiveTreeGroup: click, not pointerdown, records the zone', () => {
  let cleanup: (() => void) | undefined

  beforeEach(() => {
    vi.resetModules()
  })

  afterEach(() => {
    cleanup?.()
    cleanup = undefined
    vi.resetModules()
  })

  async function setup() {
    const tree = await import('@/components/pane-shell/tree/store')

    const zone = document.createElement('div')
    zone.setAttribute('data-tree-group', 'grp-side')
    const target = document.createElement('button')
    zone.appendChild(target)
    document.body.appendChild(zone)

    cleanup = tree.trackActiveTreeGroup()

    return { target, tree }
  }

  it('a held press (pointerdown) does not move the active zone', async () => {
    const { target, tree } = await setup()

    target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))

    expect(tree.$activeTreeGroup.get()).toBeNull()
  })

  it('a completed click records the interacted zone', async () => {
    const { target, tree } = await setup()

    target.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(tree.$activeTreeGroup.get()).toBe('grp-side')
  })

  it('focusin (keyboard navigation) still records the zone', async () => {
    const { target, tree } = await setup()

    target.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))

    expect(tree.$activeTreeGroup.get()).toBe('grp-side')
  })

  it('a press outside any zone leaves the previous zone untouched', async () => {
    const { tree } = await setup()

    tree.noteActiveTreeGroup('grp-side')

    const stray = document.createElement('div')
    document.body.appendChild(stray)

    stray.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
    stray.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(tree.$activeTreeGroup.get()).toBe('grp-side')
  })

  it('teardown removes the listeners', async () => {
    const { target, tree } = await setup()

    cleanup?.()
    cleanup = undefined

    target.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(tree.$activeTreeGroup.get()).toBeNull()
  })
})

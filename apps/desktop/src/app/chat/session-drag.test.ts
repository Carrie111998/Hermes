import type { PointerEvent as ReactPointerEvent } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree, reorderTreePane } from '@/components/pane-shell/tree/store'
import { openSessionTile } from '@/store/session-states'

import { requestComposerInsertRefs } from './composer/focus'
import { startSessionDrag } from './session-drag'

/**
 * A session drop resolves its target by rect-testing the chat surfaces in the
 * document. A tab group keeps inactive tabs MOUNTED with their layout box
 * intact, so a background tab's rect is identical to the foreground tab's —
 * the drop has to land on the tab the user can actually see.
 */

vi.mock('@/store/session-states', () => ({ openSessionTile: vi.fn() }))
vi.mock('./composer/focus', () => ({ requestComposerInsertRefs: vi.fn() }))
vi.mock('@/components/pane-shell/tree/store', async () => {
  const actual = await vi.importActual<typeof import('@/components/pane-shell/tree/store')>(
    '@/components/pane-shell/tree/store'
  )

  return {
    ...actual,
    reorderTreePane: vi.fn(actual.reorderTreePane)
  }
})

const ZONE = { left: 0, top: 0, right: 1000, bottom: 800 }
const COMPOSER = { left: 100, top: 700, right: 900, bottom: 780 }
const STRIP = { left: 0, top: 0, right: 1000, bottom: 40 }

const stubRect = (el: Element, box: { left: number; top: number; right: number; bottom: number }) => {
  el.getBoundingClientRect = () =>
    ({ ...box, width: box.right - box.left, height: box.bottom - box.top, x: box.left, y: box.top }) as DOMRect
}

/** The workspace tab kept alive behind an active session tile tab. */
function mountStackedTabs() {
  document.body.innerHTML = `
    <div data-tree-group="g1">
      <div data-pane-hidden>
        <div data-session-anchor="workspace" data-composer-target="main">
          <div data-slot="composer-root"></div>
        </div>
      </div>
      <div>
        <div data-session-anchor="session-tile:visible" data-composer-target="tile:visible">
          <div data-slot="composer-root"></div>
        </div>
      </div>
    </div>
    <div id="row"></div>
  `

  stubRect(document.querySelector('[data-tree-group]')!, ZONE)

  for (const surface of document.querySelectorAll('[data-session-anchor]')) {
    stubRect(surface, ZONE)
  }

  for (const composer of document.querySelectorAll('[data-slot="composer-root"]')) {
    stubRect(composer, COMPOSER)
  }

  $layoutTree.set(group(['workspace', 'session-tile:visible'], { id: 'g1' }))

  return document.getElementById('row')!
}

/** Press on `source`, drag to (x, y), release. The drag session flushes its
 *  pending move synchronously on release, so no frame wait is needed. */
function dragTo(source: HTMLElement, x: number, y: number) {
  startSessionDrag({ id: 'dragged', profile: 'default', title: 'Dragged chat' }, {
    button: 0,
    clientX: 0,
    clientY: 0,
    currentTarget: source,
    pointerId: 1
  } as unknown as ReactPointerEvent<HTMLElement>)

  window.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: x, clientY: y }))
  window.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, clientX: x, clientY: y }))
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  document.body.innerHTML = ''
  $layoutTree.set(null)
})

describe('session drop targeting across stacked tabs', () => {
  it('links into the visible tab’s composer, not the tab kept alive behind it', () => {
    const row = mountStackedTabs()

    dragTo(row, 500, 740)

    expect(requestComposerInsertRefs).toHaveBeenCalledWith(expect.anything(), { target: 'tile:visible' })
  })

  it('docks a split against the visible tab’s pane', () => {
    const row = mountStackedTabs()

    dragTo(row, 980, 400)

    expect(openSessionTile).toHaveBeenCalledWith('dragged', 'right', 'session-tile:visible', undefined)
    expect(requestComposerInsertRefs).not.toHaveBeenCalled()
  })

  it('commits nothing over a zone that hosts no chat surface', () => {
    mountStackedTabs()
    $layoutTree.set(group(['terminal'], { id: 'g1' }))

    dragTo(document.getElementById('row')!, 500, 740)

    expect(requestComposerInsertRefs).not.toHaveBeenCalled()
    expect(openSessionTile).not.toHaveBeenCalled()
  })
})

describe('session tab strip reorder', () => {
  it('reorders an existing tab via reorderTreePane instead of openSessionTile', () => {
    document.body.innerHTML = `
      <div data-tree-group="g1">
        <div data-zone-tabstrip="g1" id="strip">
          <div data-tree-tab="workspace" id="tab-ws"></div>
          <div data-tree-tab="session-tile:a" id="tab-a"></div>
          <div data-tree-tab="session-tile:b" id="tab-b"></div>
        </div>
        <div data-session-anchor="workspace" data-composer-target="main">
          <div data-slot="composer-root"></div>
        </div>
      </div>
    `

    stubRect(document.querySelector('[data-tree-group]')!, ZONE)
    stubRect(document.getElementById('strip')!, STRIP)
    // Three equal tabs across the strip: mids at 100 / 300 / 500
    stubRect(document.getElementById('tab-ws')!, { left: 0, top: 0, right: 200, bottom: 40 })
    stubRect(document.getElementById('tab-a')!, { left: 200, top: 0, right: 400, bottom: 40 })
    stubRect(document.getElementById('tab-b')!, { left: 400, top: 0, right: 600, bottom: 40 })
    stubRect(document.querySelector('[data-session-anchor]')!, ZONE)
    stubRect(document.querySelector('[data-slot="composer-root"]')!, COMPOSER)

    $layoutTree.set(group(['workspace', 'session-tile:a', 'session-tile:b'], { id: 'g1' }))

    const tabA = document.getElementById('tab-a')!
    // Drag tab A to the left of workspace (x=50 → before workspace)
    startSessionDrag(
      { id: 'a', profile: 'default', title: 'A' },
      {
        button: 0,
        clientX: 300,
        clientY: 20,
        currentTarget: tabA,
        pointerId: 1
      } as unknown as ReactPointerEvent<HTMLElement>
    )
    window.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 50, clientY: 20 }))
    window.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, clientX: 50, clientY: 20 }))

    expect(openSessionTile).not.toHaveBeenCalled()
    expect(reorderTreePane).toHaveBeenCalledWith('g1', 'session-tile:a', 0)
  })

  it('reorders the workspace/main tab (selected session) instead of no-op openSessionTile', () => {
    document.body.innerHTML = `
      <div data-tree-group="g1">
        <div data-zone-tabstrip="g1" id="strip">
          <div data-tree-tab="workspace" id="tab-ws"></div>
          <div data-tree-tab="session-tile:b" id="tab-b"></div>
        </div>
        <div data-session-anchor="workspace" data-composer-target="main">
          <div data-slot="composer-root"></div>
        </div>
      </div>
    `

    stubRect(document.querySelector('[data-tree-group]')!, ZONE)
    stubRect(document.getElementById('strip')!, STRIP)
    stubRect(document.getElementById('tab-ws')!, { left: 0, top: 0, right: 200, bottom: 40 })
    stubRect(document.getElementById('tab-b')!, { left: 200, top: 0, right: 400, bottom: 40 })
    stubRect(document.querySelector('[data-session-anchor]')!, ZONE)
    stubRect(document.querySelector('[data-slot="composer-root"]')!, COMPOSER)

    $layoutTree.set(group(['workspace', 'session-tile:b'], { id: 'g1' }))

    const tabWs = document.getElementById('tab-ws')!
    // Drag workspace past B (x=350 → append)
    startSessionDrag(
      { id: 'main-session', profile: 'default', title: 'Main' },
      {
        button: 0,
        clientX: 100,
        clientY: 20,
        currentTarget: tabWs,
        pointerId: 1
      } as unknown as ReactPointerEvent<HTMLElement>
    )
    window.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 350, clientY: 20 }))
    window.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, clientX: 350, clientY: 20 }))

    // openSessionTile would early-return for the selected main session — reorder must win.
    expect(openSessionTile).not.toHaveBeenCalled()
    expect(reorderTreePane).toHaveBeenCalledWith('g1', 'workspace', 1)
  })
})

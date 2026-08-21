import { useStore } from '@nanostores/react'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import { stubResizeObserver } from '@/test/jsdom'

import { group } from '../model'
import { $layoutTree } from '../store'

import { TreeGroup } from './tree-group'

function LiveTreeGroup() {
  useStore($layoutTree)

  return <TreeGroup node={$layoutTree.get() as never} parentAxis="column" />
}

beforeAll(() => {
  stubResizeObserver()
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

let disposePane: (() => void) | undefined

afterEach(() => {
  cleanup()
  disposePane?.()
  disposePane = undefined
})

const groupNode = () =>
  $layoutTree.get() as { headerHidden?: boolean; minimized?: boolean; panes: string[] }

const doubleTap = (target: Element) => {
  for (let i = 0; i < 2; i++) {
    fireEvent.pointerDown(target, { button: 0, clientX: 10, clientY: 10, pointerType: 'mouse' })
    fireEvent.pointerUp(window, { button: 0, clientX: 10, clientY: 10, pointerType: 'mouse' })
  }
}

describe('session strip visibility', () => {
  it('undoes the first-tap collapse without persisting a header hide', () => {
    const paneId = 'session-tile:test'
    disposePane = registry.register({
      area: 'panes',
      data: { placement: 'main' },
      id: paneId,
      render: () => null,
      title: 'Session'
    })
    $layoutTree.set(group([paneId], { active: paneId, id: 'grp-session' }))
    render(<LiveTreeGroup />)

    const strip = globalThis.document.querySelector<HTMLElement>('[data-zone-tabstrip="grp-session"]')
    expect(strip).toBeTruthy()

    doubleTap(strip!)

    expect(groupNode().minimized).not.toBe(true)
    expect(groupNode().headerHidden).not.toBe(true)
  })
})

import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { group, split } from '../model'
import { $layoutEqualizeMotion } from '../store'

import { TreeSplit } from './tree-split'

vi.mock('./tree-node', () => ({
  TreeNode: () => <div />
}))

afterEach(() => {
  act(() => $layoutEqualizeMotion.set(false))
  cleanup()
})

describe('TreeSplit equalization motion', () => {
  it('transitions split tracks only during the equalization motion window', () => {
    const node = split(
      'row',
      [group([], { id: 'g-a' }), group([], { id: 'g-b' })],
      [3, 1],
      's-root'
    )

    const { container } = render(<TreeSplit node={node} />)

    const wrappers = () => [...container.querySelectorAll(':scope > [data-tree-split] > div')]

    expect(wrappers()).toHaveLength(2)
    expect(wrappers().every(wrapper => !wrapper.className.includes('transition-'))).toBe(true)

    act(() => $layoutEqualizeMotion.set(true))

    expect(
      wrappers().every(wrapper =>
        wrapper.className.includes('transition-[flex-grow,flex-shrink,flex-basis]')
      )
    ).toBe(true)
    expect(wrappers().every(wrapper => wrapper.className.includes('motion-reduce:transition-none'))).toBe(true)
  })
})
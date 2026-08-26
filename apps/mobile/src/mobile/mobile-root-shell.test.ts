// @vitest-environment jsdom
import { render } from '@testing-library/react'
import { createElement } from 'react'
import { describe, expect, it } from 'vitest'

import { MobileRootShell } from './mobile-root-shell'

describe('MobileRootShell', () => {
  it('owns one viewport wrapper around every mobile child so toolbar buttons cannot become root-sized rails', () => {
    const { container } = render(
      createElement(MobileRootShell, null, createElement('button', { type: 'button' }, 'Menu'))
    )

    const shell = container.querySelector('[data-mobile-root-shell]')
    expect(shell).toBeTruthy()
    expect(shell?.firstElementChild?.tagName).toBe('BUTTON')
    expect(shell?.parentElement).toBe(container)
  })
})

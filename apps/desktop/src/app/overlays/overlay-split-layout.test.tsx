import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { OverlaySplitLayout } from './overlay-split-layout'

describe('OverlaySplitLayout', () => {
  it('exposes a stable mobile layout hook so a wide Fold does not retain the desktop rail', () => {
    const { container } = render(
      <OverlaySplitLayout>
        <div>content</div>
      </OverlaySplitLayout>,
    )

    expect(container.querySelector('[data-overlay-split-layout]')).toBeTruthy()
  })
})

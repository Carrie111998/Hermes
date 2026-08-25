import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

const { ConnectionOriginTag } = await import('./connection-origin-tag')

afterEach(cleanup)

// The tag only reads `kind` + `label`; the rest of the registry row is
// irrelevant here, so qualify a minimal fixture at the call sites.
const SSH = {
  id: 'mimir',
  kind: 'ssh',
  label: 'mimir'
} as Parameters<typeof ConnectionOriginTag>[0]['connection']

describe('ConnectionOriginTag', () => {
  it('labels a foreign gateway with kind icon + label', () => {
    render(<ConnectionOriginTag connection={SSH} />)

    const tag = screen.getByRole('img', { name: /mimir/ })
    expect(tag.textContent).toContain('mimir')
  })

  it('uses the gateway kind label in its accessible name', () => {
    render(<ConnectionOriginTag connection={SSH} />)

    const tag = screen.getByRole('img', { name: /mimir ·/ })
    // SSH is a remote gateway — never empty/blank.
    expect(tag?.getAttribute('aria-label')).toMatch(/·\s*\S+/)
  })
})
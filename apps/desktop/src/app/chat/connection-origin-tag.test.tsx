import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'

const { ConnectionOriginTag, sharedSessionsOrigin, visibleSessionOrigin } = await import('./connection-origin-tag')

afterEach(cleanup)

// The tag only reads `kind` + `label`; the rest of the registry row is
// irrelevant here, so qualify a minimal fixture at the call sites.
const SSH = {
  id: 'mimir',
  kind: 'ssh',
  label: 'mimir'
} as DesktopRegistryConnection

const LOCAL = {
  id: 'local',
  kind: 'local',
  label: 'This device'
} as DesktopRegistryConnection

const REMOTE = {
  id: 'homelab',
  kind: 'remote',
  label: 'Homelab'
} as DesktopRegistryConnection

const registry = {
  connections: [LOCAL, SSH, REMOTE],
  primary: 'local',
  secureTokenStorage: true,
  version: 2
} as DesktopConnectionsRegistry

describe('ConnectionOriginTag', () => {
  it('labels a foreign gateway with kind icon + label', () => {
    render(<ConnectionOriginTag connection={SSH} />)

    const tag = screen.getByRole('img', { name: /mimir/ })
    expect(tag.textContent).toContain('mimir')
    expect(tag.getAttribute('data-slot')).toBe('connection-origin-tag')
    expect(tag.getAttribute('data-connection-kind')).toBe('ssh')
  })

  it('uses the gateway kind label in its accessible name', () => {
    render(<ConnectionOriginTag connection={SSH} />)

    const tag = screen.getByRole('img', { name: /mimir ·/ })
    // SSH is a remote gateway — never empty/blank.
    expect(tag?.getAttribute('aria-label')).toMatch(/·\s*\S+/)
  })

  it('can render icon-only for tab chrome', () => {
    render(<ConnectionOriginTag connection={SSH} iconOnly />)

    const tag = screen.getByRole('img', { name: /mimir ·/ })
    expect(tag.textContent).toBe('')
  })

  it('can render a quiet label-only suffix for group headers', () => {
    render(<ConnectionOriginTag connection={SSH} quiet />)

    const tag = screen.getByRole('img', { name: /mimir ·/ })
    expect(tag.textContent).toBe('mimir')
  })
})

describe('visibleSessionOrigin', () => {
  it('hides the local default', () => {
    expect(visibleSessionOrigin({}, registry, 'local')).toBeNull()
  })

  it('names a session pinned to a foreign gateway', () => {
    expect(visibleSessionOrigin({ connection_id: 'mimir' }, registry, 'local')).toEqual(SSH)
  })

  it('prefers the section origin for a foreign list', () => {
    expect(visibleSessionOrigin({}, registry, 'local', REMOTE)).toEqual(REMOTE)
  })
})

describe('sharedSessionsOrigin', () => {
  it('returns the shared foreign origin', () => {
    expect(sharedSessionsOrigin([{ connection_id: 'mimir' }, { connection_id: 'mimir' }], registry, 'local')).toEqual(
      SSH
    )
  })

  it('returns null when the group is mixed', () => {
    expect(
      sharedSessionsOrigin([{ connection_id: 'mimir' }, { connection_id: 'homelab' }], registry, 'local')
    ).toBeNull()
  })
})

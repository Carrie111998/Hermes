import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { pathForRegistryBackendRequest } from './connection-config'

const here = path.dirname(fileURLToPath(import.meta.url))
const mainSource = fs.readFileSync(path.join(here, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')

describe('registry-primary REST scoping (#98578)', () => {
  it('stamps sharedRemote on the primary-owns reuse fast path so REST keeps ?profile=', () => {
    const branchStart = mainSource.indexOf(
      "if (id === registry.primary && source.kind !== 'local' && source.kind !== 'ssh') {"
    )
    expect(branchStart).toBeGreaterThan(-1)

    const body = mainSource.slice(branchStart, branchStart + 700)

    expect(body).toContain('const primaryDescriptor = await ensureBackend(profile)')
    expect(body).toContain('if (registrySourceOwnsPrimaryBackend(registry, id, primaryDescriptor)) {')

    // The reused primary IS the shared remote gateway (one host, every
    // profile). Without this stamp the descriptor falls into the
    // self-profile-translation branch, which never appends ?profile=, and
    // every registry-scoped Settings/Profiles read answers for the
    // backend's own home instead of the selected profile (#98578).
    expect(body).toContain('sharedRemote: true')
  })

  it('the stamped descriptor scopes a profile read the same way a fresh-dial remote does', () => {
    const reusedPrimaryDescriptor = {
      baseUrl: 'http://127.0.0.1:8643',
      profile: 'architect',
      connectionId: 'wsl-gentoo',
      sharedRemote: true
    }

    expect(
      pathForRegistryBackendRequest('/api/config', 'architect', reusedPrimaryDescriptor)
    ).toBe('/api/config?profile=architect')

    // Pre-fix shape (no stamp): the translation branch leaves the path
    // unscoped, so the backend serves its home profile's config.
    expect(
      pathForRegistryBackendRequest('/api/config', 'architect', {
        baseUrl: 'http://127.0.0.1:8643',
        profile: 'architect',
        connectionId: 'wsl-gentoo'
      })
    ).toBe('/api/config')
  })
})

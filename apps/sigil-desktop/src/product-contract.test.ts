import fs from 'node:fs'
import path from 'node:path'

import { DisconnectedHermesEngine } from './hermes-engine'

describe('standalone product contract', () => {
  const packageJson = JSON.parse(fs.readFileSync(path.resolve('package.json'), 'utf8'))

  it('uses collision-free Sigil package identity and artifact names', () => {
    expect(packageJson.productName).toBe('Sigil')
    expect(packageJson.build.appId).toBe('com.firecattechnology.sigil')
    expect(packageJson.build.executableName).toBe('Sigil')
    expect(packageJson.build.artifactName).toMatch(/^Sigil-/)
    expect(packageJson.build.appId).not.toContain('hermes')
  })

  it('defaults to dark and titles the product Sigil', () => {
    const html = fs.readFileSync(path.resolve('index.html'), 'utf8')
    expect(html).toContain('<html lang="en" class="dark">')
    expect(html).toContain('<title>Sigil</title>')
  })

  it('functions with a disconnected Hermes engine', async () => {
    const engine = new DisconnectedHermesEngine()
    expect(engine.status).toBe('disconnected')
    expect((await engine.analyze({ evidenceReferences: [], prompt: 'status' })).source).toBe('local')
  })
})

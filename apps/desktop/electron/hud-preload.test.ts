import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from 'vitest'

const here = path.dirname(fileURLToPath(import.meta.url))
const preloadSource = fs.readFileSync(path.join(here, 'preload.ts'), 'utf8').replace(/\r\n/g, '\n')

test('HUD preload forwards the complete session identity payload', () => {
  expect(preloadSource).toContain("setSession: state => ipcRenderer.send('hermes:hud:session', state)")
})

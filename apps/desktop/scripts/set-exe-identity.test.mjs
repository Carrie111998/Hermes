import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { expect, it } from 'vitest'

import { stampExeIdentity } from './set-exe-identity.mjs'

it('Windows executable stamping uses the Eva icon and Electric Sheep metadata', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-exe-identity-'))

  try {
    const assets = path.join(root, 'assets')
    const exe = path.join(root, 'evaOS Agent.exe')
    const icon = path.join(assets, 'eva.ico')
    fs.mkdirSync(assets, { recursive: true })
    fs.writeFileSync(exe, 'fake executable')
    fs.writeFileSync(icon, 'fake icon')

    const calls = []
    await stampExeIdentity(exe, root, async (...args) => calls.push(args))

    expect(calls).toEqual([
      [
        exe,
        {
          icon,
          'version-string': {
            ProductName: 'evaOS Agent',
            FileDescription: 'evaOS Agent',
            CompanyName: 'Electric Sheep',
            LegalCopyright: 'Copyright (c) 2026 Electric Sheep'
          }
        }
      ]
    ])
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { Data, NtExecutable, NtExecutableResource, Resource } from 'resedit'

import { stampExeIdentity } from './set-exe-identity.mjs'

const EN_US = { lang: 1033, codepage: 1200 }

test('stamps Hermes branding and icon while preserving fixed version data', async t => {
  const root = mkdtempSync(path.join(tmpdir(), 'hermes-exe-identity-'))
  t.onTestFinished(() => rmSync(root, { recursive: true, force: true }))

  const assets = path.join(root, 'assets')
  mkdirSync(assets)
  const sourceIcon = path.resolve(import.meta.dirname, '..', 'assets', 'icon.ico')
  writeFileSync(path.join(assets, 'icon.ico'), readFileSync(sourceIcon))

  const pe = NtExecutable.createEmpty(true, true)
  const resources = NtExecutableResource.from(pe)
  const version = Resource.VersionInfo.createEmpty()
  version.lang = 1033
  version.setFileVersion(41, 10, 3, 0, 1033)
  version.setProductVersion(41, 10, 3, 0, 1033)
  version.setStringValues(EN_US, { ProductName: 'Electron', FileDescription: 'Electron' })
  version.outputToResourceEntries(resources.entries)
  resources.outputResource(pe)

  const exe = path.join(root, 'Hermes.exe')
  writeFileSync(exe, Buffer.from(pe.generate()))
  await stampExeIdentity(exe, root)

  const stamped = NtExecutable.from(readFileSync(exe))
  const stampedResources = NtExecutableResource.from(stamped)
  const stampedVersion = Resource.VersionInfo.fromEntries(stampedResources.entries)[0]
  assert.ok(stampedVersion)
  assert.equal(stampedVersion.fixedInfo.fileVersionMS, (41 << 16) | 10)
  assert.equal(stampedVersion.fixedInfo.fileVersionLS, 3 << 16)
  assert.equal(stampedVersion.fixedInfo.productVersionMS, (41 << 16) | 10)
  assert.equal(stampedVersion.fixedInfo.productVersionLS, 3 << 16)
  assert.deepEqual(
    {
      ...stampedVersion.getStringValues(EN_US),
      FileVersion: undefined,
      ProductVersion: undefined
    },
    {
      ProductName: 'Hermes',
      FileDescription: 'Hermes',
      CompanyName: 'Nous Research',
      LegalCopyright: 'Copyright (c) 2026 Nous Research',
      FileVersion: undefined,
      ProductVersion: undefined
    }
  )
  assert.ok(Resource.IconGroupEntry.fromEntries(stampedResources.entries).length > 0)
  assert.ok(Data.IconFile.from(readFileSync(path.join(assets, 'icon.ico'))).icons.length > 0)
})
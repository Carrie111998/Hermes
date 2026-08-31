import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import {
  applyDesktopConnectionConfig,
  readDesktopConnectionConfigSnapshot,
  saveDesktopConnectionConfig
} from './connection-config-boundary'
import { buildAgentRoster, normalizeRegistry, resolveRegistryLocalRoute } from './connection-registry'
import { enumerateRegistryAgentSourcesObservational } from './connection-roster'

const temporaryDirectories: string[] = []

function temporaryDirectory(): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-connection-boundary-'))

  temporaryDirectories.push(directory)

  return directory
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    fs.rmSync(directory, { force: true, recursive: true })
  }
})

const secretCodec = {
  decryptSecret(secret: unknown): string {
    if (!secret || typeof secret !== 'object') {
      return ''
    }

    return String((secret as { data?: unknown }).data || '')
  },
  encryptSecret(value: unknown): { data: string; encoding: 'test' } {
    return { data: String(value || ''), encoding: 'test' }
  }
}

function writeJson(filePath: string, value: unknown): void {
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
}

function createConfigStore(filePath: string) {
  let cachedConfig: unknown = null
  let cachedMtime: null | number = null

  return {
    clearCache(): void {
      cachedConfig = null
      cachedMtime = null
    },
    read(): any {
      const snapshot = readDesktopConnectionConfigSnapshot({
        cachedConfig,
        cachedMtime,
        readText: () => fs.readFileSync(filePath, 'utf8'),
        statMtime: () => fs.statSync(filePath).mtimeMs,
        tighten: () => undefined
      })

      cachedConfig = snapshot.config
      cachedMtime = snapshot.mtime

      return snapshot.config
    },
    write(config: unknown): void {
      writeJson(filePath, config)
      cachedConfig = config
      cachedMtime = fs.statSync(filePath).mtimeMs
    }
  }
}

test('production save/apply boundary preserves exact local intent across bytes, reread, rollback, restart, and roster', async () => {
  const directory = temporaryDirectory()
  const configPath = path.join(directory, 'connection.json')
  const registryPath = path.join(directory, 'connections.json')

  const initialRegistry = normalizeRegistry({
    version: 2,
    primary: 'pop-os-hermes',
    lastUsed: 'pop-os-hermes',
    launchMode: 'last-used',
    connections: [
      { id: 'local', kind: 'local', label: 'This Device' },
      {
        id: 'pop-os-hermes',
        kind: 'remote',
        label: 'POP-OS',
        url: 'https://pop-os.example'
      }
    ]
  })

  writeJson(configPath, {
    mode: 'remote',
    remote: { authMode: 'token', token: secretCodec.encryptSecret('remote-token'), url: 'https://pop-os.example' }
  })
  writeJson(registryPath, initialRegistry)

  const store = createConfigStore(configPath)
  const readRegistry = () => JSON.parse(fs.readFileSync(registryPath, 'utf8'))
  const writeRegistry = (registry: unknown) => writeJson(registryPath, registry)
  const localInput = { mode: 'local', profile: 'mac-cockpit' }

  saveDesktopConnectionConfig({
    input: localInput,
    readConfig: store.read,
    secrets: secretCodec,
    writeConfig: store.write
  })

  const savedBytes = fs.readFileSync(configPath, 'utf8')
  const savedJson = JSON.parse(savedBytes)

  assert.deepEqual(savedJson.profiles['mac-cockpit'], { mode: 'local' })
  assert.equal(savedJson.profiles.default, undefined)

  saveDesktopConnectionConfig({
    input: localInput,
    readConfig: store.read,
    secrets: secretCodec,
    writeConfig: store.write
  })
  assert.equal(fs.readFileSync(configPath, 'utf8'), savedBytes)

  const externallyEdited = JSON.parse(savedBytes)
  externallyEdited.profiles['mac-cockpit'] = {
    mode: 'local',
    authMode: 'token',
    token: secretCodec.encryptSecret('stale-token'),
    url: 'https://stale-remote.example'
  }
  externallyEdited.profiles['remote-only'] = {
    mode: 'remote',
    remote: { authMode: 'none', url: 'https://remote-only.example' }
  }
  externallyEdited.profiles['unknown-mode'] = { mode: 'future-local' }
  externallyEdited.profiles.malformed = { mode: { nested: true } }
  writeJson(configPath, externallyEdited)
  const externalTime = new Date(Date.now() + 5_000)
  fs.utimesSync(configPath, externalTime, externalTime)

  const externallyReread = store.read()

  assert.deepEqual(externallyReread.profiles['mac-cockpit'], { mode: 'local' })
  assert.equal(externallyReread.profiles['remote-only'].mode, 'remote')
  assert.equal(externallyReread.profiles['unknown-mode'], undefined)
  assert.equal(externallyReread.profiles.malformed, undefined)

  await applyDesktopConnectionConfig({
    apply: async () => undefined,
    input: localInput,
    preflight: async () => undefined,
    readConfig: store.read,
    readRegistry,
    reconcileRegistry: registry => registry,
    secrets: secretCodec,
    writeConfig: store.write,
    writeRegistry
  })

  const beforeRollbackConfig = fs.readFileSync(configPath, 'utf8')
  const beforeRollbackRegistry = fs.readFileSync(registryPath, 'utf8')

  await assert.rejects(
    applyDesktopConnectionConfig({
      apply: async () => {
        throw new Error('activation failed')
      },
      input: { mode: 'local', profile: 'temporary-local' },
      preflight: async () => undefined,
      readConfig: store.read,
      readRegistry,
      reconcileRegistry: registry => ({ ...registry, lastUsed: 'local' }),
      secrets: secretCodec,
      writeConfig: store.write,
      writeRegistry
    }),
    /activation failed/
  )
  assert.equal(fs.readFileSync(configPath, 'utf8'), beforeRollbackConfig)
  assert.equal(fs.readFileSync(registryPath, 'utf8'), beforeRollbackRegistry)

  const writesBeforeMalformedInputs = fs.readFileSync(configPath, 'utf8')

  assert.throws(
    () =>
      saveDesktopConnectionConfig({
        input: { mode: 'mystery', profile: 'mac-cockpit' },
        readConfig: store.read,
        secrets: secretCodec,
        writeConfig: store.write
      }),
    /Unsupported connection mode/
  )
  await assert.rejects(
    applyDesktopConnectionConfig({
      apply: async () => undefined,
      input: { mode: { malformed: true }, profile: 'mac-cockpit' },
      readConfig: store.read,
      readRegistry,
      reconcileRegistry: registry => registry,
      secrets: secretCodec,
      writeConfig: store.write,
      writeRegistry
    }),
    /Unsupported connection mode/
  )
  assert.equal(fs.readFileSync(configPath, 'utf8'), writesBeforeMalformedInputs)

  store.clearCache()
  const restartedConfig = store.read()
  const restartedRegistry = normalizeRegistry(readRegistry())

  assert.deepEqual(restartedConfig.profiles['mac-cockpit'], { mode: 'local' })
  assert.equal(restartedConfig.profiles['unknown-mode'], undefined)
  assert.equal(restartedConfig.profiles.malformed, undefined)

  const registryBeforeRoster = structuredClone(restartedRegistry)
  const poolReads: string[] = []
  let descriptorInventoryReads = 0

  const primaryDescriptor = Promise.resolve({
    baseUrl: 'http://127.0.0.1:9119',
    connectionId: 'local',
    profiles: ['default', 'unrelated-profile']
  })

  const enumerations = await enumerateRegistryAgentSourcesObservational({
    cachedProfiles: new Map(),
    configuredLocalProfiles: ['mac-cockpit'],
    localRoute: resolveRegistryLocalRoute('default', { globalRemote: restartedConfig.mode === 'remote' }),
    pooledDescriptorPromises: {
      get: key => {
        poolReads.push(key)

        return key === 'unrelated-profile'
          ? Promise.resolve({ connectionId: 'local', profiles: ['unrelated-profile'] })
          : null
      }
    },
    primaryDescriptorPromise: primaryDescriptor,
    readDescriptorProfiles: async descriptor => {
      descriptorInventoryReads += 1

      return { profiles: descriptor.profiles }
    },
    registry: restartedRegistry
  })

  const roster = buildAgentRoster(enumerations)

  assert.deepEqual(
    roster.map(agent => ({ handle: agent.handle, id: `${agent.connectionId}::${agent.profile}` })),
    [{ handle: 'mac-cockpit', id: 'local::mac-cockpit' }]
  )
  assert.equal(descriptorInventoryReads, 0)
  assert.deepEqual(poolReads, ['conn:pop-os-hermes::default'])
  assert.deepEqual(restartedRegistry, registryBeforeRoster)

  const remoteOnly = await enumerateRegistryAgentSourcesObservational({
    cachedProfiles: new Map(),
    configuredLocalProfiles: [],
    localRoute: resolveRegistryLocalRoute(restartedRegistry.primary, { globalRemote: true }),
    pooledDescriptorPromises: new Map(),
    primaryDescriptorPromise: null,
    readDescriptorProfiles: async () => {
      throw new Error('unreachable: no descriptor exists')
    },
    registry: restartedRegistry
  })

  assert.deepEqual(buildAgentRoster(remoteOnly), [])
})

test('production roster boundary seeds only the exact persisted identity for a local-primary delegate', async () => {
  const registry = normalizeRegistry({
    version: 2,
    primary: 'local',
    lastUsed: 'local',
    connections: [{ id: 'local', kind: 'local', label: 'This Device' }]
  })

  const beforeRegistry = structuredClone(registry)
  const poolReads: string[] = []

  const enumerations = await enumerateRegistryAgentSourcesObservational({
    cachedProfiles: { get: () => undefined } as any,
    configuredLocalProfiles: ['mac-cockpit'],
    localRoute: resolveRegistryLocalRoute('default', {}),
    pooledDescriptorPromises: {
      get: key => {
        poolReads.push(key)

        return null
      }
    },
    // A persisted exact identity must be emitted without awaiting or inventorying
    // the primary descriptor, even when local owns that backend.
    primaryDescriptorPromise: new Promise(() => undefined),
    readDescriptorProfiles: async () => {
      throw new Error('unreachable: persisted local identity must not inventory HERMES_HOME')
    },
    registry,
    timeoutMs: 5
  })

  assert.deepEqual(
    buildAgentRoster(enumerations).map(agent => `${agent.connectionId}::${agent.profile}`),
    ['local::mac-cockpit']
  )
  assert.deepEqual(poolReads, [])
  assert.deepEqual(registry, beforeRegistry)
})

test('production roster boundary reads an exact non-primary pool entry without awaiting the unrelated primary', async () => {
  const registry = normalizeRegistry({
    version: 2,
    primary: 'local',
    connections: [
      { id: 'local', kind: 'local', label: 'This Device' },
      { id: 'lab', kind: 'remote', label: 'Lab', url: 'https://lab.example' }
    ]
  })

  const poolReads: string[] = []
  const exactPoolKey = 'conn:lab::default'

  const enumerations = await enumerateRegistryAgentSourcesObservational({
    cachedProfiles: new Map(),
    configuredLocalProfiles: ['mac-cockpit'],
    localRoute: resolveRegistryLocalRoute('default', {}),
    pooledDescriptorPromises: {
      get: key => {
        poolReads.push(key)

        return key === exactPoolKey ? Promise.resolve({ connectionId: 'lab', profiles: ['research'] }) : null
      }
    },
    primaryDescriptorPromise: new Promise<{ connectionId: string; profiles: string[] }>(() => undefined),
    readDescriptorProfiles: async (descriptor: { connectionId: string; profiles: string[] }) => ({
      profiles: descriptor.profiles
    }),
    registry,
    timeoutMs: 5
  })

  assert.deepEqual(poolReads, [exactPoolKey])
  assert.deepEqual(
    buildAgentRoster(enumerations).map(agent => `${agent.connectionId}::${agent.profile}`),
    ['local::mac-cockpit', 'lab::default', 'lab::research']
  )
})

test('production roster boundary preserves observational SSH inventory without backend ownership changes', async () => {
  const registry = normalizeRegistry({
    version: 2,
    primary: 'ssh-lab',
    connections: [
      { id: 'local', kind: 'local', label: 'This Device' },
      { id: 'ssh-lab', kind: 'ssh', label: 'SSH Lab', host: 'lab.example' }
    ]
  })

  const inventoryCalls: string[] = []
  const cachedProfiles = new Map<string, string[]>()

  const enumerations = await enumerateRegistryAgentSourcesObservational({
    cachedProfiles,
    configuredLocalProfiles: [],
    localRoute: resolveRegistryLocalRoute('default', { globalRemote: true }),
    pooledDescriptorPromises: {
      get: () => {
        throw new Error('SSH inventory must not inspect or create a backend pool entry')
      }
    },
    primaryDescriptorPromise: null,
    readDescriptorProfiles: async () => {
      throw new Error('SSH inventory must not require a backend descriptor')
    },
    probeSshProfiles: async (connection: any) => {
      inventoryCalls.push(connection.id)
      cachedProfiles.set(connection.id, ['ops'])
    },
    registry
  } as any)

  assert.deepEqual(inventoryCalls, ['ssh-lab'])
  assert.deepEqual(
    buildAgentRoster(enumerations).map(agent => `${agent.connectionId}::${agent.profile}`),
    ['ssh-lab::ops']
  )
})

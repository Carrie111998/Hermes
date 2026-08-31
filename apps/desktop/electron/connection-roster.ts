import {
  backendScopeKey,
  type ConnectionAgents,
  type ConnectionRegistry,
  enumerateRegistryLocalSource,
  type RegistryConnection,
  type RegistryLocalRoute,
  registrySourceOwnsPrimaryBackend,
  rememberSshEnumeration,
  resolvedConnectionId,
  type RosterProfileMetadata
} from './connection-registry'

export interface DescriptorProfiles {
  installId?: string
  profiles: string[]
  profileMetadata?: Record<string, RosterProfileMetadata>
}

interface EnumerateRegistryAgentSourcesObservationalOptions<TDescriptor> {
  cachedProfiles: ReadonlyMap<string, readonly string[]>
  configuredLocalProfiles: Iterable<string>
  localRoute: RegistryLocalRoute
  pooledDescriptorPromises: { get: (key: string) => null | Promise<TDescriptor> | undefined }
  primaryDescriptorPromise: null | Promise<TDescriptor>
  readDescriptorProfiles: (descriptor: TDescriptor, connection: RegistryConnection) => Promise<DescriptorProfiles>
  probeSshProfiles?: (connection: RegistryConnection) => Promise<void>
  registry: ConnectionRegistry
  timeoutMs?: number
}

function withDeadline<T>(work: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null

  return Promise.race([
    work,
    new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('roster enumeration timed out')), timeoutMs)
    })
  ]).finally(() => {
    if (timer !== null) {
      clearTimeout(timer)
    }
  })
}

async function existingOwnedDescriptor<TDescriptor>(
  registry: ConnectionRegistry,
  connection: RegistryConnection,
  primaryDescriptorPromise: null | Promise<TDescriptor>,
  pooledDescriptorPromises: { get: (key: string) => null | Promise<TDescriptor> | undefined },
  poolKey: string,
  timeoutMs: number
): Promise<null | TDescriptor> {
  if (connection.id === registry.primary && primaryDescriptorPromise) {
    const primary = await withDeadline(Promise.resolve(primaryDescriptorPromise), timeoutMs)

    if (registrySourceOwnsPrimaryBackend(registry, connection.id, primary as any)) {
      return primary
    }
  }

  const pooledDescriptorPromise = pooledDescriptorPromises.get(poolKey)

  if (!pooledDescriptorPromise) {
    return null
  }

  const pooledDescriptor = await withDeadline(Promise.resolve(pooledDescriptorPromise), timeoutMs)

  return resolvedConnectionId(registry, pooledDescriptor as any) === connection.id ? pooledDescriptor : null
}

/**
 * Enumerate the union roster without any backend-creation capability. Inputs
 * contain only already-owned descriptor promises and exact pool keys; there is
 * intentionally no ensure/dial/start/retarget callback at this boundary.
 */
export async function enumerateRegistryAgentSourcesObservational<TDescriptor>({
  cachedProfiles,
  configuredLocalProfiles,
  localRoute,
  pooledDescriptorPromises,
  primaryDescriptorPromise,
  readDescriptorProfiles,
  probeSshProfiles,
  registry,
  timeoutMs = 10_000
}: EnumerateRegistryAgentSourcesObservationalOptions<TDescriptor>): Promise<ConnectionAgents[]> {
  return Promise.all(
    registry.connections.map(async connection => {
      let raw: ConnectionAgents

      try {
        let descriptor: null | TDescriptor = null
        let details: DescriptorProfiles | null = null

        if (connection.kind === 'ssh' && probeSshProfiles) {
          await probeSshProfiles(connection)
          raw = { connection, profiles: null, error: 'connect-on-demand' }
        } else if (connection.kind === 'local') {
          const localResolution = await enumerateRegistryLocalSource({
            configuredLocalProfiles,
            route: localRoute,
            getDelegateDescriptor: () =>
              existingOwnedDescriptor(
                registry,
                connection,
                primaryDescriptorPromise,
                pooledDescriptorPromises,
                localRoute.poolKey,
                timeoutMs
              ),
            getPooledDescriptor: poolKey => {
              const descriptorPromise = pooledDescriptorPromises.get(poolKey)

              if (!descriptorPromise) {
                return null
              }

              return withDeadline(Promise.resolve(descriptorPromise), timeoutMs).then(candidate => {
                return resolvedConnectionId(registry, candidate as any) === connection.id ? candidate : null
              })
            }
          })

          if (localResolution.action === 'seed') {
            raw = {
              connection,
              profiles: localResolution.profiles,
              error: localResolution.error
            }
          } else {
            descriptor = localResolution.descriptor
            raw = { connection, profiles: null, error: 'connect-on-demand' }
          }
        } else {
          descriptor = await existingOwnedDescriptor(
            registry,
            connection,
            primaryDescriptorPromise,
            pooledDescriptorPromises,
            backendScopeKey(connection.id, null),
            timeoutMs
          )
          raw = { connection, profiles: null, error: 'connect-on-demand' }
        }

        if (descriptor) {
          details = await readDescriptorProfiles(descriptor, connection)
        }

        if (details) {
          const profiles = [...new Set(details.profiles.map(profile => String(profile || '').trim()).filter(Boolean))]

          if (!profiles.includes('default')) {
            profiles.unshift('default')
          }

          raw = {
            connection,
            profiles,
            ...(details.installId ? { installId: details.installId } : {}),
            ...(details.profileMetadata ? { profileMetadata: details.profileMetadata } : {})
          }
        }
      } catch (error: any) {
        raw = { connection, profiles: null, error: String(error?.message || error) }
      }

      const remembered = rememberSshEnumeration(
        raw,
        cachedProfiles.get(connection.id) ? [...(cachedProfiles.get(connection.id) || [])] : undefined,
        connection.kind
      )

      return {
        connection,
        ...remembered,
        ...(raw.installId ? { installId: raw.installId } : {}),
        ...(raw.profileMetadata ? { profileMetadata: raw.profileMetadata } : {})
      }
    })
  )
}

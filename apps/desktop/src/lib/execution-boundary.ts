interface ExecutionBoundaryConnection {
  mode?: 'local' | 'remote'
  remoteHost?: string
  remoteKind?: 'cloud' | 'ssh' | 'url'
}

interface ExecutionBoundary {
  host: string | null
  kind: 'cloud' | 'local' | 'remote' | 'ssh'
}

export function resolveExecutionBoundary(connection: ExecutionBoundaryConnection | null): ExecutionBoundary {
  if (connection?.mode !== 'remote') {
    return { host: null, kind: 'local' }
  }

  const kind = connection.remoteKind === 'ssh' || connection.remoteKind === 'cloud' ? connection.remoteKind : 'remote'

  return { host: connection.remoteHost?.trim() || null, kind }
}

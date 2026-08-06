import { Codecs, persistentAtom } from '@/lib/persisted'

export type WorkspaceContextBindings = Record<string, string[]>

function validBindings(raw: unknown): WorkspaceContextBindings {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {}
  }

  const result: WorkspaceContextBindings = {}

  for (const [projectId, value] of Object.entries(raw)) {
    if (!Array.isArray(value)) {
      continue
    }

    const channelIds = value.filter(item => typeof item === 'string' && /^[CG][A-Z0-9]+$/.test(item))

    if (channelIds.length) {
      result[projectId] = [...new Set(channelIds)]
    }
  }

  return result
}

export const $workspaceContextBindings = persistentAtom<WorkspaceContextBindings>(
  'hermes.desktop.workspaceContextBindings',
  {},
  Codecs.json<WorkspaceContextBindings>(validBindings)
)

export function projectSlackChannelIds(projectId: null | string): string[] {
  if (!projectId) {
    return []
  }

  return $workspaceContextBindings.get()[projectId] ?? []
}

export function setProjectSlackChannelIds(projectId: string, values: string[]): void {
  const channelIds = [...new Set(values.map(value => value.trim().toUpperCase()).filter(Boolean))]

  if (channelIds.some(value => !/^[CG][A-Z0-9]+$/.test(value))) {
    throw new Error('Slack project bindings must use channel IDs (C… or G…), not DM IDs.')
  }

  const next = { ...$workspaceContextBindings.get() }

  if (channelIds.length) {
    next[projectId] = channelIds
  } else {
    delete next[projectId]
  }

  $workspaceContextBindings.set(next)
}

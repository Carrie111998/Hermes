import { useCallback } from 'react'

import { gatewayEventCompletedFileDiff } from '@/lib/gateway-events'
import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { normalizeProfileKey } from '@/lib/session-identity'
import {
  $previewTabs,
  beginPreviewServerRestart,
  completePreviewServerRestart,
  openPreview,
  progressPreviewServerRestart,
  requestPreviewReload
} from '@/store/preview'
import { $activeGatewayProfile } from '@/store/profile'
import { $currentCwd } from '@/store/session'
import { $focusedRuntimeId, $focusedSessionIdentityKey } from '@/store/session-states'
import type { RpcEvent } from '@/types/hermes'

type EventHandler = (event: RpcEvent) => void

interface PreviewRoutingOptions {
  baseHandleGatewayEvent: EventHandler
  currentCwd: string
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

function asRecord(payload: unknown): Record<string, unknown> {
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
}

interface PreviewOwner {
  identity: null | string
  profile: string
  runtimeId: string
}

function focusedPreviewOwner(): PreviewOwner | null {
  const runtimeId = $focusedRuntimeId.get()

  if (!runtimeId) {
    return null
  }

  // Durable focused identity + active profile are authoritative. Runtime ids
  // are pooled per backend, so joining through $sessionStates[runtimeId] can
  // inherit a stale owner left by another profile before this draft publishes
  // its own stored id/state.
  const profile = normalizeProfileKey($activeGatewayProfile.get())
  const identity = $focusedSessionIdentityKey.get()

  return { identity, profile, runtimeId }
}

function eventBelongsToOwner(event: RpcEvent, owner: PreviewOwner): boolean {
  // Gateway boot/registry stamps every real socket event with its source
  // profile. A profileless event has only a pooled runtime id and therefore
  // cannot establish ownership safely.
  if (event.session_id !== owner.runtimeId || !event.profile) {
    return false
  }

  if (normalizeProfileKey(event.profile) !== owner.profile) {
    return false
  }

  return true
}

function ownerStillFocused(owner: PreviewOwner): boolean {
  const current = focusedPreviewOwner()

  return owner.identity
    ? current?.identity === owner.identity
    : current?.runtimeId === owner.runtimeId && current.profile === owner.profile
}

export function usePreviewRouting({ baseHandleGatewayEvent, currentCwd, requestGateway }: PreviewRoutingOptions) {
  const restartPreviewServer = useCallback(
    async (url: string, context?: string) => {
      const sessionId = $focusedRuntimeId.get()

      if (!sessionId) {
        throw new Error('No active session for background restart')
      }

      const cwd = $currentCwd.get() || currentCwd || ''

      const result = await requestGateway<{ task_id?: string }>('preview.restart', {
        context: context || undefined,
        cwd: cwd || undefined,
        session_id: sessionId,
        url
      })

      const taskId = result.task_id || ''

      if (!taskId) {
        throw new Error('Background restart did not return a task id')
      }

      beginPreviewServerRestart(taskId, url)

      return taskId
    },
    [currentCwd, requestGateway]
  )

  const handleDesktopGatewayEvent = useCallback<EventHandler>(
    event => {
      baseHandleGatewayEvent(event)
      const owner = focusedPreviewOwner()

      if (event.type === 'preview.open') {
        // Agent-driven open in response to an explicit user request ("show
        // cnn.com in the preview pane"). Honor it only for the session the user
        // is actually looking at — a background turn must not yank the pane open
        // (see desktop AGENTS.md: offer, don't hijack). That session is the
        // focused one, which is a TILE's runtime whenever a tile is fronted, not
        // the primary chat's. Routes through the same normalizer as the file
        // browser so URLs, localhost, and file paths all resolve correctly.
        const { url, label } = asRecord(event.payload)
        const target = typeof url === 'string' ? url.trim() : ''

        if (target && owner && eventBelongsToOwner(event, owner)) {
          void normalizeOrLocalPreviewTarget(target, $currentCwd.get() || currentCwd || undefined).then(resolved => {
            if (resolved && ownerStillFocused(owner)) {
              const trimmedLabel = typeof label === 'string' ? label.trim() : ''
              openPreview(trimmedLabel ? { ...resolved, label: trimmedLabel } : resolved, 'tool-result')
            }
          })
        }

        return
      }

      if (event.type === 'preview.restart.complete') {
        const { task_id, text } = asRecord(event.payload)

        if (typeof task_id === 'string' && task_id) {
          completePreviewServerRestart(task_id, typeof text === 'string' ? text : '')
        }
      } else if (event.type === 'preview.restart.progress') {
        const { task_id, text } = asRecord(event.payload)

        if (typeof task_id === 'string' && task_id) {
          progressPreviewServerRestart(task_id, typeof text === 'string' ? text : '')
        }
      }

      if (!owner || !eventBelongsToOwner(event, owner)) {
        return
      }

      // Only refresh an already-open live preview when a file changes; never
      // open one unprompted. (Preview links are surfaced from the tool row into
      // the status stack — see tool-fallback.tsx.)
      if ($previewTabs.get().some(tab => tab.target.kind === 'url') && gatewayEventCompletedFileDiff(event)) {
        requestPreviewReload()
      }
    },
    [baseHandleGatewayEvent, currentCwd]
  )

  return { handleDesktopGatewayEvent, restartPreviewServer }
}

import type { ReactNode } from 'react'

import type { SubmitTextOptions } from '@/app/session/hooks/use-prompt-actions/utils'
import type { HermesGateway } from '@/hermes'

import type { DroppedFile } from '../hooks/use-composer-actions'

export interface ContextSuggestion {
  text: string
  display: string
  meta?: string
}

export interface QuickModelOption {
  provider: string
  providerName: string
  model: string
}

export interface ChatBarState {
  model: {
    model: string
    provider: string
    canSwitch: boolean
    loading?: boolean
    quickModels?: QuickModelOption[]
    /** Reused status-bar dropdown (built with gateway + selectModel upstream). */
    modelMenuContent?: ReactNode
  }
  tools: { enabled: boolean; label: string; suggestions?: ContextSuggestion[] }
  voice: { enabled: boolean; active: boolean }
}

export type ComposerStorageMigrationKind = 'lineage' | 'new-session'

/** Explicit same-owner handoff between two opaque persisted composer keys. */
export interface ComposerStorageMigration {
  fromKey: string
  kind: ComposerStorageMigrationKind
  toKey: string
}

export interface ChatBarProps {
  actionsDisabled?: boolean
  busy: boolean
  disabled: boolean
  focusKey?: string | null
  /** Profile-qualified identity used only to invalidate session-bound callbacks.
   * Backend stored/runtime ids remain separate and are never parsed from it. */
  identityScopeKey?: string | null
  maxRecordingSeconds?: number
  state: ChatBarState
  gateway?: HermesGateway | null
  queueSessionKey?: string | null
  /** Profile-qualified local draft/attachment/queue key. Never sent to the backend. */
  storageScopeKey?: string | null
  /** One-time raw/pre-codec aliases proven to belong to storageScopeKey. */
  legacyStorageScopeKeys?: readonly (string | null | undefined)[]
  /** Explicit same-owner New Chat or lineage storage handoff. */
  storageMigration?: ComposerStorageMigration
  sessionId?: string | null
  cwd?: string | null
  onCancel: () => Promise<void> | void
  onAddContextRef?: (refText: string, label?: string, detail?: string) => void
  onAddUrl?: (url: string) => void
  onAttachImageBlob?: (blob: Blob) => Promise<boolean | void> | boolean | void
  onAttachDroppedItems?: (candidates: DroppedFile[]) => Promise<boolean | void> | boolean | void
  /** Pasted GitHub PR-comment deep link → structured review attachment.
   *  Returns true when the paste was consumed as an attachment. */
  onAttachPrCommentUrl?: (url: string) => boolean
  onPasteClipboardImage?: (opts?: { silent?: boolean }) => Promise<boolean> | void
  onPickFiles?: () => void
  onPickFolders?: () => void
  onPickImages?: () => void
  onRemoveAttachment?: (id: string) => void
  onSteer?: (text: string) => Promise<boolean> | boolean
  onSubmit: (value: string, options?: SubmitTextOptions) => Promise<boolean> | boolean
  onTranscribeAudio?: (audio: Blob) => Promise<string>
}

export type VoiceStatus = 'idle' | 'recording' | 'transcribing'

export interface VoiceActivityState {
  elapsedSeconds: number
  level: number
  status: VoiceStatus
}

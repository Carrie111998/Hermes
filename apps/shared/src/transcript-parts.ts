/** Additive, presentation-only transcript evidence carried beside legacy text. */
export type GatewayTranscriptPartKind =
  | 'audio'
  | 'clipped'
  | 'file'
  | 'image'
  | 'malformed'
  | 'reasoning'
  | 'text'
  | 'tool-call'
  | 'tool-result'
  | 'unknown'
  | (string & {})

export interface GatewayTranscriptPart {
  arguments?: unknown
  clipped?: boolean
  completed_at?: number
  content_kind?: string
  evidence?: string
  id?: string
  kind: GatewayTranscriptPartKind
  mime_type?: string
  name?: string
  reason?: string
  ref?: string
  source_type?: string
  text?: string
  timestamp?: number
  value?: unknown
}

export type GatewayTranscriptPartsMode = 'append' | 'replace' | 'seal'

/** Optional for compatibility with gateways predating ordered transcript parts. */
export interface GatewayTranscriptPartsFields {
  parts?: GatewayTranscriptPart[]
  parts_clipped?: boolean
  parts_mode?: GatewayTranscriptPartsMode
  parts_version?: number
}

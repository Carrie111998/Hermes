/**
 * Avatar Pack type definitions — the data contract for local avatar packs.
 *
 * A "pack" is a folder under `~/.hermes/avatar-packs/<id>/` containing a
 * `pack.json` manifest and per-state asset files (idle/talk/think/listen).
 * The desktop overlay's renderer can switch between the Petdex sprite system
 * and an Avatar Pack, choosing different media per agent state.
 *
 * Security: only local file paths are used. No remote URLs, no scripts.
 *
 * ── .hchar compatibility note (2026-07-28, SELECTIVE ADOPT) ──────────────────
 * The third-party `hermes-desktop-avatar` repo uses a `.hchar` zip-pack format
 * with a `character.json` manifest (schema v1). This format is **not directly
 * compatible** with Hermes Desktop's `pack.json` — but the concept maps 1:1:
 *
 *   .hchar (character.json)            →  pack.json (AvatarPackManifest)
 *   character.json: states.idle/talk/  →  pack.json: states.{idle,talk,
 *                                         think,listen}  ← extra 'think' +
 *                                         'listen' (P1 voice 4-state machine)
 *   character.json: render.transparent →  pack.json: render.transparent
 *   character.json: render.loop        →  pack.json: render.loop
 *
 * Differences worth knowing:
 *   - We expose 4 states (idle/talk/think/listen) vs the repo's 3 (idle/
 *     thinking/talking). The repo's 'thinking' maps to our 'think'; 'talking'
 *     maps to 'talk'; the new 'listen' covers manual recording (P1 voice).
 *   - We support more asset formats (webm, mp4, mov, gif, webp, png, svg) than
 *     the repo's WebP-only loop.
 *   - A future P2 converter could bridge .hchar → pack.json; today they're
 *     separate ecosystems and the integration is intentionally shallow.
 *
 * If/when a converter lands, treat both manifests as immutable canonical
 * documents — never auto-rewrite a user's `.hchar` in place.
 */

// ── Supported asset formats ──────────────────────────────────────────────────

/** Video formats that get rendered with <video>. */
export const VIDEO_EXTS = ['.webm', '.mp4', '.mov'] as const
/** Image formats that get rendered with <img>. */
export const IMAGE_EXTS = ['.gif', '.webp', '.png', '.svg'] as const
/** All valid asset extensions. */
export const ASSET_EXTS = [...VIDEO_EXTS, ...IMAGE_EXTS] as const

export type VideoExt = (typeof VIDEO_EXTS)[number]
export type ImageExt = (typeof IMAGE_EXTS)[number]
export type AssetExt = VideoExt | ImageExt

export function isVideoExt(ext: string): ext is VideoExt {
  return (VIDEO_EXTS as readonly string[]).includes(ext.toLowerCase())
}

export function isImageExt(ext: string): ext is ImageExt {
  return (IMAGE_EXTS as readonly string[]).includes(ext.toLowerCase())
}

export function isAssetExt(ext: string): boolean {
  return isVideoExt(ext) || isImageExt(ext)
}

// ── Avatar states ────────────────────────────────────────────────────────────

/**
 * The four avatar states. Maps to agent activity:
 *  - idle: pet is at rest (no activity)
 *  - talking: agent is responding / streaming
 *  - thinking: agent is reasoning / tool running
 *  - listening: awaiting user input
 */
export type AvatarState = 'idle' | 'talk' | 'think' | 'listen'

export const ALL_AVATAR_STATES: AvatarState[] = ['idle', 'talk', 'think', 'listen']

export const AVATAR_STATE_LABELS: Record<AvatarState, string> = {
  idle: 'Idle',
  talk: 'Talking',
  think: 'Thinking',
  listen: 'Listening'
}

// ── pack.json schema ─────────────────────────────────────────────────────────

export interface PackRenderConfig {
  /** Enable transparent alpha channel (best-effort; video format must support). */
  transparent?: boolean
  /** Whether assets should loop (default true for animation/video). */
  loop?: boolean
}

/** Per-state asset file mapping. The file is relative to the pack folder. */
export type StateAssets = Partial<Record<AvatarState, string>>

export interface AvatarPackManifest {
  /** Pack ID — must match the folder name. */
  id: string
  /** Human-readable display name. */
  name: string
  /** Semver-ish version string. */
  version: string
  /** Pack type — 'character' for this P1 implementation. */
  type: 'character'
  /** Per-state asset filenames relative to the pack folder. */
  states: StateAssets
  /** Render configuration. */
  render?: PackRenderConfig
  /** Default state when no activity signal is present. */
  defaultState?: AvatarState
}

// ── Resolved pack (after validation + path resolution) ───────────────────────

export interface ResolvedStateAsset {
  /** The state this asset belongs to. */
  state: AvatarState
  /** Absolute filesystem path to the asset file. */
  filePath: string
  /** Original filename from the manifest (relative to pack dir). */
  filename: string
  /** File extension including the dot, lowercase. */
  ext: string
  /** Whether this is a video format. */
  isVideo: boolean
  /** URL for use in <video src> or <img src> via the hermes-media protocol. */
  url: string
}

export interface ResolvedAvatarPack {
  id: string
  name: string
  version: string
  type: 'character'
  /** Folder path containing the pack. */
  folderPath: string
  /** Path to thumb.png (may not exist). */
  thumbPath: string | null
  /** Resolved per-state assets, only including states that have valid files. */
  assets: Partial<Record<AvatarState, ResolvedStateAsset>>
  /** Default state (falls back to 'idle'). */
  defaultState: AvatarState
  /** Render config. */
  render: PackRenderConfig
  /** Validation errors/warnings collected during load. */
  warnings: string[]
}

// ── IPC payloads ─────────────────────────────────────────────────────────────

export interface AvatarPackListResult {
  packs: AvatarPackSummary[]
  folderPath: string
}

export interface AvatarPackSummary {
  id: string
  name: string
  version: string
  /** Whether the pack has at least an idle asset. */
  hasIdle: boolean
  /** Number of states with valid assets. */
  stateCount: number
}

// ── Renderer type: Petdex sprite vs Avatar Pack ──────────────────────────────

export type AvatarRendererType = 'petdex' | 'avatar-pack'

export const RENDERER_TYPE_LABELS: Record<AvatarRendererType, string> = {
  petdex: 'Petdex sprite',
  'avatar-pack': 'Avatar Pack'
}

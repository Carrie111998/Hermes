/**
 * Avatar Pack Renderer — renders per-state video/image assets from a local
 * avatar pack folder, replacing the Petdex canvas sprite when the renderer
 * type is set to 'avatar-pack'.
 *
 * For video assets (.webm, .mp4, .mov): uses <video autoplay loop muted playsInline>.
 * For image assets (.gif, .webp, .png, .svg): uses <img>.
 *
 * Transparent alpha is supported via the pack's render.transparent flag
 * (best-effort — the asset format must actually have an alpha channel).
 */

import { memo, useEffect, useMemo, useRef, useState } from 'react'

import {
  type AvatarState,
  type ResolvedAvatarPack,
  type ResolvedStateAsset
} from '@/store/avatar-pack-types'

export interface AvatarPackRendererProps {
  /** The resolved avatar pack to render. */
  pack: ResolvedAvatarPack
  /** The state to render (idle/talk/think/listen). Falls back to defaultState. */
  state: AvatarState
  /** Scale multiplier (same as PetSprite's scale). */
  scale: number
  /** Opacity (0-1). */
  opacity: number
}

/**
 * Pick the best available asset for the given state. If the exact state
 * isn't available, falls back to idle, then to the first available state.
 */
function pickAsset(
  pack: ResolvedAvatarPack,
  state: AvatarState
): ResolvedStateAsset | null {
  const exact = pack.assets[state]

  if (exact) {
    return exact
  }

  // Fall back to idle if the requested state isn't available
  if (state !== 'idle' && pack.assets.idle) {
    return pack.assets.idle
  }

  // Last resort: first available state
  for (const s of ['idle', 'talk', 'think', 'listen'] as AvatarState[]) {
    if (pack.assets[s]) {
      return pack.assets[s]!
    }
  }

  return null
}

function AvatarPackRendererImpl({
  pack,
  state,
  scale,
  opacity
}: AvatarPackRendererProps) {
  const asset = useMemo(() => pickAsset(pack, state), [pack, state])

  const containerRef = useRef<HTMLDivElement | null>(null)

  // Track natural dimensions for sizing
  const [naturalSize, setNaturalSize] = useState<{ w: number; h: number } | null>(null)

  // Reset size when asset changes
  useEffect(() => {
    setNaturalSize(null)
  }, [asset?.filePath])

  const displaySize = useMemo(() => {
    if (!naturalSize) {
      // Default size before we know the asset's natural dimensions.
      // Matches the Petdex default frame size (192×208) so the overlay window
      // sizing math stays compatible.
      return { w: 192, h: 208 }
    }

    return {
      w: Math.round(naturalSize.w * scale),
      h: Math.round(naturalSize.h * scale)
    }
  }, [naturalSize, scale])

  if (!asset) {
    // No assets at all — show a minimal placeholder
    return (
      <div
        ref={containerRef}
        style={{
          alignItems: 'center',
          background: 'transparent',
          color: 'var(--ui-text-quaternary)',
          display: 'flex',
          fontSize: 10,
          height: 100,
          justifyContent: 'center',
          opacity,
          width: 100
        }}
      >
        ?
      </div>
    )
  }

  const onLoadSize = (e: React.SyntheticEvent<HTMLImageElement | HTMLVideoElement>) => {
    const target = e.currentTarget
    const w = 'videoWidth' in target ? target.videoWidth : target.naturalWidth
    const h = 'videoHeight' in target ? target.videoHeight : target.naturalHeight

    if (w && h && w > 0 && h > 0) {
      setNaturalSize({ h, w })
    }
  }

  const commonStyle: React.CSSProperties = {
    display: 'block',
    height: displaySize.h,
    objectFit: 'contain',
    opacity,
    width: displaySize.w
  }

  if (asset.isVideo) {
    return (
      <div ref={containerRef} style={{ lineHeight: 0 }}>
        <video
          autoPlay
          loop={pack.render.loop !== false}
          muted
          onLoadedMetadata={onLoadSize}
          playsInline
          src={asset.url}
          style={commonStyle}
        />
      </div>
    )
  }

  return (
    <div ref={containerRef} style={{ lineHeight: 0 }}>
      <img
        alt={`${pack.name} — ${state}`}
        draggable={false}
        onLoad={onLoadSize}
        src={asset.url}
        style={commonStyle}
      />
    </div>
  )
}

export const AvatarPackRenderer = memo(AvatarPackRendererImpl)

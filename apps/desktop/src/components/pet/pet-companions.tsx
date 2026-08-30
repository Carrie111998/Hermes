import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { useOnProfileSwitch } from '@/app/hooks/use-on-profile-switch'
import { persistString, storedString } from '@/lib/storage'
import { $gatewayState } from '@/store/session'
import {
  $petAtRest,
  $petCompanions,
  $petInfo,
  flashPetActivity as flashPetActivityStore,
  type PetInfo,
  petProfile,
  setPetCompanions
} from '@/store/pet'
import { isSecondaryWindow } from '@/store/windows'

import { petInfoPollIntervalMs } from './pet-info-poll'
import { PetSprite } from './pet-sprite'
import { $petOverlayActive } from '@/store/pet-overlay'

// One shared localStorage prefix, keyed per pet slug so each companion holds its
// own spot (and a removed pet's stale spot is ignored on next load).
const COMPANION_POS_KEY = 'hermes.desktop.pet-companion.v1'
const NOMINAL_PET_PX = 96

interface Point {
  x: number
  y: number
}

function clampPoint(x: number, y: number, w: number, h: number): Point {
  return {
    x: Math.min(Math.max(0, x), Math.max(0, (window.innerWidth || 800) - w)),
    y: Math.min(Math.max(0, y), Math.max(0, (window.innerHeight || 600) - h))
  }
}

// Same inward-facing rule as FloatingPet: mirror when the pet's center sits on
// the left half so it always faces the content / its companion pair.
function facing(leftX: number, petW: number): string {
  return leftX + petW / 2 < (window.innerWidth || 800) / 2 ? 'scaleX(-1)' : 'none'
}

function posKey(slug: string): string {
  return `${COMPANION_POS_KEY}.${slug}`
}

/**
 * A second floating mascot. Renders beside the primary pet and reacts to the
 * same activity state (via the shared `$petState` inside `PetSprite`), so the
 * pair reads as companions. Draggable + clamped, facing inner.
 */
function CompanionPet({ info, index }: { info: PetInfo; index: number }) {
  const [position, setPosition] = useState<Point>(() => loadPosition(info.slug ?? '', index))
  const containerRef = useRef<HTMLDivElement | null>(null)
  const spriteWrapRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ dx: number; dy: number; x: number; y: number } | null>(null)

  const petW = (info.frameW ?? 192) * (info.scale ?? 0.33)
  const petH = (info.frameH ?? 208) * (info.scale ?? 0.33)

  const clamp = useCallback(
    ({ x, y }: Point): Point => clampPoint(x, y, petW, petH),
    [petW, petH]
  )

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const el = containerRef.current
    if (!el) {
      return
    }
    const rect = el.getBoundingClientRect()
    dragRef.current = { dx: e.clientX - rect.left, dy: e.clientY - rect.top, x: rect.left, y: rect.top }
    el.setPointerCapture(e.pointerId)
    el.style.cursor = 'grabbing'
  }, [])

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current
      const el = containerRef.current
      if (!drag || !el) {
        return
      }
      const next = clamp({ x: e.clientX - drag.dx, y: e.clientY - drag.dy })
      drag.x = next.x
      drag.y = next.y
      el.style.left = `${next.x}px`
      el.style.top = `${next.y}px`
      if (spriteWrapRef.current) {
        spriteWrapRef.current.style.transform = facing(next.x, petW)
      }
    },
    [clamp, petW]
  )

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    const drag = dragRef.current
    if (drag) {
      dragRef.current = null
      const committed = { x: drag.x, y: drag.y }
      setPosition(committed)
      if (info.slug) {
        persistString(posKey(info.slug), JSON.stringify(committed))
      }
    }
    const el = containerRef.current
    if (el) {
      el.style.cursor = 'grab'
      el.releasePointerCapture?.(e.pointerId)
    }
  }, [info.slug])

  // Re-clamp + persist when the viewport or size changes, so a companion is
  // never stranded or cropped.
  useEffect(() => {
    const reclamp = () =>
      setPosition(prev => {
        const next = clamp(prev)
        if (next.x === prev.x && next.y === prev.y) {
          return prev
        }
        if (info.slug) {
          persistString(posKey(info.slug), JSON.stringify(next))
        }
        return next
      })
    reclamp()
    window.addEventListener('resize', reclamp)
    return () => window.removeEventListener('resize', reclamp)
  }, [clamp, info.slug])

  if (!info.enabled || !info.spritesheetBase64) {
    return null
  }

  return (
    <div
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      ref={containerRef}
      style={{
        cursor: 'grab',
        left: position.x,
        pointerEvents: 'auto',
        position: 'fixed',
        top: position.y,
        touchAction: 'none',
        userSelect: 'none',
        zIndex: 61
      }}
    >
      <div
        ref={spriteWrapRef}
        style={{ lineHeight: 0, transform: facing(position.x, petW), transformOrigin: 'bottom center' }}
      >
        <PetSprite info={info} />
      </div>
    </div>
  )
}

function loadPosition(slug: string, index: number): Point {
  try {
    const raw = storedString(posKey(slug))
    if (raw) {
      const parsed = JSON.parse(raw) as Point
      if (typeof parsed.x === 'number' && typeof parsed.y === 'number') {
        return clampPoint(parsed.x, parsed.y, NOMINAL_PET_PX, NOMINAL_PET_PX)
      }
    }
  } catch {
    // fall through to default
  }
  // Default: line up to the right of the primary's lower-left spot.
  return clampPoint(24 + 150 + index * 150, (window.innerHeight || 600) - 220, NOMINAL_PET_PX, NOMINAL_PET_PX)
}

/**
 * Renders every extra pet from the ``display.pet.pets`` roster alongside the
 * primary floating mascot, and gives the pair a gentle "play together" beat:
 * while the agent is at rest (idle) and at least one companion is present,
 * both mascots share a short playful jump on a slow, jittered interval — they
 * visibly play as a pair.
 *
 * Primary pet stays owned by ``FloatingPet`` (``pet.info``). This component
 * fetches ``pet.info.list`` and drops the primary slug so the two never
 * duplicate. Single-pet rosters render nothing and change nothing.
 */
export function PetCompanions() {
  const { requestGateway } = useGatewayRequest()
  const gatewayState = useStore($gatewayState)
  const info = useStore($petInfo)
  const companions = useStore($petCompanions)
  const atRest = useStore($petAtRest)
  const overlayActive = useStore($petOverlayActive)

  // Fetch the roster on connect + slow backstop poll, mirroring FloatingPet.
  useEffect(() => {
    if (gatewayState !== 'open') {
      return
    }
    let cancelled = false

    const pull = async () => {
      try {
        const held = $petCompanions.get()
        const knownRevisions: Record<string, string> = {}
        for (const p of held) {
          if (p.enabled && p.spritesheetBase64 && p.slug && p.spritesheetRevision) {
            knownRevisions[p.slug] = p.spritesheetRevision
          }
        }
        const res = await requestGateway<{
          enabled: boolean
          pets?: Array<PetInfo & { spritesheetUnchanged?: boolean }>
        }>('pet.info.list', {
          knownRevisions,
          profile: petProfile()
        })
        if (cancelled || !res || !res.enabled) {
          return
        }
        const primarySlug = info.slug
        const extras = (res.pets ?? [])
          .filter(p => p.enabled && p.spritesheetBase64 && p.slug && p.slug !== primarySlug)
          .map(p => {
            // Send-once: keep bytes we already hold for an unchanged sheet.
            const current = held.find(c => c.slug === p.slug)
            if (p.spritesheetUnchanged && current?.spritesheetBase64 && !p.spritesheetBase64) {
              return { ...p, spritesheetBase64: current.spritesheetBase64 }
            }
            return p
          })
        setPetCompanions(extras)
      } catch {
        // cosmetic feature — never surface gateway errors
      }
    }

    const pullIfVisible = () => {
      if (document.visibilityState === 'visible') {
        void pull()
      }
    }

    void pull()
    const timer = window.setInterval(pullIfVisible, petInfoPollIntervalMs(false, !!$petInfo.get().enabled))
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [gatewayState, info.slug, requestGateway])

  // Pets are per-profile: reset the companions on profile switch.
  useOnProfileSwitch(() => setPetCompanions([]))

  // Only the primary window owns floating mascots.
  const secondary = isSecondaryWindow()
  const shouldHide = secondary || overlayActive || companions.length === 0

  // "Play together": while idle with a companion nearby, periodically share a
  // short jump so both mascots react in unison — a visible pair beat.
  const beatRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (shouldHide || !atRest) {
      if (beatRef.current) {
        clearTimeout(beatRef.current)
        beatRef.current = null
      }
      return
    }
    const schedule = () => {
      if (document.visibilityState === 'hidden') {
        return
      }
      const delay = 18000 + Math.random() * 12000
      beatRef.current = setTimeout(() => {
        flashPetActivityStore({ celebrate: true }, 1100)
        schedule()
      }, delay)
    }
    schedule()
    return () => {
      if (beatRef.current) {
        clearTimeout(beatRef.current)
        beatRef.current = null
      }
    }
  }, [shouldHide, atRest])

  if (shouldHide) {
    return null
  }

  return (
    <>
      {companions.map((c, i) => (
        <CompanionPet key={c.slug ?? i} info={c} index={i} />
      ))}
    </>
  )
}

import { useEffect } from 'react'

import { $petMotion, $petRoamDir } from '@/store/pet'

import { dwellMs, PAUSE_DWELL, pickStrollTarget } from './roam-behavior'
import { groundTop, type Ledge, overlapsX, resolveLedge } from './roam-geometry'

export interface OverlayBounds {
  height: number
  width: number
  x: number
  y: number
}

export interface OverlayRoamEnvironment {
  windows: OverlayBounds[]
  workArea: OverlayBounds
}

interface OverlayRoamOptions {
  enabled: boolean
  isInteracting: () => boolean
  loopMs: number
  petH: number
  petW: number
  /** Changing this restarts planning immediately (for example after a drag). */
  replanKey?: number
}

interface Span {
  left: number
  right: number
}

const PET_FOOT_INSET_PX = 24
const MIN_TRAVEL_PX = 48
const GRAVITY_PX_S2 = 2600
const MAX_DT_S = 0.05
const HOP_CHANCE = 0.35
const BASE_HOP_DURATION_MS = 800
const PAUSE_POLL_MS = 250
const MIN_LEDGE_REACH_PX = 180
const MIN_HOP_TRAVEL_PX = 32

type Phase = 'fall' | 'hop' | 'walk'

/** Keep lateral hops compact, regardless of how wide the desktop ledge is. */
export const overlayHopMaxTravel = (petW: number): number => Math.max(80, petW * 1.5)

/**
 * The portion of a destination ledge reachable by one compact sideways hop.
 * A null range means the pet should walk closer before trying that ledge.
 */
export function overlayHopRange(from: Ledge, to: Ledge, fromX: number, petW: number): Ledge | null {
  const maxTravel = overlayHopMaxTravel(petW)
  const left = Math.max(from.left, to.left, fromX - maxTravel)
  const right = Math.min(from.right, to.right, fromX + maxTravel)

  return right > left + 2 ? { left, right, y: to.y } : null
}

/** Choose a visible hop landing point without crossing most of a wide ledge. */
export function pickOverlayHopTarget(range: Ledge, fromX: number, rng: () => number = Math.random): number {
  const leftRoom = Math.max(0, fromX - range.left)
  const rightRoom = Math.max(0, range.right - fromX)
  let direction: -1 | 1

  if (leftRoom < MIN_HOP_TRAVEL_PX) {
    direction = 1
  } else if (rightRoom < MIN_HOP_TRAVEL_PX) {
    direction = -1
  } else {
    direction = rng() < 0.5 ? -1 : 1
  }

  const available = direction < 0 ? leftRoom : rightRoom

  if (available <= 0) {
    return Math.max(range.left, Math.min(range.right, fromX))
  }

  const minimum = Math.min(MIN_HOP_TRAVEL_PX, available)
  const distance = minimum + rng() * (available - minimum)

  return fromX + direction * distance
}

/** A taller arc, with extra lift when the landing surface is below the start. */
export function overlayHopArcHeight(petH: number, deltaY: number): number {
  const baseLift = Math.max(88, petH * 1.5)

  return Math.min(300, baseLift + Math.max(0, deltaY) * 0.55)
}

const clipTo = (bounds: OverlayBounds, area: OverlayBounds): OverlayBounds | null => {
  const x = Math.max(bounds.x, area.x)
  const y = Math.max(bounds.y, area.y)
  const right = Math.min(bounds.x + bounds.width, area.x + area.width)
  const bottom = Math.min(bounds.y + bounds.height, area.y + area.height)

  return right > x && bottom > y ? { height: bottom - y, width: right - x, x, y } : null
}

const subtract = (spans: Span[], cut: Span): Span[] =>
  spans.flatMap(span => {
    if (cut.right <= span.left || cut.left >= span.right) {
      return [span]
    }

    const pieces: Span[] = []

    if (cut.left > span.left) {
      pieces.push({ left: span.left, right: Math.min(span.right, cut.left) })
    }

    if (cut.right < span.right) {
      pieces.push({ left: Math.max(span.left, cut.right), right: span.right })
    }

    return pieces
  })

/**
 * Convert front-to-back app-window rectangles into visible horizontal ledges.
 * A front window subtracts the part of every lower window edge it covers, so
 * the pet never appears to stand on an edge hidden behind another app. The
 * work-area bottom is always the first/fallback ledge.
 */
export function overlayRoamLedges(
  { windows, workArea }: OverlayRoamEnvironment,
  overlayWidth: number,
  petW: number,
  petH: number
): Ledge[] {
  const spriteOffsetX = Math.max(0, (overlayWidth - petW) / 2)

  const toLedge = (span: Span, y: number): Ledge => ({
    left: span.left - spriteOffsetX,
    right: span.right - spriteOffsetX - petW,
    y
  })

  const ledges: Ledge[] = [
    toLedge({ left: workArea.x, right: workArea.x + workArea.width }, workArea.y + workArea.height)
  ]

  const occluders: OverlayBounds[] = []

  for (const windowBounds of windows) {
    const clipped = clipTo(windowBounds, workArea)

    if (!clipped) {
      continue
    }

    const surfaceY = windowBounds.y
    let visible: Span[] = [{ left: clipped.x, right: clipped.x + clipped.width }]

    for (const front of occluders) {
      if (front.y <= surfaceY && front.y + front.height > surfaceY) {
        visible = subtract(visible, { left: front.x, right: front.x + front.width })
      }
    }

    // A window flush with the display top has no room for the sprite above it;
    // a taskbar/shell surface at the work-area bottom duplicates the floor.
    if (surfaceY - petH >= workArea.y && surfaceY < workArea.y + workArea.height - 8) {
      for (const span of visible) {
        const ledge = toLedge(span, surfaceY)

        if (ledge.right > ledge.left + 2) {
          ledges.push(ledge)
        }
      }
    }

    occluders.push(clipped)
  }

  return ledges
}

/** Top coordinate where the overlay's visible feet meet the desktop floor. */
export function overlayGroundY(workArea: OverlayBounds, windowHeight: number): number {
  return groundTop(
    { left: workArea.x, right: workArea.x + workArea.width, y: workArea.y + workArea.height },
    Math.max(1, windowHeight - PET_FOOT_INSET_PX)
  )
}

/** Keep the visible sprite on-screen while allowing transparent window padding off-screen. */
export function clampOverlayRoamBounds(
  workArea: OverlayBounds,
  bounds: OverlayBounds,
  petW: number = bounds.width,
  petH: number = bounds.height
): OverlayBounds {
  const spriteOffsetX = Math.max(0, (bounds.width - petW) / 2)
  const minX = workArea.x - spriteOffsetX
  const maxX = Math.max(minX, workArea.x + workArea.width - spriteOffsetX - petW)
  const spriteOffsetY = Math.max(0, bounds.height - PET_FOOT_INSET_PX - petH)
  const maxY = overlayGroundY(workArea, bounds.height)
  const minY = Math.min(maxY, workArea.y - spriteOffsetY)

  return {
    ...bounds,
    x: Math.max(minX, Math.min(maxX, bounds.x)),
    y: Math.max(minY, Math.min(maxY, bounds.y))
  }
}

/**
 * Wander the entire transparent BrowserWindow across the desktop. The usable
 * display bottom and visible top edges of other apps are platformer surfaces:
 * the pet walks along them, falls when a surface disappears, and hops between
 * overlapping ledges. Enumeration failure degrades to the display floor.
 */
export function usePetOverlayRoam({
  enabled,
  isInteracting,
  loopMs,
  petH,
  petW,
  replanKey = 0
}: OverlayRoamOptions): void {
  useEffect(() => {
    const api = window.hermesDesktop?.petOverlay

    if (!enabled || !api?.roamEnvironment) {
      $petMotion.set(null)
      $petRoamDir.set(0)

      return
    }

    let stopped = false
    let raf = 0
    let timer = 0
    let lastFrame = 0
    let lastPaint = 0
    let current: OverlayBounds | null = null
    let ledges: Ledge[] = []
    let currentLedge: Ledge | null = null
    let targetLedge: Ledge | null = null
    let phase: Phase = 'walk'
    let targetX = 0
    let startX = 0
    let startY = 0
    let phaseStarted = 0
    let fallVelocity = 0
    let hopHeight = 0
    let hopDuration = BASE_HOP_DURATION_MS
    const speed = (petW * 0.8) / Math.max(0.25, loopMs / 1000)

    const footHeight = (): number => Math.max(1, (current?.height ?? 1) - PET_FOOT_INSET_PX)
    const restY = (ledge: Ledge): number => groundTop(ledge, footHeight())

    const signal = (pose: 'jump' | 'run' | null, dir: -1 | 0 | 1 = 0) => {
      $petMotion.set(pose)
      $petRoamDir.set(pose === 'run' ? dir : 0)
    }

    const remember = () => {
      if (current) {
        api.control({ bounds: current, type: 'bounds' })
      }
    }

    const schedulePlan = () => {
      signal(null)
      remember()
      timer = window.setTimeout(plan, dwellMs(PAUSE_DWELL))
    }

    const syncFromWindow = () => {
      current = {
        height: window.outerHeight,
        width: window.outerWidth,
        x: window.screenX,
        y: window.screenY
      }
    }

    const paint = (now: number, force = false) => {
      if (current && (force || now - lastPaint >= 1000 / 30)) {
        lastPaint = now
        api.setBounds(current)
      }
    }

    const beginHop = (ledge: Ledge, x: number, now: number) => {
      if (!current) {
        return
      }

      targetLedge = ledge
      targetX = x
      startX = current.x
      startY = current.y
      phase = 'hop'
      phaseStarted = now
      const deltaY = restY(ledge) - startY
      const vertical = Math.abs(deltaY)

      hopHeight = overlayHopArcHeight(petH, deltaY)
      hopDuration = Math.min(1300, BASE_HOP_DURATION_MS + vertical * 0.7)
      signal('jump')
    }

    const beginMotion = (now: number): boolean => {
      if (!current || ledges.length === 0) {
        return false
      }

      currentLedge = resolveLedge(ledges, current.x, current.y, footHeight())
      const restingY = restY(currentLedge)

      if (current.y < restingY - 1) {
        targetLedge = currentLedge
        phase = 'fall'
        fallVelocity = 0
        signal('jump')

        return true
      }

      if (current.y > restingY + 1) {
        beginHop(currentLedge, current.x, now)

        return true
      }

      current.y = restingY
      const verticalReach = Math.max(MIN_LEDGE_REACH_PX, petH * 2.75)
      const fromLedge = currentLedge
      const fromX = current.x

      const reachable = ledges.flatMap(ledge => {
        if (
          ledge === fromLedge ||
          !overlapsX(fromLedge, ledge) ||
          Math.abs(restY(ledge) - restingY) > verticalReach
        ) {
          return []
        }

        const range = overlayHopRange(fromLedge, ledge, fromX, petW)

        return range ? [{ ledge, range }] : []
      })

      if (Math.random() < HOP_CHANCE) {
        const next = reachable[Math.floor(Math.random() * reachable.length)]
        const range = next?.range ?? overlayHopRange(fromLedge, fromLedge, fromX, petW)

        if (range) {
          beginHop(next?.ledge ?? fromLedge, pickOverlayHopTarget(range, fromX), now)

          return true
        }
      }

      targetX = pickStrollTarget(currentLedge, current.x)

      if (Math.abs(targetX - current.x) < Math.min(MIN_TRAVEL_PX, (currentLedge.right - currentLedge.left) / 2)) {
        targetX = current.x < (currentLedge.left + currentLedge.right) / 2 ? currentLedge.right : currentLedge.left
      }

      const dir = targetX > current.x ? 1 : targetX < current.x ? -1 : 0

      if (dir === 0) {
        return false
      }

      targetLedge = currentLedge
      phase = 'walk'
      signal('run', dir)

      return true
    }

    const step = (now: number) => {
      raf = 0

      if (stopped || !current || !targetLedge) {
        return
      }

      if (isInteracting()) {
        syncFromWindow()
        signal(null)
        timer = window.setTimeout(plan, PAUSE_POLL_MS)

        return
      }

      const dt = Math.min(MAX_DT_S, Math.max(0, now - lastFrame) / 1000)
      const targetY = restY(targetLedge)
      lastFrame = now

      if (phase === 'fall') {
        fallVelocity += GRAVITY_PX_S2 * dt
        current.y = Math.min(targetY, current.y + fallVelocity * dt)
      } else if (phase === 'hop') {
        const progress = Math.min(1, (now - phaseStarted) / hopDuration)
        current.x = startX + (targetX - startX) * progress
        current.y = startY + (targetY - startY) * progress - Math.sin(Math.PI * progress) * hopHeight
      } else {
        const remaining = targetX - current.x
        const distance = speed * dt
        current.x = Math.abs(remaining) <= distance ? targetX : current.x + Math.sign(remaining) * distance
        current.y = targetY
      }

      const arrived =
        (phase === 'fall' && current.y >= targetY) ||
        (phase === 'hop' && now - phaseStarted >= hopDuration) ||
        (phase === 'walk' && current.x === targetX)

      if (arrived) {
        current.x = phase === 'fall' ? current.x : targetX
        current.y = targetY
        paint(now, true)
        schedulePlan()
      } else {
        paint(now)
        raf = window.requestAnimationFrame(step)
      }
    }

    const plan = async () => {
      timer = 0

      if (stopped) {
        return
      }

      if (isInteracting()) {
        syncFromWindow()
        timer = window.setTimeout(plan, PAUSE_POLL_MS)

        return
      }

      const environment = await api.roamEnvironment()

      if (stopped) {
        return
      }

      if (!environment) {
        timer = window.setTimeout(plan, 1000)

        return
      }

      const raw = {
        height: window.outerHeight,
        width: window.outerWidth,
        x: window.screenX,
        y: window.screenY
      }

      current = clampOverlayRoamBounds(environment.workArea, raw, petW, petH)
      ledges = overlayRoamLedges(environment, current.width, petW, petH)
      const now = performance.now()

      if (!beginMotion(now)) {
        schedulePlan()

        return
      }

      lastFrame = now
      raf = window.requestAnimationFrame(step)
    }

    void plan()

    return () => {
      stopped = true
      window.clearTimeout(timer)
      window.cancelAnimationFrame(raf)
      signal(null)
    }
  }, [enabled, isInteracting, loopMs, petH, petW, replanKey])
}

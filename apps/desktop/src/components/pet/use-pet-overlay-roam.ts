import { useEffect } from 'react'

import { createRendererLoopPauseController } from '@/lib/renderer-loop-pause'
import { $petMotion, $petRoamDir } from '@/store/pet'
import type { PetOverlayBounds } from '@/store/pet-overlay'

import { chooseMove, dwellMs, PAUSE_DWELL, pickStrollTarget } from './roam-behavior'

// Match the in-window gait: one sprite body covers roughly this much ground per
// animation loop, so the feet read as walking instead of sliding.
const STRIDE_PER_LOOP = 0.8
const ARRIVE_EPS = 1.5
const MAX_DT_S = 0.05
const PAUSE_POLL_MS = 250
const DROP_SETTLE_MS = 90

type Phase = 'pause' | 'planning' | 'walk'

const rand = (min: number, max: number): number => min + Math.random() * (max - min)
const signDir = (n: number): -1 | 0 | 1 => (n > 0 ? 1 : n < 0 ? -1 : 0)

export function desktopRoamRange(workArea: PetOverlayBounds, windowWidth: number): { left: number; right: number } {
  const left = workArea.x

  return { left, right: Math.max(left, workArea.x + workArea.width - windowWidth) }
}

interface PetOverlayRoamOptions {
  /** Roam preference + active pet + idle agent + closed quick composer. */
  enabled: boolean
  /** True while the user owns the window position through a pointer drag. */
  isInteracting: () => boolean
  /** Sprite animation loop duration, used to pace the walking stride. */
  loopMs: number
  /** On-screen sprite width rather than the padded overlay-window width. */
  petW: number
}

/**
 * Wander the popped-out pet across its current display while preserving the
 * height chosen by the user. Electron owns multi-display geometry, so the hook
 * asks main for the overlay's current work area before every stroll instead of
 * guessing from browser `screen.availWidth` (which has no reliable origin on a
 * secondary display).
 */
export function usePetOverlayRoam({ enabled, isInteracting, loopMs, petW }: PetOverlayRoamOptions): void {
  useEffect(() => {
    const api = window.hermesDesktop?.petOverlay

    if (!enabled || !api) {
      $petMotion.set(null)
      $petRoamDir.set(0)

      return
    }

    const walkSpeedPxS = (petW * STRIDE_PER_LOOP) / (loopMs / 1000)
    let curX = window.screenX
    let curY = window.screenY
    let targetX = curX
    let phase: Phase = 'pause'
    let pauseUntil = performance.now() + rand(400, 1200)
    let last = performance.now()
    let raf = 0
    let pauseTimer = 0
    let stopped = false
    let planNonce = 0
    let pauseController: ReturnType<typeof createRendererLoopPauseController> | null = null

    const signal = (dir: -1 | 0 | 1) => {
      $petMotion.set(dir === 0 ? null : 'run')
      $petRoamDir.set(dir)
    }

    const applyBounds = () => {
      api.setBounds({
        height: window.outerHeight,
        width: window.outerWidth,
        x: Math.round(curX),
        y: Math.round(curY)
      })
    }

    const rendererPaused = () => pauseController?.isPaused() ?? document.visibilityState === 'hidden'

    const clearScheduled = () => {
      if (raf !== 0) {
        window.cancelAnimationFrame(raf)
        raf = 0
      }

      if (pauseTimer !== 0) {
        window.clearTimeout(pauseTimer)
        pauseTimer = 0
      }
    }

    const beginPause = (now: number, delay = dwellMs(PAUSE_DWELL)) => {
      phase = 'pause'
      pauseUntil = now + delay
      signal(0)
    }

    const schedule = (now = performance.now()) => {
      if (stopped || rendererPaused() || raf !== 0 || pauseTimer !== 0 || phase === 'planning') {
        return
      }

      if (phase === 'pause') {
        const delay = Math.max(0, pauseUntil - now)

        if (delay > 0) {
          pauseTimer = window.setTimeout(
            () => {
              pauseTimer = 0
              step(performance.now())
            },
            Math.min(delay, PAUSE_POLL_MS)
          )

          return
        }
      }

      raf = window.requestAnimationFrame(step)
    }

    const planNext = () => {
      phase = 'planning'
      const nonce = ++planNonce

      void api
        .getWorkArea()
        .then(workArea => {
          if (stopped || nonce !== planNonce) {
            return
          }

          // Re-seed from the native window: a drag may have moved the overlay to
          // another display while the roam loop was yielding.
          curX = window.screenX
          curY = window.screenY

          if (!workArea || chooseMove(false) === 'rest') {
            beginPause(performance.now())
            schedule()

            return
          }

          const range = desktopRoamRange(workArea, window.outerWidth)
          curX = Math.min(Math.max(range.left, curX), range.right)
          targetX = pickStrollTarget({ left: range.left, right: range.right, y: curY }, curX)

          if (Math.abs(targetX - curX) <= ARRIVE_EPS) {
            beginPause(performance.now())
          } else {
            phase = 'walk'
            last = performance.now()
            signal(signDir(targetX - curX))
          }

          schedule()
        })
        .catch(() => {
          if (stopped || nonce !== planNonce) {
            return
          }

          // The overlay may be closing while IPC resolves. Treat geometry
          // failure as another quiet beat instead of leaking a rejection.
          beginPause(performance.now())
          schedule()
        })
    }

    const step = (now: number) => {
      raf = 0
      pauseTimer = 0

      if (stopped || rendererPaused()) {
        signal(0)

        return
      }

      const dt = Math.min(MAX_DT_S, Math.max(0, (now - last) / 1000))
      last = now

      if (isInteracting()) {
        ++planNonce
        curX = window.screenX
        curY = window.screenY
        beginPause(now, DROP_SETTLE_MS)
        schedule(now)

        return
      }

      if (phase === 'pause') {
        if (now >= pauseUntil) {
          planNext()
        }
      } else if (phase === 'walk') {
        const remaining = targetX - curX
        const stepDist = walkSpeedPxS * dt

        if (Math.abs(remaining) <= Math.max(ARRIVE_EPS, stepDist)) {
          curX = targetX
          applyBounds()
          beginPause(now)
        } else {
          curX += Math.sign(remaining) * stepDist
          applyBounds()
        }
      }

      schedule(now)
    }

    const handleVisibilityChange = () => {
      clearScheduled()
      last = performance.now()

      if (rendererPaused()) {
        signal(0)

        return
      }

      schedule(last)
    }

    // The overlay is intentionally non-activating, so blur must not stop its
    // desktop behavior; hidden/minimized native state still pauses the loop.
    pauseController = createRendererLoopPauseController(handleVisibilityChange, { pauseWhenUnfocused: false })
    schedule()

    return () => {
      stopped = true
      ++planNonce
      clearScheduled()
      pauseController?.dispose()
      signal(0)
    }
  }, [enabled, isInteracting, loopMs, petW])
}

/**
 * Hermes, on the canvas.
 *
 * This is the app's own chat — `SessionChat` from the SDK is the same
 * `ChatView` the workspace pane renders, so the transcript, the tool cards,
 * the streaming and thinking indicators, attachments, voice and the composer
 * are not approximations of the real thing, they ARE it. The plugin's only job
 * is to say which conversation and to give it a shape that suits a canvas.
 *
 * There was a hand-rolled version of this: bespoke turn rows, a hand-driven
 * event stream, a spinner made of three dots. It cost a lot and looked like a
 * copy, which is what it was. Everything it did is now upstream, and the parts
 * worth keeping — the dock the composer sits in, the transport fused to its
 * top — are chrome around the real component rather than a rebuild of it.
 *
 * Layout is CSS, from `[data-canvas-chat]` down, the way HUD mode restyles
 * this same tree through `[data-hud-shell]`. No variant props, nothing forked:
 * when the chat gains a feature, this gains it too.
 */

import { Codicon, SessionChat } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { ensureCanvasSession } from './session'

/** How much of the pane the conversation may take before it scrolls. The
 *  transport and composer ride under the band, so the whole dock tops out
 *  around 40% of the viewport — the canvas stays the protagonist. */
const BAND_MAX_FRACTION = 0.28

/** Breathing room under the last row, so text doesn't sit on the seam. */
const BAND_PAD_PX = 10

/**
 * Size the transcript band to its contents, the way HUD mode does.
 *
 * The chat is built to fill a pane: its scroll container is `min-height: 100%`,
 * so in a dock it either fills everything it is given or collapses to nothing,
 * and neither is a band. HUD hit this first and solved it by measuring — the
 * tight bbox of the message rows, not the viewport, which is the whole trick
 * (measuring the viewport counts the full-height scroll container and paints an
 * empty slab). The measurement drives CSS vars and the stylesheet does the rest.
 *
 * Same two numbers here: how tall the conversation is, and how tall the bar
 * under it is, since the band is positioned off the bar's top edge.
 */
function useBandMetrics(root: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const el = root.current

    if (!el) {
      return
    }

    let viewport: HTMLElement | null = null
    const ro = new ResizeObserver(() => measure())

    const measure = () => {
      const found = el.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')

      if (found !== viewport) {
        viewport = found

        if (found) {
          ro.observe(found)

          if (found.firstElementChild) {
            ro.observe(found.firstElementChild)
          }
        }
      }

      // Turns only. The content box always has furniture — titlebar pad,
      // composer clearance, the empty-state grid (h-full + py-8) — and
      // measuring those is how a brand-new session grew a blank sheet the
      // size of the band the moment the composer took focus.
      const turns = Array.from(
        viewport?.querySelectorAll<HTMLElement>(
          '[data-slot="aui_user-message-root"], [data-slot="aui_assistant-message-root"]'
        ) ?? []
      ).filter(row => row.getBoundingClientRect().height > 0)

      const span = !turns.length
        ? 0
        : Math.max(0, turns[turns.length - 1].getBoundingClientRect().bottom - turns[0].getBoundingClientRect().top)

      el.toggleAttribute('data-canvas-thread', span > 0)

      const bar = el.querySelector<HTMLElement>('[data-slot="composer-dock"]')

      if (bar) {
        ro.observe(bar)
        el.style.setProperty('--canvas-bar-height', `${Math.round(bar.getBoundingClientRect().height)}px`)
      }

      // The transport rides between the band and the composer, so the band has
      // to clear it as well. It lives outside this element (it's the dock card
      // the composer is fused to), hence the reach upward.
      const transport = el.parentElement?.querySelector<HTMLElement>('.canvas-dock-transport')

      if (transport) {
        ro.observe(transport)
        el.style.setProperty('--canvas-transport-height', `${Math.round(transport.getBoundingClientRect().height)}px`)
      }

      el.style.setProperty(
        '--canvas-band-height',
        `${span < 1 ? 0 : Math.round(Math.min(span + BAND_PAD_PX, window.innerHeight * BAND_MAX_FRACTION))}px`
      )
    }

    // The chat surface mounts async, so poll until the viewport exists and let
    // the observer take it from there. Window resize is separate: the rows may
    // not change, but the ceiling they're capped against does.
    measure()
    const probe = window.setInterval(measure, 500)
    window.addEventListener('resize', measure)

    return () => {
      window.clearInterval(probe)
      window.removeEventListener('resize', measure)
      ro.disconnect()
    }
  }, [root])
}

export function CanvasChat({ autofocus, workflowId }: { autofocus?: boolean; workflowId: string }) {
  const [session, setSession] = useState('')
  const [error, setError] = useState('')
  const root = useRef<HTMLDivElement>(null)

  useBandMetrics(root)

  useEffect(() => {
    let live = true

    ensureCanvasSession(workflowId)
      .then(id => live && setSession(id))
      .catch((err: unknown) => live && setError(err instanceof Error ? err.message : String(err)))

    return () => {
      live = false
    }
  }, [workflowId])

  useEffect(() => {
    if (!autofocus || !session) {
      return
    }

    const node = root.current
    let tries = 0

    const tick = window.setInterval(() => {
      const input = node?.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')

      if (input) {
        input.focus()
        window.clearInterval(tick)
      } else if (++tries > 40) {
        window.clearInterval(tick)
      }
    }, 50)

    return () => window.clearInterval(tick)
  }, [autofocus, session])

  if (error) {
    return (
      <div className="canvas-chat-idle">
        <Codicon name="warning" />
        {error}
      </div>
    )
  }

  return (
    <div className="canvas-chat" data-canvas-chat="" ref={root}>
      {/* The band's backing, on a layer of its own so the chat surface doesn't
          have to know it's floating over a graph. First child, so it paints
          behind the transcript. */}
      <div aria-hidden className="canvas-chat-sheet" />
      {session ? <SessionChat storedSessionId={session} /> : null}
    </div>
  )
}

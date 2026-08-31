/**
 * PIN PANEL — annotation mode's UI: the toggle, the list, and the one button
 * that turns a review into a prompt.
 *
 * The pins themselves are drawn IN the page by the engine, because only the
 * page knows where its elements are after a scroll or a reflow. This panel is
 * the durable side: it holds what the engine would lose to a navigation, and it
 * is what replays them back afterwards.
 *
 * "Attach to chat" produces a `pins` composer attachment rather than sending a
 * message. Deciding when to send stays the user's — they usually have something
 * to add on top of the comments.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { dataUrlToBlob } from '@/lib/embedded-images'
import { orderedShots, pinAttachmentLabel } from '@/lib/preview-pins/pin-block'
import { allPins, mergeReport, otherPages, type PinBook, pinsForPage } from '@/lib/preview-pins/pin-book'
import type { PreviewPin } from '@/lib/preview-pins/types'
import { cn } from '@/lib/utils'
import { addComposerAttachment, createComposerAttachmentOccurrenceId } from '@/store/composer'
import { relayComposerAttachment } from '@/store/composer-relay'
import { notify } from '@/store/notifications'
import { isBrowserWindow } from '@/store/windows'

import {
  armPins,
  clearPins,
  disarmPins,
  hidePins,
  readPins,
  reattachPins,
  removePin,
  showPins,
  takeShot,
  togglePinResolved
} from './preview-pins'

/** How often to re-read while armed. The engine owns placement, so the panel
 *  only learns about a new pin by asking — and a gesture the list does not
 *  reflect within a beat reads as the click having missed. */
const POLL_MS = 700

/** How many comments the panel shows before it asks to be expanded. */
const COLLAPSED_ROWS = 2

export function PreviewPinPanel({ open, url }: { open: boolean; url: string }) {
  const [pins, setPins] = useState<PreviewPin[]>([])
  const [armed, setArmed] = useState(false)
  const [live, setLive] = useState(true)
  /** Every page's pins, so a review can walk the site. The engine only ever
   *  holds the current page's; this is the side that outlives a navigation. */
  const book = useRef<PinBook>({})
  const [elsewhere, setElsewhere] = useState({ count: 0, pages: 0 })
  const [expanded, setExpanded] = useState(false)

  /** Full image bytes, drained out of the page and owned here. */
  const bytes = useRef<Map<string, string>>(new Map())

  const sync = useCallback(async (report: Awaited<ReturnType<typeof readPins>>) => {
    if (!report) {
      setLive(false)

      return
    }

    // Drain first, and after every verb rather than only while annotating: an
    // image pasted and then left alone still has to get out of the page before
    // the next navigation takes the page with it.
    for (const id of report.pendingShots ?? []) {
      if (bytes.current.has(id)) {continue}
      const answer = await takeShot(id)

      if (answer?.shot) {bytes.current.set(id, answer.shot)}
    }

    setLive(true)
    setArmed(report.armed === true)
    setPins(report.pins)
    // File under the page's OWN url, not the pane's — the pane's value lags a
    // redirect, and filing under the wrong key is how a page's comments end up
    // replayed onto a different page.
    book.current = mergeReport(book.current, report.url, report.pins)
    setElsewhere(otherPages(book.current, report.url))
  }, [])

  // Poll while the panel is open — not only while armed. A marker stays
  // clickable after disarming, so a comment can be edited or an image pasted
  // with annotation mode off, and those bytes need draining too. Closed, this
  // stops entirely: a poll against a page nobody is reviewing is a round trip
  // into the guest document every beat for nothing.
  useEffect(() => {
    if (!open) {return}
    const timer = setInterval(() => void readPins().then(sync), POLL_MS)

    return () => clearInterval(timer)
  }, [open, sync])

  // Closing the panel hands the page back. Without this the engine stays armed
  // behind a UI that is no longer on screen: the next click on a link is eaten
  // by the review overlay instead of navigating, and nothing visible explains
  // why. Opening repaints what the page is still holding.
  useEffect(() => {
    if (!open) {
      void hidePins()

      return
    }

    void showPins(pinsForPage(book.current, url)).then(sync)
  }, [open, sync, url])

  // A pane teardown is a close the effect above never sees.
  useEffect(() => () => void hidePins(), [])

  // A navigation destroys the engine and every pin with it. Seed the new one
  // from this page's bucket — and only this page's — then re-run the ladder.
  useEffect(() => {
    if (!open) {return}
    void reattachPins(pinsForPage(book.current, url)).then(sync)
  }, [open, sync, url])

  const toggleArmed = async () => {
    const report = armed ? await disarmPins() : await armPins(pinsForPage(book.current, url))
    await sync(report)
  }

  const attach = async () => {
    // The whole review, not just the page in front of us. Someone who commented
    // on the home page and then on a product page meant one request.
    const held = allPins(book.current).filter(pin => !pin.resolved)

    // Drop any image whose bytes never reached us — a page closed before the
    // drain, say. The block numbers images off this list and the attachments
    // are built from the same walk, so pruning here keeps "[image 2]" and the
    // second picture the same picture by construction.
    const sending = held.map(pin => {
      const shots = (pin.shots ?? []).filter(shot => bytes.current.has(shot.id))

      return shots.length === (pin.shots?.length ?? 0) ? pin : { ...pin, shots }
    })

    if (!sending.length) {return}

    /**
     * Put the chip where the composer actually is.
     *
     * The popped-out Browser is the one window with no composer, so adding to
     * its own store was a click that succeeded into a void. Every other window
     * — including the secondary session window and the HUD — renders a real
     * composer, and the chip belongs in THAT one: relaying from there would
     * hand it to the primary window, which is not the window the user is
     * looking at.
     *
     * Posts immediately and hands back the acknowledgement to await LATER.
     * Awaiting each one here instead cost a full relay timeout per attachment
     * — a review with eight pictures spent nearly four seconds deciding it had
     * failed. Order is unaffected: the posts still leave in call order, and a
     * BroadcastChannel delivers them in that order.
     */
    const acks: Promise<boolean>[] = []

    const deliver = (attachment: Parameters<typeof addComposerAttachment>[0]) => {
      if (isBrowserWindow()) {
        acks.push(relayComposerAttachment(attachment))

        return
      }

      addComposerAttachment(attachment)
      acks.push(Promise.resolve(true))
    }

    deliver({
      detail: JSON.stringify(sending),
      // Derived from the pins alone: once a batch spans pages, no single url
      // identifies it.
      id: `pins:${sending.map(pin => pin.id).join(',')}`,
      kind: 'pins',
      label: pinAttachmentLabel(sending),
      refText: `${sending.length} preview comment${sending.length === 1 ? '' : 's'}`
    })

    // Then the pictures themselves, as ordinary image attachments — the same
    // road a dropped screenshot takes, so nothing downstream needs to know
    // these came from a pin.
    let index = 0

    for (const { shot } of orderedShots(sending)) {
      index += 1
      const data = bytes.current.get(shot.id)
      const blob = data ? dataUrlToBlob(data) : null

      if (!blob) {continue}

      try {
        const buffer = new Uint8Array(await blob.arrayBuffer())
        const path = await window.hermesDesktop?.saveImageBuffer(buffer, blob.type === 'image/png' ? '.png' : '.jpg')

        if (!path) {continue}

        deliver({
          detail: path,
          id: `pin-image:${shot.id}`,
          kind: 'image',
          // Matches the "[image 2]" marker in the block above.
          label: `image ${index}`,
          occurrenceId: createComposerAttachmentOccurrenceId(),
          path,
          thumbnailUrl: shot.thumb
        })
      } catch {
        // One picture that would not stage is not worth losing the comments
        // over; the block still describes what the user meant.
        continue
      }
    }

    const delivered = (await Promise.all(acks)).filter(Boolean).length

    // Say so either way. The chip lands in a composer that may be scrolled out
    // of sight or in another window entirely, and a button that looks inert is
    // exactly how this was reported.
    notify(
      delivered
        ? {
            kind: 'success',
            message: `${sending.length} comment${sending.length === 1 ? '' : 's'} ready in the composer`,
            title: 'Added to chat'
          }
        : {
            kind: 'error',
            message: 'No composer window is open to receive them.',
            title: 'Could not add to chat'
          }
    )
  }

  const clearEverything = async () => {
    book.current = {}
    setElsewhere({ count: 0, pages: 0 })
    await clearPins().then(sync)
  }

  if (!open) {return null}

  const openCount = pins.filter(pin => !pin.resolved).length

  // Newest first, and only a couple of them: the panel sits above the page it
  // is describing, and a comment list that grows without bound eats the very
  // thing being reviewed. The number stays the pin's own, so a row and the
  // marker on the page always agree even when the order does not.
  const numbered = pins.map((pin, index) => ({ number: index + 1, pin })).reverse()
  const listed = expanded ? numbered : numbered.slice(0, COLLAPSED_ROWS)

  return (
    <div className="flex flex-col gap-2 border-t border-border/60 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <button
          className={cn(
            'flex items-center gap-1.5 rounded px-2 py-1 font-medium transition-colors',
            armed ? 'bg-primary text-primary-foreground' : 'bg-muted hover:bg-muted/70'
          )}
          onClick={() => void toggleArmed()}
          type="button"
        >
          <Codicon name="comment-draft" size="0.8125rem" />
          {armed ? 'Annotating' : 'Annotate'}
        </button>

        <span className="truncate text-muted-foreground">
          {!live
            ? 'no live page'
            : armed
              ? 'click an element, or drag a region · Esc to stop'
              : `${openCount} open`}
          {/* Comments left on pages the user has since navigated away from.
              Without this the panel looks empty on a fresh page and the review
              they already wrote appears to have been lost. */}
          {!armed && elsewhere.count > 0 && (
            <span className="ms-1 text-muted-foreground/70">
              · {elsewhere.count} on {elsewhere.pages} other page{elsewhere.pages === 1 ? '' : 's'}
            </span>
          )}
        </span>

        <div className="ms-auto flex shrink-0 items-center gap-1">
          <button
            className="rounded px-2 py-1 hover:bg-muted disabled:opacity-40"
            disabled={!openCount && !elsewhere.count}
            onClick={() => void attach()}
            title="Add every open comment, across every page, to the composer"
            type="button"
          >
            Attach to chat
          </button>
          <button
            className="rounded px-2 py-1 hover:bg-muted disabled:opacity-40"
            disabled={!pins.length && !elsewhere.count}
            onClick={() => void clearEverything()}
            title="Discard the whole review, every page"
            type="button"
          >
            Clear
          </button>
        </div>
      </div>

      {listed.length > 0 && (
        <ul className={cn('flex flex-col gap-0.5', expanded && 'max-h-40 overflow-y-auto pe-0.5')}>
          {listed.map(({ number, pin }) => (
            <li
              className={cn(
                // One line per comment. Two lines each turned four comments
                // into half the preview, which is the space the page needs.
                'flex items-center gap-2 rounded px-2 py-1',
                pin.resolved ? 'opacity-50' : 'bg-muted/40'
              )}
              key={pin.id}
            >
              <span className="font-mono text-[10px] text-muted-foreground">{number}</span>
              {/* The thumbnail the user pasted, at list size. Seeing it here is
                  what makes the strip inside the bubble discoverable at all. */}
              {pin.shots?.length ? (
                <img
                  alt=""
                  className="size-5 shrink-0 rounded-sm border border-border/60 object-cover"
                  src={pin.shots[0].thumb}
                />
              ) : null}
              <span className="min-w-0 flex-1 truncate">
                <span className="font-medium">{pin.target || 'region'}</span>
                {(pin.shots?.length ?? 0) > 1 && (
                  <span className="ms-1 text-muted-foreground">·{pin.shots!.length} images</span>
                )}
                {/* A pin that came back on a weak rung is worth seeing. The
                    comment is still attached to something, but not to the
                    thing the page promised it. */}
                {pin.orphaned && <span className="ms-1.5 text-amber-500">· detached</span>}
                <span className="ms-1.5 text-muted-foreground">{pin.comment || 'no comment yet'}</span>
              </span>
              <button
                className="rounded px-1 hover:bg-muted"
                onClick={() => void togglePinResolved(pin.id).then(sync)}
                title={pin.resolved ? 'Reopen' : 'Mark resolved'}
                type="button"
              >
                <Codicon name={pin.resolved ? 'circle-outline' : 'check'} size="0.75rem" />
              </button>
              <button
                className="rounded px-1 hover:bg-muted"
                onClick={() => void removePin(pin.id).then(sync)}
                title="Delete"
                type="button"
              >
                <Codicon name="trash" size="0.75rem" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {pins.length > COLLAPSED_ROWS && (
        <button
          className="self-start rounded px-1 text-muted-foreground hover:text-foreground"
          onClick={() => setExpanded(open => !open)}
          type="button"
        >
          {expanded ? 'Show fewer' : `Show all ${pins.length}`}
        </button>
      )}
    </div>
  )
}

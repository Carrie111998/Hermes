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
import { pinAttachmentLabel } from '@/lib/preview-pins/pin-block'
import { allPins, mergeReport, otherPages, type PinBook, pinsForPage } from '@/lib/preview-pins/pin-book'
import type { PreviewPin } from '@/lib/preview-pins/types'
import { cn } from '@/lib/utils'
import { addComposerAttachment } from '@/store/composer'

import {
  armPins,
  clearPins,
  disarmPins,
  hidePins,
  readPins,
  reattachPins,
  removePin,
  showPins,
  togglePinResolved
} from './preview-pins'

/** How often to re-read while armed. The engine owns placement, so the panel
 *  only learns about a new pin by asking — and a gesture the list does not
 *  reflect within a beat reads as the click having missed. */
const POLL_MS = 700

export function PreviewPinPanel({ open, url }: { open: boolean; url: string }) {
  const [pins, setPins] = useState<PreviewPin[]>([])
  const [armed, setArmed] = useState(false)
  const [live, setLive] = useState(true)
  /** Every page's pins, so a review can walk the site. The engine only ever
   *  holds the current page's; this is the side that outlives a navigation. */
  const book = useRef<PinBook>({})
  const [elsewhere, setElsewhere] = useState({ count: 0, pages: 0 })

  const sync = useCallback(async (report: Awaited<ReturnType<typeof readPins>>) => {
    if (!report) {
      setLive(false)

      return
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

  // Poll only while the panel is open AND armed. A background poll against a
  // page the user is not annotating is a round trip into the guest document
  // every beat for nothing.
  useEffect(() => {
    if (!open || !armed) {return}
    const timer = setInterval(() => void readPins().then(sync), POLL_MS)

    return () => clearInterval(timer)
  }, [armed, open, sync])

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

  const attach = () => {
    // The whole review, not just the page in front of us. Someone who commented
    // on the home page and then on a product page meant one request.
    const sending = allPins(book.current).filter(pin => !pin.resolved)

    if (!sending.length) {return}
    addComposerAttachment({
      detail: JSON.stringify(sending),
      // Derived from the pins alone: once a batch spans pages, no single url
      // identifies it.
      id: `pins:${sending.map(pin => pin.id).join(',')}`,
      kind: 'pins',
      label: pinAttachmentLabel(sending),
      refText: `${sending.length} preview comment${sending.length === 1 ? '' : 's'}`
    })
  }

  const clearEverything = async () => {
    book.current = {}
    setElsewhere({ count: 0, pages: 0 })
    await clearPins().then(sync)
  }

  if (!open) {return null}

  const openCount = pins.filter(pin => !pin.resolved).length

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
            onClick={attach}
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

      {pins.length > 0 && (
        <ul className="flex max-h-44 flex-col gap-1 overflow-y-auto">
          {pins.map((pin, index) => (
            <li
              className={cn(
                'flex items-start gap-2 rounded px-2 py-1',
                pin.resolved ? 'opacity-50' : 'bg-muted/40'
              )}
              key={pin.id}
            >
              <span className="mt-0.5 font-mono text-muted-foreground">{index + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">
                  {pin.target || 'region'}
                  {/* A pin that came back on a weak rung is worth seeing. The
                      comment is still attached to something, but not to the
                      thing the page promised it. */}
                  {pin.orphaned && <span className="ms-1.5 text-amber-500">· detached</span>}
                </div>
                <div className="truncate text-muted-foreground">{pin.comment || 'no comment yet'}</div>
              </div>
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
    </div>
  )
}

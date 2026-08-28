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
import type { PreviewPin } from '@/lib/preview-pins/types'
import { cn } from '@/lib/utils'
import { addComposerAttachment } from '@/store/composer'

import {
  armPins,
  clearPins,
  disarmPins,
  readPins,
  reattachPins,
  removePin,
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
  /** Last known pins, so a navigation can replay them into a fresh engine. */
  const held = useRef<PreviewPin[]>([])

  const sync = useCallback(async (report: Awaited<ReturnType<typeof readPins>>) => {
    if (!report) {
      setLive(false)

      return
    }

    setLive(true)
    setArmed(report.armed === true)
    setPins(report.pins)
    held.current = report.pins
  }, [])

  // Poll only while the panel is open AND armed. A background poll against a
  // page the user is not annotating is a round trip into the guest document
  // every beat for nothing.
  useEffect(() => {
    if (!open || !armed) {return}
    const timer = setInterval(() => void readPins().then(sync), POLL_MS)

    return () => clearInterval(timer)
  }, [armed, open, sync])

  useEffect(() => {
    if (open) {void readPins().then(sync)}
  }, [open, sync])

  // A navigation destroys the engine and every pin with it. Seed the new one
  // from what the panel is holding, then re-run the ladder over it.
  useEffect(() => {
    if (!open || !held.current.length) {return}
    void reattachPins(held.current).then(sync)
  }, [open, sync, url])

  const toggleArmed = async () => {
    const report = armed ? await disarmPins() : await armPins(held.current)
    await sync(report)
  }

  const attach = () => {
    const openPins = pins.filter(pin => !pin.resolved)

    if (!openPins.length) {return}
    addComposerAttachment({
      detail: JSON.stringify(openPins),
      id: `pins:${url}:${openPins.map(pin => pin.id).join(',')}`,
      kind: 'pins',
      label: pinAttachmentLabel(openPins),
      refText: `${openPins.length} preview comment${openPins.length === 1 ? '' : 's'}`
    })
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

        <span className="text-muted-foreground">
          {!live
            ? 'no live page'
            : armed
              ? 'click an element, or drag a region · Esc to stop'
              : `${openCount} open`}
        </span>

        <div className="ms-auto flex items-center gap-1">
          <button
            className="rounded px-2 py-1 hover:bg-muted disabled:opacity-40"
            disabled={!openCount}
            onClick={attach}
            title="Add these comments to the composer"
            type="button"
          >
            Attach to chat
          </button>
          <button
            className="rounded px-2 py-1 hover:bg-muted disabled:opacity-40"
            disabled={!pins.length}
            onClick={() => void clearPins().then(sync)}
            title="Remove every pin on this page"
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

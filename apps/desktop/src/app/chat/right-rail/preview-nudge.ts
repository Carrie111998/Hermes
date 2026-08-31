/**
 * OVERLAY NUDGE — the one way to say a stage to an overlay that is ALREADY on
 * the page.
 *
 * The act pipeline (preview-act.ts) injects the whole engine because it has to:
 * it needs to resolve a ref, measure a node and read the page back. The stages
 * that only narrate — the idle pulse, the read wipe — need none of that. They
 * are one function call against something the last action already left on the
 * page, so re-shipping the engine to make it would put the payload on the wire
 * on every turn boundary.
 *
 * A page the agent has never acted on has no overlay, and the nudge is a no-op
 * there. That is the right answer rather than a gap: chrome on a page the agent
 * never touched would be a lie about what it did.
 */

import type { WatchStage } from '@/lib/preview-act/watch-in-page'

import { agentPreviewScriptRunner } from './preview-script-runner'

/** Run one stage against the AGENT's pane overlay, if it has one. The overlay
 *  narrates the agent, so it belongs to the tab the agent acts on — not to
 *  whichever tab you happen to be looking at. */
export function nudgeOverlay(stage: WatchStage, sessionId: null | string): void {
  const run = agentPreviewScriptRunner(sessionId)

  if (!run) {
    return
  }

  void run(`(function () {
  var w = window;
  var fn = w.__hermesWatch_fn;
  if (!fn) { return 'cold'; }
  try { fn(document, w.__hermesActHolder || {}, ${JSON.stringify(stage)}); } catch (err) {}
  return 'ok';
})()`).catch(() => {
    // The page navigated out from under us. The next action re-injects.
  })
}

// Large-transcript mount cost. New scenario (no prior script measured this):
// loads N synthetic turns of mixed markdown into $messages and records the
// mount→paint time plus any longtasks the mount blocks the main thread with.
// This is the "open a long session" path — a first-impression latency.

import { SELECTORS, sleep } from '../lib/cdp.mjs'

const OBSERVE = `
  (() => {
    window.__TM__ = { longtasks: [] }
    try {
      const po = new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__TM__.longtasks.push(e.duration)
      })
      po.observe({ entryTypes: ['longtask'] })
      window.__TM__.po = po
    } catch {}
    return 'observing'
  })()
`

export default {
  name: 'transcript',
  tier: 'ci',
  description: 'Mount + paint cost of loading a long transcript.',
  async run(cdp, opts = {}) {
    const turns = Number(opts.turns ?? 200)

    await cdp.send('Runtime.enable')

    let mountMs
    let longtasks
    let groupVisibility

    try {
      await cdp.eval(OBSERVE)
      mountMs = await cdp.eval(`window.__PERF_DRIVE__.loadTranscript(${turns})`)

      // Let post-mount work settle before checking the rendered transcript.
      await sleep(1500)

      longtasks = await cdp.eval('window.__TM__.longtasks')
      groupVisibility = await cdp.eval(`(() => {
        const groups = [...document.querySelectorAll(${JSON.stringify(SELECTORS.threadGroup)})]
        return {
          count: groups.length,
          nonVisible: groups.filter(group => getComputedStyle(group).contentVisibility !== 'visible').length
        }
      })()`)

      if (!groupVisibility.count) {
        throw new Error('transcript scenario rendered no thread groups')
      }

      if (groupVisibility.nonVisible) {
        throw new Error(
          `transcript rendered ${groupVisibility.nonVisible}/${groupVisibility.count} groups without content-visibility:visible`
        )
      }

      return {
        metrics: {
          transcript_mount_ms: Math.round(mountMs * 10) / 10,
          transcript_longtask_ms: Math.round(longtasks.reduce((a, b) => a + b, 0) * 10) / 10,
          transcript_longtask_max_ms: Math.round((longtasks.length ? Math.max(...longtasks) : 0) * 10) / 10
        },
        detail: {
          turns,
          messages: turns * 2,
          longtasks: longtasks.length,
          renderedGroups: groupVisibility.count,
          contentVisibilityNonVisibleGroups: groupVisibility.nonVisible
        }
      }
    } finally {
      try {
        await cdp.eval('try { window.__TM__?.po?.disconnect() } catch {}')
      } finally {
        await cdp.eval('window.__PERF_DRIVE__?.reset()')
      }
    }
  }
}

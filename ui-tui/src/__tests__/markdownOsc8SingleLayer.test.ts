import { PassThrough } from 'stream'

import { Box, renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Md } from '../components/markdown.js'
import { __resetLinkTitleCache } from '../lib/externalLink.js'
import { DEFAULT_THEME } from '../theme.js'

afterEach(() => {
  __resetLinkTitleCache()
  vi.unstubAllGlobals()
})

const ESC = String.fromCharCode(27)
const BEL = String.fromCharCode(7)

// No network in unit tests: unresolved titles fall back to the
// hostPathLabel-derived label, which is exactly the shape whose OSC 8
// wrapping stacks on the terminal's own URL autodetection (#98091).
const stubUnresolvedTitles = () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
}

// The renderer emits `ESC ] 8 ; id=<group> ; url BEL` (the id param groups
// wrapped lines), so match the url with an optional id segment.
const osc8For = (url: string) =>
  new RegExp(`${ESC}\\]8;(?:id=[^;${BEL}]*;)?${url.replace(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`)}${BEL}`)

const renderPlain = (text: string, width = 120) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(Box, { width }, React.createElement(Md, { t: DEFAULT_THEME, text })),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  instance.unmount()

  return output
}

describe('URL-shaped labels emit a single clickable layer (#98091)', () => {
  it('does not wrap a bare URL whose label equals the target in OSC 8', () => {
    stubUnresolvedTitles()

    const output = renderPlain('see https://github.com for details')

    // The fallback label (hostPathLabel) is `github.com`, which the
    // terminal autodetects as a link by itself; an OSC 8 wrapper on top
    // makes Cmd+Click in Warp open two tabs.
    expect(output).toContain('github.com')
    expect(output).not.toMatch(osc8For('https://github.com'))
  })

  it('does not wrap a markdown link whose label is just the URL', () => {
    stubUnresolvedTitles()

    // A numeric path has no readable slug, so the label falls back to the
    // host/path itself — URL-shaped text that pickAuthoredLabel also drops
    // from `[url](url)` forms.
    const url = 'https://github.com/123'
    const output = renderPlain(`[${url}](${url})`)

    expect(output).toContain('github.com/123')
    expect(output).not.toMatch(osc8For(url))
  })

  it('keeps OSC 8 when the label drops part of the target, so the href stays exact', async () => {
    // `https://example.com/1?q=1` falls back to the host/path label
    // `example.com/1` — the query string never survives into the text, so
    // without the OSC 8 layer the click would silently drop `?q=1`.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('<html><head><title></title></head></html>', {
          headers: { 'content-type': 'text/html' },
          status: 200
        })
      )
    )

    const output = renderPlain('see https://example.com/1?q=1 now')

    expect(output).toContain('example.com/1')
    expect(output).toMatch(osc8For('https://example.com/1?q=1'))
  })

  it('keeps OSC 8 for authored labels and resolved titles', async () => {
    const url = 'https://www.expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure'

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(`<html><head><title>Rainforest Adventure Tour</title></head></html>`, {
          headers: { 'content-type': 'text/html' },
          status: 200
        })
      )
    )

    const { fetchLinkTitle } = await import('../lib/externalLink.js')
    await fetchLinkTitle(url)

    const authored = renderPlain(`[Trip details](${url})`)
    expect(authored).toContain('Trip details')
    expect(authored).toMatch(osc8For(url))

    const titled = renderPlain(`see ${url} now`)
    expect(titled).toContain('Rainforest Adventure Tour')
    expect(titled).toMatch(osc8For(url))
  })
})

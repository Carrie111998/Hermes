import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  __resetLinkTitleCache,
  ExternalLink,
  fetchLinkTitle,
  hostPathLabel,
  isTitleFetchable,
  LinkifiedText,
  openExternalLink,
  openExternalLinkWithFallback,
  PrettyLink,
  urlSlugTitleLabel
} from './external-link'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

function installDesktopBridge(partial: Partial<Window['hermesDesktop']> = {}) {
  desktopWindow.hermesDesktop = {
    fetchLinkTitle: vi.fn().mockResolvedValue(''),
    openExternal: vi.fn().mockResolvedValue(undefined),
    ...partial
  } as unknown as Window['hermesDesktop']
}

const FORGEJO_URL = 'https://forgejo.home.example/homelab/homelab-ops/issues/101'

function installTitleBridge(title: string) {
  const bridge = vi.fn().mockResolvedValue(title)

  installDesktopBridge({ fetchLinkTitle: bridge as unknown as Window['hermesDesktop']['fetchLinkTitle'] })

  return bridge
}

afterEach(() => {
  __resetLinkTitleCache()
  vi.restoreAllMocks()
  cleanup()

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('external link helpers', () => {
  it('formats URL fallbacks as host + path', () => {
    expect(
      hostPathLabel(
        'https://www.getyourguide.com/culebra-island-l145468/from-fajardo-full-day-cordillera-islands-catamaran-tour-t19894/'
      )
    ).toBe('getyourguide.com/culebra-island-l145468/from-fajardo-full-day-cordillera-islands-catamaran-tour-t19894')
  })

  it('derives readable title fallbacks from URL slugs', () => {
    expect(
      urlSlugTitleLabel(
        'https://www.getyourguide.com/fajardo-l882/from-fajardo-icacos-island-full-day-catamaran-trip-t19891/'
      )
    ).toBe('From Fajardo Icacos Island Full Day Catamaran Trip')
  })

  it('filters out local/non-http targets for title fetches', () => {
    expect(isTitleFetchable('https://www.expedia.com/things-to-do/foo')).toBe(true)
    expect(isTitleFetchable('http://localhost:5174')).toBe(false)
    expect(isTitleFetchable('file:///tmp/demo.html')).toBe(false)
    expect(isTitleFetchable('mailto:hello@example.com')).toBe(false)
  })

  it('deduplicates in-flight title fetches and caches results', async () => {
    const bridge = vi.fn().mockResolvedValue('El Yunque Tour Water Slide, Rope Swing & Pickup')
    installDesktopBridge({ fetchLinkTitle: bridge as unknown as Window['hermesDesktop']['fetchLinkTitle'] })

    const url =
      'https://www.expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure-with-transport.a46272756.activity-details'

    const [first, second] = await Promise.all([fetchLinkTitle(url), fetchLinkTitle(url)])

    expect(first).toBe('El Yunque Tour Water Slide, Rope Swing & Pickup')
    expect(second).toBe('El Yunque Tour Water Slide, Rope Swing & Pickup')
    expect(bridge).toHaveBeenCalledTimes(1)

    const third = await fetchLinkTitle(url)

    expect(third).toBe('El Yunque Tour Water Slide, Rope Swing & Pickup')
    expect(bridge).toHaveBeenCalledTimes(1)
  })

  it('shares cache across protocol/www URL variants', async () => {
    const bridge = vi.fn().mockResolvedValue('Shared Canonical Title')
    installDesktopBridge({ fetchLinkTitle: bridge as unknown as Window['hermesDesktop']['fetchLinkTitle'] })

    const first = 'https://www.getyourguide.com/san-juan-puerto-rico-l355/sunset-tours-tc306/'
    const second = 'http://getyourguide.com/san-juan-puerto-rico-l355/sunset-tours-tc306/'

    const [a, b] = await Promise.all([fetchLinkTitle(first), fetchLinkTitle(second)])

    expect(a).toBe('Shared Canonical Title')
    expect(b).toBe('Shared Canonical Title')
    expect(bridge).toHaveBeenCalledTimes(1)
  })

  it('opens links via the desktop bridge', () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    installDesktopBridge({ openExternal: openExternal as unknown as Window['hermesDesktop']['openExternal'] })

    render(<ExternalLink href="https://example.com/path/to/resource">Example link</ExternalLink>)

    fireEvent.click(screen.getByRole('link', { name: 'Example link' }))
    expect(openExternal).toHaveBeenCalledWith('https://example.com/path/to/resource')
  })

  it('opens intercepted links in a browser preview without the desktop bridge', () => {
    const popup = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    delete desktopWindow.hermesDesktop

    render(<ExternalLink href="mailto:hello@example.com">Email link</ExternalLink>)

    fireEvent.click(screen.getByRole('link', { name: 'Email link' }))

    expect(popup).toHaveBeenCalledWith('mailto:hello@example.com', '_blank', 'noopener,noreferrer')
  })

  it('does not bypass a present but incomplete desktop bridge', () => {
    const popup = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    desktopWindow.hermesDesktop = {} as Window['hermesDesktop']

    render(<ExternalLink href="https://example.com/path/to/resource">Example link</ExternalLink>)

    fireEvent.click(screen.getByRole('link', { name: 'Example link' }))

    expect(popup).not.toHaveBeenCalled()
  })

  it('routes middle-clicks through the bridge while preserving right-click menus', () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    installDesktopBridge({ openExternal: openExternal as unknown as Window['hermesDesktop']['openExternal'] })

    render(<ExternalLink href="https://example.com/path/to/resource">Example link</ExternalLink>)

    const link = screen.getByRole('link', { name: 'Example link' })
    const middleClick = new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 })
    expect(link.dispatchEvent(middleClick)).toBe(false)
    expect(openExternal).toHaveBeenCalledWith('https://example.com/path/to/resource')

    openExternal.mockClear()
    const rightClick = new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 2 })
    expect(link.dispatchEvent(rightClick)).toBe(true)
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('uses a browser popup only without the desktop bridge and surfaces bridge failures', async () => {
    const popup = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    delete desktopWindow.hermesDesktop

    await openExternalLinkWithFallback('https://example.com/sign-in')
    expect(popup).toHaveBeenCalledWith('https://example.com/sign-in', '_blank', 'noopener,noreferrer')

    const bridgeError = new Error('OS opener unavailable')
    installDesktopBridge({ openExternal: vi.fn().mockRejectedValue(bridgeError) })

    await expect(openExternalLinkWithFallback('https://example.com/sign-in')).rejects.toBe(bridgeError)
    expect(popup).toHaveBeenCalledTimes(1)
  })

  it('does not treat a null browser popup result as a navigation failure', async () => {
    const popup = vi.spyOn(window, 'open').mockReturnValue(null)
    delete desktopWindow.hermesDesktop

    await expect(openExternalLinkWithFallback('https://example.com/sign-in')).resolves.toBeUndefined()
    expect(popup).toHaveBeenCalledWith('https://example.com/sign-in', '_blank', 'noopener,noreferrer')
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'file:///tmp/demo.html',
    'spotify:track:123',
    '/relative/path',
    'not a URL',
    'https:///missing-host'
  ])('rejects unsafe external schemes before bridge or browser navigation: %s', async href => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    const popup = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    installDesktopBridge({ openExternal: openExternal as unknown as Window['hermesDesktop']['openExternal'] })

    openExternalLink(href)
    await expect(openExternalLinkWithFallback(href)).rejects.toThrow('Unsupported external URL')

    expect(openExternal).not.toHaveBeenCalled()
    expect(popup).not.toHaveBeenCalled()
  })

  it('rejects unsafe external schemes in browser fallback mode', async () => {
    const popup = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    delete desktopWindow.hermesDesktop

    openExternalLink('javascript:alert(1)')
    await expect(openExternalLinkWithFallback('file:///tmp/demo.html')).rejects.toThrow('Unsupported external URL')

    expect(popup).not.toHaveBeenCalled()
  })

  it('preserves supported external schemes after normalization', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    installDesktopBridge({ openExternal: openExternal as unknown as Window['hermesDesktop']['openExternal'] })

    openExternalLink('example.com/docs')
    await openExternalLinkWithFallback('mailto:hello@example.com')

    expect(openExternal).toHaveBeenNthCalledWith(1, 'https://example.com/docs')
    expect(openExternal).toHaveBeenNthCalledWith(2, 'mailto:hello@example.com')
  })

  it('returns without a browser fallback when window is unavailable', async () => {
    delete desktopWindow.hermesDesktop
    vi.stubGlobal('window', undefined)

    try {
      await expect(openExternalLinkWithFallback('https://example.com/sign-in')).resolves.toBeUndefined()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('hides the trailing external-link icon by default', () => {
    installDesktopBridge()

    render(<ExternalLink href="https://example.com/path/to/resource">Example link</ExternalLink>)

    const link = screen.getByRole('link', { name: 'Example link' })
    expect(link.querySelector('svg')).toBeNull()
  })

  it('shows a trailing external-link icon when opted in', () => {
    installDesktopBridge()

    render(
      <ExternalLink href="https://example.com/path/to/resource" showExternalIcon>
        Example link
      </ExternalLink>
    )

    const link = screen.getByRole('link', { name: 'Example link' })
    expect(link.querySelector('svg')).toBeTruthy()
  })

  it('renders pretty links with fetched titles and no host suffix', async () => {
    const bridge = vi.fn().mockResolvedValue('From Fajardo: Full-Day Culebra Islands Catamaran Tour')
    installDesktopBridge({ fetchLinkTitle: bridge as unknown as Window['hermesDesktop']['fetchLinkTitle'] })

    const url =
      'https://www.getyourguide.com/culebra-island-l145468/from-fajardo-full-day-cordillera-islands-catamaran-tour-t19894/'

    render(<LinkifiedText text={`Read ${url}`} />)

    const link = screen.getByTitle(url)
    expect(link.textContent).toContain('From Fajardo Full Day Cordillera Islands Catamaran Tour')

    await waitFor(() => {
      expect(link.textContent).toContain('From Fajardo: Full-Day Culebra Islands Catamaran Tour')
    })
    expect(link.textContent).not.toContain('getyourguide.com')
  })

  it('shows host/path fallback when title is unavailable', () => {
    installDesktopBridge()
    const url = 'https://www.expedia.com/things-to-do/puerto-rico-el-yunque'

    render(<PrettyLink href={url} />)

    const link = screen.getByTitle(url)

    expect(link.textContent).toBe('Puerto Rico El Yunque')
  })

  it('ignores error-like fetched titles and falls back to slug label', async () => {
    const bridge = vi.fn().mockResolvedValue('GetYourGuide – Error')
    installDesktopBridge({ fetchLinkTitle: bridge as unknown as Window['hermesDesktop']['fetchLinkTitle'] })

    const url =
      'https://www.getyourguide.com/culebra-island-l145468/from-fajardo-full-day-cordillera-islands-catamaran-tour-t19894/'

    render(<PrettyLink href={url} />)

    const link = screen.getByTitle(url)
    await waitFor(() => {
      expect(link.textContent).toBe('From Fajardo Full Day Cordillera Islands Catamaran Tour')
    })
  })

  it('treats not-found fetched titles as unusable', async () => {
    const bridge = installTitleBridge('Page not found - Forgejo')

    await expect(fetchLinkTitle(FORGEJO_URL)).resolves.toBe('')
    expect(bridge).toHaveBeenCalledTimes(1)
  })

  it('keeps an authored fallbackLabel ahead of a fetched title, and skips the fetch', async () => {
    const bridge = installTitleBridge('Kinkolino Forgejo')

    // Chat markdown passes authored link text as `fallbackLabel`, not `label`.
    render(<PrettyLink fallbackLabel="FJ #101" href={FORGEJO_URL} />)

    const link = screen.getByTitle(FORGEJO_URL)

    await waitFor(() => {
      expect(link.textContent).toContain('FJ #101')
    })
    expect(link.textContent).not.toContain('Kinkolino Forgejo')
    expect(bridge).not.toHaveBeenCalled()
  })

  it('still resolves a title when no label was authored', async () => {
    const bridge = installTitleBridge('Homelab Ops Issue 101')

    render(<PrettyLink href={FORGEJO_URL} />)

    await waitFor(() => {
      expect(screen.getByTitle(FORGEJO_URL).textContent).toContain('Homelab Ops Issue 101')
    })
    expect(bridge).toHaveBeenCalledTimes(1)
  })

  it('normalizes scheme-less links before opening', () => {
    installDesktopBridge()

    render(<LinkifiedText text="Source expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure" />)

    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe(
      'https://expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure'
    )
  })

  it('explicitOnly skips bare filename/domain tokens and only links explicit URLs', () => {
    installDesktopBridge()

    render(
      <LinkifiedText
        explicitOnly
        pretty={false}
        text={'Report  https://paste.rs/abc\nagent.log  https://paste.rs/def\nerrors.log'}
      />
    )

    const links = screen.getAllByRole('link')
    expect(links.map(a => a.getAttribute('href'))).toEqual(['https://paste.rs/abc', 'https://paste.rs/def'])
    // Bare filename-shaped tokens stay as plain text, not links.
    expect(screen.queryByText(content => content.includes('agent.log'))).toBeTruthy()
    expect(links.some(a => (a.textContent ?? '').includes('.log'))).toBe(false)
  })

  it('without explicitOnly, bare filename tokens are still linkified (default behavior)', () => {
    installDesktopBridge()

    render(<LinkifiedText pretty={false} text="open agent.log please" />)

    const link = screen.getByRole('link', { name: 'agent.log' })
    expect(link.getAttribute('href')).toBe('https://agent.log')
  })

  it('prefixes a pretty link to a known host with its brand glyph', () => {
    installDesktopBridge()

    const url = 'https://github.com/NousResearch/hermes-agent/pull/123'

    render(<PrettyLink fallbackLabel="#123" href={url} />)

    const link = screen.getByTitle(url)

    expect(link.querySelector('svg')).toBeTruthy()
    // The glyph is decorative — it must not pollute the link's accessible name.
    expect(link.textContent).toBe('#123')
  })

  it('renders no brand glyph for an unknown host', () => {
    installDesktopBridge()

    const url = 'https://example.com/some/page'

    render(<PrettyLink fallbackLabel="Some Page" href={url} />)

    expect(screen.getByTitle(url).querySelector('svg')).toBeNull()
  })
})

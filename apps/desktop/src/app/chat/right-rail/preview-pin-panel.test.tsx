/**
 * The panel is the durable half of the feature: the engine dies with the page,
 * and everything that has to outlive a navigation lives here. These are the
 * behaviours that only exist at this level — closing hands the page back,
 * pages keep their own comments, and attaching sends the whole review.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PinEngineReport, PreviewPin } from '@/lib/preview-pins/types'
import { $composerAttachments } from '@/store/composer'

import { PreviewPinPanel } from './preview-pin-panel'

const browserWindow = vi.fn(() => false)
const relay = vi.fn(async (_attachment: unknown) => true)
const notified: { kind?: string; title?: string }[] = []

vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  isBrowserWindow: () => browserWindow()
}))

vi.mock('@/store/composer-relay', () => ({ relayComposerAttachment: (a: unknown) => relay(a) }))

vi.mock('@/store/notifications', () => ({
  notify: (input: { kind?: string; title?: string }) => {
    notified.push(input)

    return 'id'
  }
}))

const HOME = 'http://localhost:5178/en/index.html'
const ABOUT = 'http://localhost:5178/en/about.html'

/** The guest page, standing in for the engine: one bucket of pins per url. */
const page = {
  armed: false,
  hidden: false,
  pins: {} as Record<string, PreviewPin[]>,
  url: HOME
}

function report(): PinEngineReport {
  return {
    armed: page.armed,
    hidden: page.hidden,
    pendingShots: [],
    pins: page.pins[page.url] ?? [],
    url: page.url
  }
}

const hidePins = vi.fn(async () => {
  page.armed = false
  page.hidden = true

  return report()
})

const showPins = vi.fn(async (seed?: null | PreviewPin[]) => {
  page.hidden = false

  if (seed?.length) {page.pins[page.url] = seed}

  return report()
})

const armPins = vi.fn(async (seed?: null | PreviewPin[]) => {
  page.armed = true

  if (seed?.length) {page.pins[page.url] = seed}

  return report()
})

vi.mock('./preview-pins', () => ({
  armPins: (seed?: null | PreviewPin[]) => armPins(seed),
  clearPins: vi.fn(async () => {
    page.pins = {}

    return report()
  }),
  disarmPins: vi.fn(async () => {
    page.armed = false

    return report()
  }),
  hidePins: () => hidePins(),
  readPins: vi.fn(async () => report()),
  reattachPins: vi.fn(async (seed?: null | PreviewPin[]) => {
    // Mirrors the real seed filter: only pins belonging to this page, and only
    // when there is something to seed — buildScript skips an empty seed, so a
    // mock that honours `[]` would wipe the page the panel just opened.
    if (seed?.length) {page.pins[page.url] = seed.filter(pin => pin.pageUrl === page.url)}

    return report()
  }),
  removePin: vi.fn(async () => report()),
  showPins: (seed?: null | PreviewPin[]) => showPins(seed),
  takeShot: vi.fn(async () => report()),
  togglePinResolved: vi.fn(async () => report())
}))

function pin(pageUrl: string, comment: string, id = comment): PreviewPin {
  return {
    anchor: {
      label: comment,
      ordinal: 0,
      path: 'body>button',
      rect: { h: 0.1, w: 0.1, x: 0, y: 0 },
      role: 'button',
      selector: `#${id}`,
      text: comment
    },
    comment,
    createdAt: id.length,
    id,
    kind: 'element',
    pageUrl,
    resolved: false,
    target: comment
  }
}

beforeEach(() => {
  page.armed = false
  page.hidden = false
  page.pins = {}
  page.url = HOME
  $composerAttachments.set([])
  browserWindow.mockReturnValue(false)
  relay.mockResolvedValue(true)
  notified.length = 0
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('closing the panel', () => {
  it('hands the page back instead of leaving it armed', async () => {
    const view = render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(showPins).toHaveBeenCalled())

    view.rerender(<PreviewPinPanel open={false} url={HOME} />)

    // The reported bug: a closed panel that still swallows the next click.
    await waitFor(() => expect(hidePins).toHaveBeenCalled())
  })

  it('hides on unmount too, since a pane can go away without closing', async () => {
    const view = render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(showPins).toHaveBeenCalled())
    view.unmount()
    await waitFor(() => expect(hidePins).toHaveBeenCalled())
  })

  it('renders nothing while closed', () => {
    render(<PreviewPinPanel open={false} url={HOME} />)
    expect(screen.queryByText('Annotate')).toBeNull()
  })
})

describe('a review across pages', () => {
  it('keeps each page\'s comments and does not carry them over', async () => {
    page.pins[HOME] = [pin(HOME, 'hero')]
    const view = render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    page.url = ABOUT
    page.pins[ABOUT] = []
    view.rerender(<PreviewPinPanel open url={ABOUT} />)

    // The home page's comment must not reappear here as a detached pin.
    await waitFor(() => expect(screen.queryAllByText('hero')).toHaveLength(0))
    await waitFor(() => expect(screen.getByText(/1 on 1 other page/)).toBeTruthy())
  })

  it('gives a page its own comments back when the user returns', async () => {
    page.pins[HOME] = [pin(HOME, 'hero')]
    const view = render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    page.url = ABOUT
    page.pins[ABOUT] = [pin(ABOUT, 'team photo')]
    view.rerender(<PreviewPinPanel open url={ABOUT} />)
    await waitFor(() => expect(screen.getAllByText('team photo').length).toBeGreaterThan(0))

    page.url = HOME
    view.rerender(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))
  })

  it('attaches the whole review, not just the page in front of you', async () => {
    page.pins[HOME] = [pin(HOME, 'hero')]
    const view = render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    page.url = ABOUT
    page.pins[ABOUT] = [pin(ABOUT, 'team photo')]
    view.rerender(<PreviewPinPanel open url={ABOUT} />)
    await waitFor(() => expect(screen.getAllByText('team photo').length).toBeGreaterThan(0))

    screen.getByText('Attach to chat').click()

    await waitFor(() => expect($composerAttachments.get()).toHaveLength(1))
    const attachment = $composerAttachments.get()[0]
    expect(attachment.kind).toBe('pins')
    expect(attachment.label).toBe('2 comments')
    // Both pages in one payload — someone who commented on two pages meant one
    // request, not two.
    expect(attachment.detail).toContain('hero')
    expect(attachment.detail).toContain('team photo')
  })

  it('hands the chip to the window that owns the composer', async () => {
    // A popped-out Browser window has no composer of its own: adding there is a
    // click into a void, which is exactly how this was reported.
    browserWindow.mockReturnValue(true)
    page.pins[HOME] = [pin(HOME, 'hero')]
    render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    screen.getByText('Attach to chat').click()

    await waitFor(() => expect(relay).toHaveBeenCalled())
    expect($composerAttachments.get()).toHaveLength(0)
    await waitFor(() => expect(notified.at(-1)?.kind).toBe('success'))
  })

  it('keeps the chip in a window that has its own composer', async () => {
    // The regression this replaced: the guard was `isAuxiliaryWindow()`, which
    // also covers the secondary session window and the HUD. Both render a real
    // composer, so relaying handed the chip to the PRIMARY window — a success
    // toast in front of the user and the attachment one window away.
    browserWindow.mockReturnValue(false)
    page.pins[HOME] = [pin(HOME, 'hero')]
    render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    screen.getByText('Attach to chat').click()

    await waitFor(() => expect($composerAttachments.get()).toHaveLength(1))
    expect(relay).not.toHaveBeenCalled()
  })

  it('says so when there is nowhere to put it', async () => {
    browserWindow.mockReturnValue(true)
    relay.mockResolvedValue(false)
    page.pins[HOME] = [pin(HOME, 'hero')]
    render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    screen.getByText('Attach to chat').click()

    // Silence is what made the bug invisible; an error is the minimum.
    await waitFor(() => expect(notified.at(-1)?.kind).toBe('error'))
  })

  it('confirms out loud when it did land, since the composer may be off-screen', async () => {
    page.pins[HOME] = [pin(HOME, 'hero')]
    render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    screen.getByText('Attach to chat').click()

    await waitFor(() => expect($composerAttachments.get()).toHaveLength(1))
    await waitFor(() => expect(notified.at(-1)?.title).toBe('Added to chat'))
  })

  it('shows only the two newest, so the list never eats the page', async () => {
    page.pins[HOME] = ['one', 'two', 'three', 'four'].map((name, index) => ({
      ...pin(HOME, name),
      createdAt: index
    }))
    render(<PreviewPinPanel open url={HOME} />)

    await waitFor(() => expect(screen.getAllByText('four').length).toBeGreaterThan(0))
    expect(screen.getAllByText('three').length).toBeGreaterThan(0)
    // The oldest two are behind the toggle rather than pushing the preview down.
    expect(screen.queryAllByText('one')).toHaveLength(0)
    expect(screen.queryAllByText('two')).toHaveLength(0)

    screen.getByText('Show all 4').click()
    await waitFor(() => expect(screen.getAllByText('one').length).toBeGreaterThan(0))

    screen.getByText('Show fewer').click()
    await waitFor(() => expect(screen.queryAllByText('one')).toHaveLength(0))
  })

  it('keeps each row numbered as its own marker, not as its place in the list', async () => {
    page.pins[HOME] = ['one', 'two', 'three'].map((name, index) => ({
      ...pin(HOME, name),
      createdAt: index
    }))
    render(<PreviewPinPanel open url={HOME} />)

    // Newest first, but "3" is still the third pin placed — the page's marker
    // says 3 too, and the two must agree.
    await waitFor(() => expect(screen.getByText('3')).toBeTruthy())
    expect(screen.getByText('2')).toBeTruthy()
    expect(screen.queryByText('1')).toBeNull()
  })

  it('offers no toggle when everything already fits', async () => {
    page.pins[HOME] = [pin(HOME, 'only one')]
    render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('only one').length).toBeGreaterThan(0))
    expect(screen.queryByText(/Show all/)).toBeNull()
  })

  it('can still attach from a page that has nothing on it', async () => {
    page.pins[HOME] = [pin(HOME, 'hero')]
    const view = render(<PreviewPinPanel open url={HOME} />)
    await waitFor(() => expect(screen.getAllByText('hero').length).toBeGreaterThan(0))

    page.url = ABOUT
    page.pins[ABOUT] = []
    view.rerender(<PreviewPinPanel open url={ABOUT} />)
    await waitFor(() => expect(screen.getByText(/1 on 1 other page/)).toBeTruthy())

    const button = screen.getByText('Attach to chat') as HTMLButtonElement
    expect(button.disabled).toBe(false)
    button.click()
    await waitFor(() => expect($composerAttachments.get()).toHaveLength(1))
  })
})

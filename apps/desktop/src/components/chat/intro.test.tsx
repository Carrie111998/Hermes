import { cleanup, render, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { Intro } from './intro'

const WORDMARK = 'HERMES AGENT'

interface StubIntroBridge {
  hermesDesktop: {
    introImage: {
      get: () => Promise<{ imagePath: null | string; dataUrl: null | string; error: null | string }>
      set: (imagePath: null | string) => Promise<{ imagePath: null | string }>
      pick: () => Promise<{ canceled: boolean; imagePath: null | string }>
    }
  }
}

function stubBridge(value: { dataUrl: string } | { reject: Error } | null) {
  if (value === null) {
    vi.stubGlobal('window', { hermesDesktop: undefined })
    return
  }

  const get = 'dataUrl' in value
    ? vi.fn(async () => ({ imagePath: '/x.png', dataUrl: value.dataUrl, error: null }))
    : vi.fn(async () => {
        throw (value as { reject: Error }).reject
      })

  vi.stubGlobal('window', {
    hermesDesktop: {
      introImage: { get, set: vi.fn(), pick: vi.fn() }
    }
  } satisfies StubIntroBridge)
}

async function waitForWordmark(container: HTMLElement) {
  for (let i = 0; i < 50; i += 1) {
    const scope = within(container)
    if (scope.queryByLabelText(WORDMARK)) {
      return
    }
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error('Wordmark never rendered')
}

async function waitForImage(container: HTMLElement): Promise<string> {
  for (let i = 0; i < 50; i += 1) {
    const img = container.querySelector('img')
    if (img) {
      return img.getAttribute('src') ?? ''
    }
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error('Image never rendered')
}

describe('Intro', () => {
  beforeEach(() => {
    vi.stubGlobal('window', {})
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  test('renders the wordmark when no bridge is present', () => {
    const { container } = render(<Intro />)

    expect(within(container).getByLabelText(WORDMARK)).toBeTruthy()
    expect(within(container).queryByRole('img')).toBeNull()
  })

  test('renders the wordmark when the bridge returns no dataUrl', async () => {
    vi.stubGlobal('window', {
      hermesDesktop: {
        introImage: {
          get: vi.fn(async () => ({ imagePath: null, dataUrl: null, error: null })),
          set: vi.fn(),
          pick: vi.fn()
        }
      }
    })

    const { container } = render(<Intro />)

    await waitForWordmark(container)
    expect(within(container).queryByRole('img')).toBeNull()
  })

  test('renders the image when the bridge returns a dataUrl', async () => {
    stubBridge({ dataUrl: 'data:image/png;base64,AAAA' })

    const { container } = render(<Intro />)

    const src = await waitForImage(container)
    expect(src).toBe('data:image/png;base64,AAAA')
    expect(within(container).queryByLabelText(WORDMARK)).toBeNull()
  })

  test('falls back to wordmark when the bridge throws', async () => {
    stubBridge({ reject: new Error('boom') })

    const { container } = render(<Intro />)

    await waitForWordmark(container)
    expect(within(container).queryByRole('img')).toBeNull()
  })
})
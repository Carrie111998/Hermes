import { cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { IntroImageSettings } from './intro-image-settings'

interface StubIntroBridge {
  hermesDesktop: {
    introImage: {
      get: () => Promise<{ imagePath: null | string; dataUrl: null | string; error: null | string }>
      set: (imagePath: null | string) => Promise<{ imagePath: null | string }>
      pick: () => Promise<{ canceled: boolean; imagePath: null | string }>
    }
  }
}

function stubBridge({
  get,
  set,
  pick
}: {
  get: () => Promise<{ imagePath: null | string; dataUrl: null | string; error: null | string }>
  set?: (imagePath: null | string) => Promise<{ imagePath: null | string }>
  pick?: () => Promise<{ canceled: boolean; imagePath: null | string }>
}) {
  vi.stubGlobal('window', {
    hermesDesktop: {
      introImage: {
        get,
        set: set ?? vi.fn(async (imagePath: null | string) => ({ imagePath })),
        pick: pick ?? vi.fn(async () => ({ canceled: true, imagePath: null }))
      }
    }
  } satisfies StubIntroBridge)
}

const EMPTY_GET = vi.fn(async () => ({ imagePath: null, dataUrl: null, error: null }))

describe('IntroImageSettings', () => {
  beforeEach(() => {
    vi.stubGlobal('window', {})
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  test('shows the empty state when no image is configured', async () => {
    stubBridge({ get: EMPTY_GET })

    const { container } = render(<IntroImageSettings />)

    await new Promise(resolve => setTimeout(resolve, 10))

    const scope = within(container)
    expect(scope.queryByRole('img')).toBeNull()
    expect(scope.getByRole('button', { name: /choose image/i })).toBeTruthy()
  })

  test('renders the preview when a valid image is set', async () => {
    stubBridge({
      get: vi.fn(async () => ({
        imagePath: '/x.png',
        dataUrl: 'data:image/png;base64,AAAA',
        error: null
      }))
    })

    const { container } = render(<IntroImageSettings />)

    await new Promise(resolve => setTimeout(resolve, 10))

    const img = container.querySelector('img')
    expect(img?.getAttribute('src')).toBe('data:image/png;base64,AAAA')
    expect(within(container).getByRole('button', { name: /clear/i })).toBeTruthy()
  })

  test('renders the error when the image is unreadable', async () => {
    stubBridge({
      get: vi.fn(async () => ({
        imagePath: '/missing.png',
        dataUrl: null,
        error: 'Intro image failed: file does not exist.'
      }))
    })

    const { container } = render(<IntroImageSettings />)

    await new Promise(resolve => setTimeout(resolve, 10))

    expect(container.textContent).toMatch(/Intro image failed/)
    expect(within(container).getByRole('button', { name: /clear/i })).toBeTruthy()
  })

  test('pick → set persists the chosen path', async () => {
    const set = vi.fn(async (imagePath: null | string) => ({ imagePath }))
    const pick = vi.fn(async () => ({ canceled: false, imagePath: '/picked.png' }))

    stubBridge({
      get: vi.fn(async () => ({
        imagePath: null,
        dataUrl: null,
        error: null
      })),
      set,
      pick
    })

    const { container } = render(<IntroImageSettings />)

    await new Promise(resolve => setTimeout(resolve, 10))

    const chooseBtn = within(container).getByRole('button', { name: /choose image/i })
    fireEvent.click(chooseBtn)

    await new Promise(resolve => setTimeout(resolve, 50))

    expect(pick).toHaveBeenCalledTimes(1)
    expect(set).toHaveBeenCalledWith('/picked.png')
  })

  test('clear → set(null) wipes the preview', async () => {
    const set = vi.fn(async (imagePath: null | string) => ({ imagePath }))

    stubBridge({
      get: vi.fn(async () => ({
        imagePath: '/picked.png',
        dataUrl: 'data:image/png;base64,BB',
        error: null
      })),
      set
    })

    const { container } = render(<IntroImageSettings />)

    await new Promise(resolve => setTimeout(resolve, 10))

    const clearBtn = within(container).getByRole('button', { name: /clear/i })
    fireEvent.click(clearBtn)

    await new Promise(resolve => setTimeout(resolve, 50))

    expect(set).toHaveBeenCalledWith(null)
    expect(container.querySelector('img')).toBeNull()
  })
})
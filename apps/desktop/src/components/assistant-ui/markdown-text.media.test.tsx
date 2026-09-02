import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { MarkdownImage, MarkdownTextContent } from './markdown-text'

const REMOTE_IMAGE_PATH = '/home/user/project/images/remote-preview.png'
const REMOTE_IMAGE_DATA_URL = 'data:image/png;base64,cmVtb3RlLWltYWdl'

describe('MarkdownTextContent remote images', () => {
  const api = vi.fn(async ({ path }: { path: string }) => {
    if (path.startsWith('/api/fs/read-data-url?')) {
      return { dataUrl: REMOTE_IMAGE_DATA_URL }
    }

    throw new Error(`unexpected path ${path}`)
  })

  let originalDesktop: typeof window.hermesDesktop

  beforeEach(() => {
    api.mockClear()
    originalDesktop = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api }
    })
    $connection.set({ mode: 'remote', profile: 'remote-work' } as never)
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: originalDesktop
    })
  })

  it('passes the gateway bridge data URL through Streamdown to the zoomable image', async () => {
    render(<MarkdownTextContent isRunning={false} text={`![Remote preview](${REMOTE_IMAGE_PATH})`} />)

    const image = await screen.findByRole('img', { name: 'Remote preview' })

    expect(image.getAttribute('src')).toBe(REMOTE_IMAGE_DATA_URL)
    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2Fhome%2Fuser%2Fproject%2Fimages%2Fremote-preview.png',
      profile: 'remote-work'
    })
  })
})

// Regression for #40896: generated media often arrives as image markdown
// (`![clip](clip.mp4)`). A raw <img> with a video/audio source paints a
// broken-image icon even though the file is valid, so MarkdownImage must route
// video/audio sources to the proper <video>/<audio> element.
describe('MarkdownImage media routing', () => {
  afterEach(cleanup)

  it('renders a <video> (not a broken <img>) for a video source', async () => {
    const { container } = render(<MarkdownImage alt="clip" src="file:///tmp/clip.mp4" />)

    await waitFor(() => expect(container.querySelector('video')).not.toBeNull())
    expect(container.querySelector('img')).toBeNull()
  })

  it('renders an <audio> element for an audio source', async () => {
    const { container } = render(<MarkdownImage alt="note" src="file:///tmp/note.mp3" />)

    await waitFor(() => expect(container.querySelector('audio')).not.toBeNull())
    expect(container.querySelector('img')).toBeNull()
  })

  it('still renders an <img> for an image source', () => {
    const { container } = render(<MarkdownImage alt="pic" src="file:///tmp/pic.png" />)

    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('audio')).toBeNull()
  })
})

// Regression for the "fullscreen button does nothing" report: Electron's
// renderer never services macOS's native media-controls fullscreen button, so
// chat videos must expose our own HTML5-fullscreen toggle (labeled button +
// double-click) instead of relying on the native controls.
describe('MediaAttachment video fullscreen', () => {
  afterEach(cleanup)

  const renderVideo = async (text = `[clip](#media:%2Ftmp%2Fclip.mp4)`) => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={text} />)

    await waitFor(() => expect(container.querySelector('video')).not.toBeNull())

    return container
  }

  it('exposes a labeled Fullscreen button next to the filename', async () => {
    const container = await renderVideo()

    expect(screen.getByRole('button', { name: 'Fullscreen' })).not.toBeNull()
    // The native controls stay on for play/seek/volume.
    expect(container.querySelector('video[controls]')).not.toBeNull()
  })

  it('requests element fullscreen on the video when clicked', async () => {
    const container = await renderVideo()
    const video = container.querySelector('video')!

    const requestFullscreen = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(video, 'requestFullscreen', { configurable: true, value: requestFullscreen })

    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))

    expect(requestFullscreen).toHaveBeenCalledTimes(1)
  })

  it('toggles via double-click and exits when already fullscreen', async () => {
    const container = await renderVideo()
    const video = container.querySelector('video')!

    const requestFullscreen = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(video, 'requestFullscreen', { configurable: true, value: requestFullscreen })
    const exitFullscreen = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window.document, 'exitFullscreen', { configurable: true, value: exitFullscreen })
    Object.defineProperty(window.document, 'fullscreenElement', {
      configurable: true,
      get: () => null
    })

    fireEvent.doubleClick(video)
    expect(requestFullscreen).toHaveBeenCalledTimes(1)

    Object.defineProperty(window.document, 'fullscreenElement', {
      configurable: true,
      get: () => video
    })

    fireEvent.doubleClick(video)
    expect(exitFullscreen).toHaveBeenCalledTimes(1)

    delete (window.document as any).fullscreenElement
    delete (window.document as any).exitFullscreen
  })

  it('swallows a rejected requestFullscreen instead of throwing', async () => {
    const container = await renderVideo()

    Object.defineProperty(container.querySelector('video')!, 'requestFullscreen', {
      configurable: true,
      value: vi.fn().mockRejectedValue(new Error('denied'))
    })

    expect(() => fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))).not.toThrow()
  })
})


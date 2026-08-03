import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LogsPane } from './panes'

const getLogs = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal()),
  getLogs: (...args: unknown[]) => getLogs(...args)
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  getLogs.mockResolvedValue({ lines: ['first line'] })
})

async function renderLogs() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <LogsPane />
    </QueryClientProvider>
  )

  await screen.findByText('first line')
  return client
}

function setScrollMetrics(element: HTMLElement, { clientHeight, scrollHeight, scrollTop }: Record<string, number>) {
  Object.defineProperties(element, {
    clientHeight: { configurable: true, value: clientHeight },
    scrollHeight: { configurable: true, value: scrollHeight },
    scrollTop: { configurable: true, value: scrollTop, writable: true }
  })
}

describe('LogsPane', () => {
  it('keeps log text selectable and follows new logs when the reader is already at the bottom', async () => {
    const client = await renderLogs()
    const logs = screen.getByLabelText('Agent logs')

    expect(logs.classList.contains('select-text')).toBe(true)

    setScrollMetrics(logs, { clientHeight: 100, scrollHeight: 500, scrollTop: 400 })
    fireEvent.scroll(logs)
    setScrollMetrics(logs, { clientHeight: 100, scrollHeight: 800, scrollTop: 400 })

    await act(async () => {
      client.setQueryData(['contrib-logs-tail'], { lines: ['first line', 'new line'] })
    })

    await waitFor(() => expect(logs.scrollTop).toBe(800))
  })

  it('does not pull a reader away from older logs when new lines arrive', async () => {
    const client = await renderLogs()
    const logs = screen.getByLabelText('Agent logs')

    setScrollMetrics(logs, { clientHeight: 100, scrollHeight: 500, scrollTop: 100 })
    fireEvent.scroll(logs)
    setScrollMetrics(logs, { clientHeight: 100, scrollHeight: 800, scrollTop: 100 })

    await act(async () => {
      client.setQueryData(['contrib-logs-tail'], { lines: ['first line', 'new line'] })
    })

    expect(logs.scrollTop).toBe(100)
  })
})

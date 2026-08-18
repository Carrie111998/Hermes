import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { TrajectoryPanel } from './trajectory-panel'

const messages: ChatMessage[] = [
  { id: 'u1', role: 'user', parts: [{ type: 'text', text: 'Inspect repo', timestamp: 1 }] },
  {
    id: 'a1',
    role: 'assistant',
    parts: [
      {
        type: 'tool-call',
        toolCallId: 'call-1',
        toolName: 'read_file',
        args: { path: 'README.md' },
        argsText: '{"path":"README.md"}',
        result: { preview: 'ok' },
        timestamp: 2,
        completedAt: 2.25
      },
      { type: 'text', text: 'Finished', timestamp: 3 }
    ]
  }
]

describe('TrajectoryPanel', () => {
  it('shows live summary and searchable trajectory records', () => {
    render(<TrajectoryPanel messages={messages} model="configured-model" provider="configured-provider" />)

    expect(screen.getByText('Turns')).toBeTruthy()
    expect(screen.getByText('Calls')).toBeTruthy()
    expect(screen.getByText('read_file')).toBeTruthy()

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search trajectory' }), {
      target: { value: 'Finished' }
    })

    expect(screen.queryByText('read_file')).toBeNull()
    expect(screen.getByText('Finished')).toBeTruthy()
  })

  it('renders a dense execution overview with lanes and inline tool results', () => {
    render(<TrajectoryPanel messages={messages} model="configured-model" provider="configured-provider" />)

    const overview = screen.getByRole('region', { name: 'Execution overview' })

    expect(within(overview).getByText('Input')).toBeTruthy()
    expect(within(overview).getByText('Model')).toBeTruthy()
    expect(within(overview).getByText('Tools')).toBeTruthy()
    const toolRow = screen.getByRole('button', { name: /read_file/ })
    expect(toolRow.textContent).toContain('→')
    expect(toolRow.textContent).toContain('"preview":"ok"')
  })

  it('opens payload and result details for a selected tool', () => {
    render(<TrajectoryPanel messages={messages} model="configured-model" provider="configured-provider" />)

    fireEvent.click(screen.getByRole('button', { name: /read_file/ }))

    expect(screen.getByText('Payload')).toBeTruthy()
    expect(screen.getAllByText(/README\.md/).length).toBeGreaterThan(0)
    expect(screen.getByText('Result')).toBeTruthy()
    expect(screen.getAllByText(/preview/).length).toBeGreaterThan(0)
  })
})

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { ProfileInfo } from '@/types/hermes'

import { ProfileDropdown } from './profile-switcher'

class TestResizeObserver {
  disconnect() {}
  observe() {}
  unobserve() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      profiles: {
        noSearchResults: 'No profiles found.',
        search: 'Search profiles...',
        title: 'Profiles'
      }
    }
  })
}))

vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm: vi.fn(), startPrewarm: vi.fn() })
}))

const profile = (name: string): ProfileInfo => ({
  has_env: false,
  is_default: false,
  model: null,
  name,
  path: `/profiles/${name}`,
  provider: null,
  skill_count: 0
})

const profiles = [profile('vps'), profile('agency-ai-engineer'), profile('agency-audio-designer')]

describe('ProfileDropdown', () => {
  it('filters profile suggestions case-insensitively and selects one', () => {
    const onSelect = vi.fn()

    render(<ProfileDropdown activeKey="vps" colors={{}} onSelect={onSelect} profiles={profiles} />)

    fireEvent.click(screen.getByRole('combobox', { name: 'Profiles' }))

    const input = screen.getByPlaceholderText('Search profiles...')
    expect(input.ownerDocument.activeElement).toBe(input)

    fireEvent.change(input, { target: { value: 'AUDIO' } })

    expect(screen.getByRole('option', { name: 'agency-audio-designer' })).not.toBeNull()
    expect(screen.queryByRole('option', { name: 'agency-ai-engineer' })).toBeNull()

    fireEvent.click(screen.getByRole('option', { name: 'agency-audio-designer' }))

    expect(onSelect).toHaveBeenCalledWith('agency-audio-designer')
    expect(screen.queryByPlaceholderText('Search profiles...')).toBeNull()
  })

  it('supports keyboard autocomplete selection', () => {
    const onSelect = vi.fn()

    render(<ProfileDropdown activeKey={null} colors={{}} onSelect={onSelect} profiles={profiles} />)

    fireEvent.click(screen.getByRole('combobox', { name: 'Profiles' }))
    const input = screen.getByPlaceholderText('Search profiles...')

    fireEvent.change(input, { target: { value: 'engineer' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(onSelect).toHaveBeenCalledWith('agency-ai-engineer')
  })

  it('shows an empty state and resets the query after closing', () => {
    render(<ProfileDropdown activeKey={null} colors={{}} onSelect={vi.fn()} profiles={profiles} />)

    const trigger = screen.getByRole('combobox', { name: 'Profiles' })
    fireEvent.click(trigger)

    const input = screen.getByPlaceholderText('Search profiles...')
    fireEvent.change(input, { target: { value: 'missing-profile' } })

    expect(screen.getByText('No profiles found.')).not.toBeNull()

    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByPlaceholderText('Search profiles...')).toBeNull()

    fireEvent.click(trigger)
    expect((screen.getByPlaceholderText('Search profiles...') as HTMLInputElement).value).toBe('')
    expect(screen.getAllByRole('option')).toHaveLength(profiles.length)
  })
})

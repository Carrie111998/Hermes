import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $desktopOnboarding, type DesktopOnboardingState, type OnboardingContext } from '@/store/onboarding'
import type { OAuthProvider } from '@/types/hermes'

import { DesktopOnboardingOverlay, Picker } from '.'

function provider(id: string, name = id): OAuthProvider {
  return {
    cli_command: `hermes login ${id}`,
    docs_url: `https://example.com/${id}`,
    flow: 'pkce',
    id,
    name,
    status: { logged_in: false }
  }
}

function setProviders(providers: OAuthProvider[]) {
  $desktopOnboarding.set({
    configured: false,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  } satisfies DesktopOnboardingState)
}

const ctx: OnboardingContext = { requestGateway: async () => undefined as never }

afterEach(() => {
  cleanup()
  Reflect.deleteProperty(window, 'hermesDesktop')

  try {
    window.localStorage.clear()
  } catch {
    // jsdom localStorage should always be present; ignore if not.
  }

  $desktopOnboarding.set({
    configured: null,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
})

describe('onboarding Picker', () => {
  it('does not cover managed Eva enrollment with the local provider picker', () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { eva: {} },
      writable: true
    })

    render(
      <DesktopOnboardingOverlay
        enabled={false}
        profile="default"
        requestGateway={async () => undefined as never}
      />
    )

    expect(screen.queryByText("Let's connect Eva to your assigned agent")).toBeNull()
    expect(screen.queryByText('Starting Eva…')).toBeNull()
  })

  it('allows a manually requested provider flow in managed mode', () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { eva: {} },
      writable: true
    })
    $desktopOnboarding.set({
      configured: true,
      flow: { status: 'idle' },
      mode: 'oauth',
      providers: [provider('openai-codex', 'OpenAI Codex / ChatGPT')],
      reason: null,
      requested: true,
      firstRunSkipped: false,
      manual: true,
      localEndpoint: false
    })

    render(
      <DesktopOnboardingOverlay enabled={false} profile="default" requestGateway={async () => undefined as never} />
    )

    expect(screen.getByText('Connect evaOS Agent to your assigned agent')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Close' })).toBeTruthy()
  })

  it('keeps OpenAI available without managed Nous or Fireworks promotions', () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { eva: {} },
      writable: true
    })
    setProviders([
      provider('nous', 'Nous Portal'),
      provider('openai-codex', 'OpenAI Codex / ChatGPT'),
      provider('anthropic', 'Anthropic Claude')
    ])
    $desktopOnboarding.set({ ...$desktopOnboarding.get(), manual: true })

    render(<Picker ctx={ctx} />)

    expect(screen.queryByText('Electric Sheep account')).toBeNull()
    expect(screen.queryByText('Fireworks AI')).toBeNull()
    expect(screen.getByText('OpenAI OAuth (ChatGPT)')).toBeTruthy()
    expect(screen.getByText('Anthropic API Key')).toBeTruthy()
  })

  it('features the Electric Sheep account and hides other providers behind a disclosure', () => {
    setProviders([provider('anthropic', 'Anthropic Claude'), provider('nous', 'Nous Portal')])
    render(<Picker ctx={ctx} />)

    expect(screen.getByText('Electric Sheep account')).toBeTruthy()
    expect(screen.getByText('Recommended')).toBeTruthy()
    // Fireworks is the always-visible #2 slot (after Nous), even while OAuth
    // alternatives stay collapsed behind the disclosure.
    expect(screen.getByText('Fireworks AI')).toBeTruthy()
    expect(screen.queryByText('Anthropic API Key')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Other providers' }))

    expect(screen.getByText('Anthropic API Key')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Collapse' })).toBeTruthy()
  })

  it('shows Fireworks in slot #2 ahead of other OAuth providers', () => {
    setProviders([
      provider('openai-codex', 'OpenAI Codex / ChatGPT'),
      provider('minimax-oauth', 'MiniMax'),
      provider('nous', 'Nous Portal')
    ])
    render(<Picker ctx={ctx} />)
    fireEvent.click(screen.getByRole('button', { name: 'Other providers' }))

    const labels = screen
      .getAllByRole('button')
      .map(el => el.textContent ?? '')
      .filter(text => /Electric Sheep account|Fireworks AI|OpenAI OAuth|MiniMax|OpenRouter/.test(text))

    const indexOf = (needle: string) => labels.findIndex(text => text.includes(needle))
    expect(indexOf('Electric Sheep account')).toBeGreaterThanOrEqual(0)
    expect(indexOf('Fireworks AI')).toBeGreaterThan(indexOf('Electric Sheep account'))
    expect(indexOf('OpenAI OAuth')).toBeGreaterThan(indexOf('Fireworks AI'))
    expect(indexOf('MiniMax')).toBeGreaterThan(indexOf('OpenAI OAuth'))
  })

  it('shows every provider directly when Nous Portal is absent', () => {
    setProviders([provider('anthropic', 'Anthropic Claude'), provider('openai-codex', 'OpenAI Codex / ChatGPT')])
    render(<Picker ctx={ctx} />)

    expect(screen.getByText('Fireworks AI')).toBeTruthy()
    expect(screen.getByText('Anthropic API Key')).toBeTruthy()
    expect(screen.getByText('OpenAI OAuth (ChatGPT)')).toBeTruthy()
    expect(screen.queryByText('Other sign-in options')).toBeNull()
    expect(screen.queryByText('Recommended')).toBeNull()
  })

  it('offers "choose later" on first run and persists the skip', () => {
    setProviders([provider('nous', 'Nous Portal')])
    render(<Picker ctx={ctx} />)

    const skip = screen.getByRole('button', { name: "I'll choose a provider later" })

    fireEvent.click(skip)

    expect($desktopOnboarding.get().firstRunSkipped).toBe(true)
    expect(window.localStorage.getItem('hermes-onboarding-skipped-v1')).toBe('1')
  })

  it('hides "choose later" in manual (add-provider) mode', () => {
    setProviders([provider('nous', 'Nous Portal')])
    $desktopOnboarding.set({ ...$desktopOnboarding.get(), manual: true })
    render(<Picker ctx={ctx} />)

    expect(screen.queryByRole('button', { name: "I'll choose a provider later" })).toBeNull()
  })
})

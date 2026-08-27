import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it } from 'vitest'

import type { ChatBarState } from '@/app/chat/composer/types'
import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { modelOptionsQueryKey } from '@/lib/model-options'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $currentModel, setCurrentModel, setCurrentModelSource } from '@/store/session'

import { ModelPill } from './model-pill'

const modelState = (over: Partial<ChatBarState['model']> = {}): ChatBarState['model'] => ({
  canSwitch: true,
  model: 'gpt-6',
  provider: 'openai',
  ...over
})

// #96063: the pill subscribes to the profile-scoped model-options cache via
// useQuery (enabled:false) so a config default change re-paints the tag. That
// hook needs a QueryClient in context; tests that don't care about the cache
// still wrap with an empty one so the hook doesn't throw.
function withQueryClient(node: React.ReactNode, queryClient: QueryClient = new QueryClient()) {
  return <QueryClientProvider client={queryClient}>{node}</QueryClientProvider>
}

afterEach(() => {
  cleanup()
  $activeGatewayProfile.set('default')
  $activeSessionId.set(null)
  setCurrentModel('')
  setCurrentModelSource('')
})

// #62055: a manual composer pick is sticky and silently overrides the
// Settings → Model default for every NEW chat. The pill must say so.
describe('ModelPill pinned-override badge', () => {
  it('shows the pin dot on a draft running a manual pick', () => {
    setCurrentModel('deepseek/deepseek-v4-flash')
    setCurrentModelSource('manual')
    $activeSessionId.set(null)

    render(withQueryClient(<ModelPill disabled={false} model={modelState({ model: 'deepseek/deepseek-v4-flash' })} />))

    expect(screen.getByTestId('model-pinned-dot')).toBeTruthy()
  })

  it('stays quiet when the composer reflects the profile default', () => {
    setCurrentModel('google/gemma-4-26b-a4b-it:free')
    setCurrentModelSource('default')
    $activeSessionId.set(null)

    render(withQueryClient(<ModelPill disabled={false} model={modelState()} />))

    expect(screen.queryByTestId('model-pinned-dot')).toBeNull()
  })

  it('stays quiet on a live session (footer shows that session, not the pin)', () => {
    setCurrentModel('deepseek/deepseek-v4-flash')
    setCurrentModelSource('manual')
    $activeSessionId.set('live-1')

    render(withQueryClient(<ModelPill disabled={false} model={modelState()} />))

    expect(screen.queryByTestId('model-pinned-dot')).toBeNull()
  })

  it('is exercised in both render paths', () => {
    setCurrentModel('deepseek/deepseek-v4-flash')
    setCurrentModelSource('manual')
    $activeSessionId.set(null)

    // Fallback (no live menu) path.
    const { unmount } = render(
      withQueryClient(<ModelPill disabled={false} model={modelState({ model: 'deepseek/deepseek-v4-flash' })} />)
    )

    expect(screen.getByTestId('model-pinned-dot')).toBeTruthy()
    unmount()

    // Live-menu (dropdown) path.
    render(
      withQueryClient(
        <ModelPill
          disabled={false}
          model={modelState({ model: 'deepseek/deepseek-v4-flash', modelMenuContent: <div /> })}
        />
      )
    )
    expect(screen.getByTestId('model-pinned-dot')).toBeTruthy()
    expect($currentModel.get()).toBe('deepseek/deepseek-v4-flash')
  })
})

describe('ModelPill per-surface model label', () => {
  it('shows the chat-bar model even when the primary global differs', () => {
    setCurrentModel('primary/model')
    $activeSessionId.set('primary-runtime')

    const tileView: SessionView = {
      kind: 'tile',
      $awaitingResponse: atom(false),
      $busy: atom(false),
      $cwd: atom(''),
      $fast: atom(false),
      $lastVisibleIsUser: atom(false),
      $messages: atom([]),
      $messagesEmpty: atom(true),
      $model: atom('tile/claude-sonnet'),
      $provider: atom('anthropic'),
      $reasoningEffort: atom('high'),
      $runtimeId: atom('tile-runtime'),
      $storedId: atom('stored-tile'),
      $turnStartedAt: atom<number | null>(null)
    }

    render(
      withQueryClient(
        <SessionViewProvider value={tileView}>
          <ModelPill
            disabled={false}
            model={modelState({ model: 'tile/claude-sonnet', provider: 'anthropic', modelMenuContent: <div /> })}
          />
        </SessionViewProvider>
      )
    )

    expect(screen.getByText('Sonnet · High')).toBeTruthy()
    expect(screen.queryByText(/primary/i)).toBeNull()
  })
})

// #96063: a stale persisted provider on a same-named model would silently send
// API calls to the wrong gateway. The pill must (1) visibly name the live
// provider when it differs from the Settings → Model default, (2) keep the
// tooltip / aria-label informative for hover and screen readers, and
// (3) stay quiet when the live provider IS the default so the pill doesn't
// grow noise for the common case.
describe('ModelPill non-default provider tag (#96063)', () => {
  const renderWithDefaults = (defaultProvider: string, liveProvider: string, model = 'qwen3.7-plus') => {
    $activeGatewayProfile.set('default')
    const queryClient = new QueryClient()

    queryClient.setQueryData(modelOptionsQueryKey('default'), {
      model,
      provider: defaultProvider,
      providers: [{ models: [model], name: defaultProvider, slug: defaultProvider }]
    })

    return render(
      withQueryClient(
        <ModelPill
          disabled={false}
          model={modelState({
            canSwitch: false,
            model,
            provider: liveProvider,
            modelMenuContent: <div data-testid="live-menu" />
          })}
        />,
        queryClient
      )
    )
  }

  it('renders the visible provider tag when the live provider differs from the config default', () => {
    renderWithDefaults('custom:aliyun-coding-plan', 'custom:token-plan-a')

    // The pill text now carries both the model name AND the live provider,
    // so a same-named model on a different provider can no longer look
    // identical (#96063).
    const tag = screen.getByTestId('model-provider-tag')

    expect(tag.textContent).toBe('· custom:token-plan-a')
    expect(tag.getAttribute('aria-hidden')).toBe('true')
  })

  it('keeps the tooltip / aria-label informative when the provider has drifted', () => {
    renderWithDefaults('custom:aliyun-coding-plan', 'custom:token-plan-a')

    // The button's aria-label is the same string the Tip renders; it must
    // name BOTH the live provider and the config default so screen readers
    // and hover both surface the desync.
    const pill = screen.getByRole('button', { name: /custom:token-plan-a/ })

    expect(pill.getAttribute('aria-label')).toContain('custom:token-plan-a')
    expect(pill.getAttribute('aria-label')).toContain('custom:aliyun-coding-plan')
    expect(pill.getAttribute('aria-label')).toContain('not the Settings default')
  })

  it('stays quiet when the live provider IS the config default', () => {
    renderWithDefaults('openai', 'openai')

    // Common case: provider matches default → no tag, no extra tooltip noise.
    expect(screen.queryByTestId('model-provider-tag')).toBeNull()

    const pill = screen.getByRole('button')

    expect(pill.getAttribute('aria-label')).not.toContain('not the Settings default')
  })

  it('stays quiet when the config default has not loaded yet', () => {
    $activeGatewayProfile.set('default')
    const queryClient = new QueryClient()

    // No cache data → defaultProvider resolves to '' → no false-positive tag.
    render(
      withQueryClient(
        <ModelPill
          disabled={false}
          model={modelState({
            canSwitch: false,
            model: 'qwen3.7-plus',
            provider: 'custom:token-plan-a',
            modelMenuContent: <div />
          })}
        />,
        queryClient
      )
    )

    expect(screen.queryByTestId('model-provider-tag')).toBeNull()
  })
})

// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import type * as ReactRouterDom from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { queryClient } from '@/lib/query-client'

const getSkills = vi.fn()
const getToolsets = vi.fn()
const setSkillEnabled = vi.fn()
const setToolsetEnabled = vi.fn()
const getToolsetConfig = vi.fn()
const selectToolsetProvider = vi.fn()
const getUsageAnalytics = vi.fn()
const getProfiles = vi.fn()
const getSkillContent = vi.fn()

// Partial mock: keep the real module (SkillsView pulls in @/store/profile,
// whose import-time subscription calls setApiRequestProfile) and stub only the
// calls we assert on. Args are forwarded so the per-profile scope arg is
// observable.
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getSkills: (profile?: null | string) => getSkills(profile),
  getToolsets: (profile?: null | string) => getToolsets(profile),
  setSkillEnabled: (name: string, enabled: boolean, profile?: null | string) => setSkillEnabled(name, enabled, profile),
  setToolsetEnabled: (name: string, enabled: boolean, profile?: null | string) =>
    setToolsetEnabled(name, enabled, profile),
  getToolsetConfig: (name: string, profile?: null | string) => getToolsetConfig(name, profile),
  selectToolsetProvider: (toolset: string, provider: string) => selectToolsetProvider(toolset, provider),
  getUsageAnalytics: (days: number, profile?: null | string) => getUsageAnalytics(days, profile),
  getProfiles: () => getProfiles(),
  getSkillContent: (name: string, profile?: null | string) => getSkillContent(name, profile)
}))

// Notifications hit nanostores/timers we don't care about here.
vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

// The vision detail navigates to Settings → Models via useNavigate; spy on it
// so the deep-link target is assertable.
const navigateSpy = vi.fn()

vi.mock('react-router', async importOriginal => ({
  ...(await importOriginal<typeof ReactRouterDom>()),
  useNavigate: () => navigateSpy
}))

function toolset(overrides: Record<string, unknown> = {}) {
  return {
    name: 'web',
    label: 'Web Search',
    description: 'web_search, web_extract',
    enabled: true,
    available: true,
    configured: true,
    tools: ['web_search', 'web_extract'],
    ...overrides
  }
}

async function renderSkills() {
  const { SkillsView } = await import('./index')
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      // SkillsView reads skills/toolsets via useQuery, so it needs a provider.
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/skills?tab=toolsets']}>
          <SkillsView />
        </MemoryRouter>
      </QueryClientProvider>
    )
  })

  return result!
}

beforeEach(() => {
  getSkills.mockResolvedValue([])
  getToolsets.mockResolvedValue([toolset()])
  setToolsetEnabled.mockResolvedValue({ ok: true, name: 'web', enabled: false })
  getToolsetConfig.mockResolvedValue({ has_category: true, active_provider: null, providers: [] })
  getUsageAnalytics.mockResolvedValue({ tools: [] })
  getSkillContent.mockResolvedValue({
    name: 'web-research',
    path: '/skills/web-research/SKILL.md',
    content: '---\nname: web-research\nversion: 1.2.0\nauthor: Nous\n---\n\n# Web Research\n\nDeep research steps.'
  })
  // Single profile by default → the scope selector stays hidden (>1 gate),
  // so existing tests see unchanged single-profile behavior.
  getProfiles.mockResolvedValue({ profiles: [{ name: 'default', is_default: true }] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  // Shared singleton client — drop cached skills/toolsets so each test refetches.
  queryClient.clear()
})

// SkillsView is a heavy module: the first test pays the whole dynamic-import
// cost, and the file legitimately runs ~14s on CI runners — right against the
// global 15s per-test budget, so slow runners cascade-fail all 11 tests
// (2× in a row on PR #93612, plus a main run the same hour). Give this file
// headroom; the tests are not slow individually.
describe('SkillsView toolset management', { timeout: 60_000 }, () => {
  it('renders a switch for each toolset and toggles it off', async () => {
    await renderSkills()

    // The switch names the action, so an enabled toolset offers to turn it off.
    const sw = await screen.findByRole('switch', { name: 'Turn Web Search toolset off' })
    expect(sw.getAttribute('aria-checked')).toBe('true')

    await act(async () => {
      fireEvent.click(sw)
    })

    await waitFor(() => expect(setToolsetEnabled).toHaveBeenCalled())
    expect(setToolsetEnabled.mock.calls[0].slice(0, 2)).toEqual(['web', false])
  })

  it('renders toolset titles without leading emoji', async () => {
    getToolsets.mockResolvedValue([toolset({ name: 'cronjob', label: '⏰ Cron Jobs', description: 'cron tools' })])

    await renderSkills()

    // The label renders in both the row and the auto-selected detail header, so
    // assert via the switch's (emoji-stripped) accessible name and the absence
    // of the emoji rather than a single-match text lookup.
    await screen.findByRole('switch', { name: 'Turn Cron Jobs toolset off' })
    expect(screen.queryByText(/⏰/)).toBeNull()
  })

  it('renders the provider config panel inline for the selected toolset', async () => {
    // The master-detail UI dropped the resting "Configured" pill and the
    // "Configure" expander: the detail column auto-selects the first toolset
    // and renders its config panel directly, which fetches on mount.
    await renderSkills()

    await screen.findByRole('switch', { name: 'Turn Web Search toolset off' })
    await waitFor(() => expect(getToolsetConfig).toHaveBeenCalled())
    expect(getToolsetConfig.mock.calls[0][0]).toBe('web')
  })

  it('scopes Tools config to the profile chosen in the selector', async () => {
    // Two profiles → the "Configuring:" selector renders. Picking a non-active
    // profile must re-fetch toolsets scoped to THAT profile.
    // jsdom's scrollIntoView is missing/non-functional; Radix Select calls it
    // on open. Force a stub so the dropdown can render in the test env.
    Element.prototype.scrollIntoView = vi.fn()
    getProfiles.mockResolvedValue({
      profiles: [
        { name: 'default', is_default: true },
        { name: 'researcher', is_default: false }
      ]
    })

    const { SkillsView } = await import('./index')
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/skills?tab=toolsets']}>
            <SkillsView />
          </MemoryRouter>
        </QueryClientProvider>
      )
    })

    // The selector appears with >1 profile.
    const trigger = await screen.findByRole('combobox')
    await act(async () => {
      fireEvent.click(trigger)
    })
    const option = await screen.findByRole('option', { name: 'researcher' })
    await act(async () => {
      fireEvent.click(option)
    })

    // Toolsets refetch scoped to the picked profile.
    await waitFor(() => expect(getToolsets).toHaveBeenCalledWith('researcher'))
  })

  it('scopes the Skills tab (and skill toggles) to the profile chosen in the selector', async () => {
    // The selector is Capabilities-WIDE: picking a profile on the Skills tab
    // must refetch the skill list scoped to it, and route toggles there too.
    Element.prototype.scrollIntoView = vi.fn()
    getProfiles.mockResolvedValue({
      profiles: [
        { name: 'default', is_default: true },
        { name: 'researcher', is_default: false }
      ]
    })
    getSkills.mockResolvedValue([
      {
        name: 'web-research',
        description: 'Research the web',
        category: 'research',
        enabled: true,
        usage: 3,
        provenance: 'bundled'
      }
    ])

    const { SkillsView } = await import('./index')
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/skills?tab=skills']}>
            <SkillsView />
          </MemoryRouter>
        </QueryClientProvider>
      )
    })

    // The selector renders on the Skills tab too (Capabilities-wide).
    const trigger = await screen.findByRole('combobox')
    await act(async () => {
      fireEvent.click(trigger)
    })
    const option = await screen.findByRole('option', { name: 'researcher' })
    await act(async () => {
      fireEvent.click(option)
    })

    // Skills refetch scoped to the picked profile...
    await waitFor(() => expect(getSkills).toHaveBeenCalledWith('researcher'))

    // ...and a toggle routes its write to that profile as well.
    const sw = await screen.findByRole('switch', { name: 'web-research' })
    await act(async () => {
      fireEvent.click(sw)
    })
    await waitFor(() => expect(setSkillEnabled).toHaveBeenCalledWith('web-research', false, 'researcher'))
  })

  it('shows the FULL skill in the detail pane — frontmatter metadata + body', async () => {
    getSkills.mockResolvedValue([
      {
        name: 'web-research',
        description: 'Research the web',
        category: 'research',
        enabled: true,
        usage: 3,
        provenance: 'bundled'
      }
    ])

    const { SkillsView } = await import('./index')
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/skills?tab=skills']}>
            <SkillsView />
          </MemoryRouter>
        </QueryClientProvider>
      )
    })

    // Frontmatter renders as metadata rows, the body as full text — not just
    // the one-line description.
    await waitFor(() => expect(getSkillContent).toHaveBeenCalled())
    expect(getSkillContent.mock.calls[0][0]).toBe('web-research')
    expect(await screen.findByText('version')).toBeTruthy()
    expect(await screen.findByText('1.2.0')).toBeTruthy()
    expect(await screen.findByText(/Deep research steps/)).toBeTruthy()
  })

  it('hub picker refuses to reinstall an already-installed skill', async () => {
    const { notify } = await import('@/store/notifications')
    const { EmbeddedHubPicker } = await import('./embedded-hub-picker')

    render(<EmbeddedHubPicker installedNames={new Set(['web-research'])} profile={null} />)

    // The picker is expanded by default — the hub iframe is live on mount.
    expect(document.querySelector('iframe')).toBeTruthy()

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'hermes-skill-pick', name: 'web-research', identifier: 'web-research' },
          origin: 'https://hermes-agent.nousresearch.com'
        })
      )
    })

    // Refused with an informational toast, no install action spawned.
    await waitFor(() =>
      expect(vi.mocked(notify)).toHaveBeenCalledWith(
        expect.objectContaining({ title: '"web-research" is already installed' })
      )
    )
  })

  it('mounts the hub iframe lazily and keeps it (hidden) across tab switches', async () => {
    // On a non-Skills tab the docs-site iframe must not exist at all — an
    // eagerly mounted hub is exactly the Capabilities lag bug.
    await renderSkills() // ?tab=toolsets
    await screen.findByRole('switch', { name: 'Turn Web Search toolset off' })
    expect(document.querySelector('iframe')).toBeNull()
    cleanup()

    // Embedded mode drives tabs through local state (the route hooks are
    // mocked here), starting on Skills: the picker mounts with the tab.
    const { SkillsView } = await import('./index')
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/skills']}>
            <SkillsView embedded />
          </MemoryRouter>
        </QueryClientProvider>
      )
    })

    const iframe = document.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(iframe!.closest('section')!.classList.contains('hidden')).toBe(false)

    // Switch to Tools → the iframe STAYS mounted (no docs-site reload on the
    // next visit) but its section is fully hidden, so nothing from the hub
    // can paint over the toolsets UI.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Tools/ }))
    })
    const kept = document.querySelector('iframe')
    expect(kept).toBeTruthy()
    expect(kept!.closest('section')!.classList.contains('hidden')).toBe(true)
  })

  it('shows a vision explainer that deep-links to Settings → Models', async () => {
    // Vision has no TOOL_CATEGORIES provider matrix — its model lives in the
    // auxiliary model config, so the detail pane must point there instead of
    // rendering an empty panel.
    getToolsets.mockResolvedValue([
      toolset({
        name: 'vision',
        label: 'Vision / Image Analysis',
        description: 'vision_analyze',
        tools: ['vision_analyze']
      })
    ])
    getToolsetConfig.mockResolvedValue({ has_category: false, active_provider: null, providers: [] })

    await renderSkills()

    expect(await screen.findByText(/auxiliary model configuration/)).toBeTruthy()
    const link = screen.getByRole('button', { name: /Choose vision model in Settings/ })

    await act(async () => {
      fireEvent.click(link)
    })

    // Internal route change into the Models section with the aux slot target —
    // consumed by ModelSettings' deep-link highlight. Never an external URL.
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/settings?tab=config:model&aux=vision'))
  })

  it('fixedConnection pins every read to the target connection', async () => {
    // Bot Mode's remote-target door: a bot on another registered gateway gets
    // the live surface pointed at ITS backend — the reads must carry the
    // (connection, profile) pin, not a bare profile name that would resolve
    // against the ACTIVE gateway (the wrong-machine bug).
    const { SkillsView } = await import('./index')
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/skills']}>
            <SkillsView embedded fixedConnection="homelab" fixedProfile="inbox-bot" />
          </MemoryRouter>
        </QueryClientProvider>
      )
    })

    await waitFor(() => expect(getSkills).toHaveBeenCalled())
    expect(getSkills.mock.calls[0][0]).toEqual({ connectionId: 'homelab', profile: 'inbox-bot' })
    expect(getToolsets.mock.calls[0][0]).toEqual({ connectionId: 'homelab', profile: 'inbox-bot' })
    // Pinned scope → no roster/profiles fetch, selector hidden.
    expect(getProfiles).not.toHaveBeenCalled()
  })

  it('fixedProfile without fixedConnection rides the active connection and follows a backend flip', async () => {
    // Bot Mode's profile-only pin: the pinned profile is served by WHICHEVER
    // connection is active, resolved as an exact (connectionId, profile)
    // snapshot — a same-profile app-wide backend flip must re-point the
    // surface even though the profile name never changes. (A bare profile
    // name here would resolve the ambient connection outside React's
    // knowledge, and the Computer Use grant lifecycle prop would stay on the
    // previous backend.)
    const { setPrimaryGatewayConnectionId } = await import('@/store/gateway')
    const { resetToolCallsCache } = await import('./index')

    resetToolCallsCache()
    setPrimaryGatewayConnectionId('homelab')

    const botSkill = (connectionId: string) => [
      {
        name: `${connectionId}-bot-skill`,
        description: 'bot skill',
        category: 'bot',
        enabled: true,
        usage: 2,
        provenance: 'bundled' as const
      }
    ]

    getSkills.mockImplementation((scope?: unknown) =>
      Promise.resolve(
        botSkill(
          !!scope &&
            typeof scope === 'object' &&
            (scope as { connectionId?: string }).connectionId === 'local'
            ? 'local'
            : 'remote'
        )
      )
    )

    try {
      const { SkillsView } = await import('./index')

      await act(async () => {
        render(
          <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={['/skills']}>
              <SkillsView fixedProfile="inbox-bot" />
            </MemoryRouter>
          </QueryClientProvider>
        )
      })

      await waitFor(() => expect(getSkills).toHaveBeenCalledWith({ connectionId: 'homelab', profile: 'inbox-bot' }))
      expect(await screen.findAllByText('remote-bot-skill')).not.toHaveLength(0)

      // App-wide switch to the local default — the pinned profile follows.
      await act(async () => {
        setPrimaryGatewayConnectionId(null)
      })

      await waitFor(() => expect(getSkills).toHaveBeenCalledWith({ connectionId: 'local', profile: 'inbox-bot' }))
      expect(await screen.findAllByText('local-bot-skill')).not.toHaveLength(0)
      expect(screen.queryAllByText('remote-bot-skill')).toHaveLength(0)
    } finally {
      setPrimaryGatewayConnectionId(null)
      resetToolCallsCache()
    }
  })

  it('offers (connection, profile) scope rows on multi-connection desktops', async () => {
    // With a v2 registry holding >1 connection, the scope selector lists the
    // union agent roster — profile + owning device — instead of the local
    // profiles list, so a selection identifies WHICH gateway's capabilities
    // are being configured.
    const connections = {
      list: vi.fn().mockResolvedValue({
        version: 2,
        primary: 'local',
        secureTokenStorage: true,
        connections: [
          { id: 'local', kind: 'local', label: 'This device', tokenSet: false, tokenPreview: null },
          { id: 'homelab', kind: 'remote', label: 'Homelab', tokenSet: true, tokenPreview: '…' }
        ]
      })
    }

    const getAgentRoster = vi.fn().mockResolvedValue({
      agents: [
        {
          connectionId: 'local',
          connectionKind: 'local',
          connectionLabel: 'This device',
          profile: 'default',
          handle: 'default'
        },
        {
          connectionId: 'homelab',
          connectionKind: 'remote',
          connectionLabel: 'Homelab',
          profile: 'inbox-bot',
          handle: 'inbox-bot-homelab'
        }
      ],
      sources: []
    })

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { connections, getAgentRoster }

    try {
      await renderSkills()

      await waitFor(() => expect(getAgentRoster).toHaveBeenCalled())
      // The selector paints roster rows labeled profile — device.
      expect(await screen.findByText('default — This device (current)')).toBeTruthy()
    } finally {
      delete (window as { hermesDesktop?: unknown }).hermesDesktop
    }
  })

  it('does not keep remote default caches after switching to the local default', async () => {
    Element.prototype.scrollIntoView = vi.fn()

    const { setPrimaryGatewayConnectionId } = await import('@/store/gateway')
    const { resetToolCallsCache } = await import('./index')

    resetToolCallsCache()
    setPrimaryGatewayConnectionId('homelab')

    const localPin = { connectionId: 'local', profile: 'default' }
    const localSkill = {
      name: 'local-only-skill',
      description: 'from local',
      category: 'local',
      enabled: true,
      usage: 1,
      provenance: 'bundled' as const
    }
    const remoteSkill = {
      name: 'remote-only-skill',
      description: 'from remote',
      category: 'remote',
      enabled: true,
      usage: 9,
      provenance: 'bundled' as const
    }
    const isLocalPin = (scope: unknown) =>
      !!scope && typeof scope === 'object' && (scope as { connectionId?: string }).connectionId === 'local'

    getSkills.mockImplementation((scope?: unknown) => Promise.resolve(isLocalPin(scope) ? [localSkill] : [remoteSkill]))
    getToolsets.mockImplementation((scope?: unknown) =>
      Promise.resolve([
        isLocalPin(scope)
          ? toolset({ name: 'web', label: 'Local Search', tools: ['local_search'] })
          : toolset({ name: 'web', label: 'Remote Search', tools: ['remote_search'] })
      ])
    )
    getUsageAnalytics.mockImplementation((_days: number, scope?: unknown) =>
      Promise.resolve({
        tools: isLocalPin(scope) ? [{ tool: 'local_search', count: 7 }] : [{ tool: 'remote_search', count: 99 }]
      })
    )

    const connections = {
      list: vi.fn().mockResolvedValue({
        version: 2,
        primary: 'homelab',
        secureTokenStorage: true,
        connections: [
          { id: 'local', kind: 'local', label: 'This device', tokenSet: false, tokenPreview: null },
          { id: 'homelab', kind: 'remote', label: 'Homelab', tokenSet: true, tokenPreview: '…' }
        ]
      })
    }
    const getAgentRoster = vi.fn().mockResolvedValue({
      agents: [
        {
          connectionId: 'local',
          connectionKind: 'local',
          connectionLabel: 'This device',
          profile: 'default',
          handle: 'default'
        },
        {
          connectionId: 'homelab',
          connectionKind: 'remote',
          connectionLabel: 'Homelab',
          profile: 'default',
          handle: 'default-homelab'
        }
      ],
      sources: []
    })

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = { connections, getAgentRoster }

    try {
      const { SkillsView } = await import('./index')

      await act(async () => {
        render(
          <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={['/skills']}>
              <SkillsView embedded />
            </MemoryRouter>
          </QueryClientProvider>
        )
      })

      // The ambient scope resolves the EXACT active route — the remote default
      // is an explicit (connectionId, profile) snapshot, not a bare profile
      // name whose cache key re-reads the mutable ambient connection tag.
      const remoteScope = { connectionId: 'homelab', profile: 'default' }

      await waitFor(() => expect(getSkills).toHaveBeenCalledWith(remoteScope))
      expect(await screen.findAllByText('remote-only-skill')).not.toHaveLength(0)
      expect(screen.queryAllByText('local-only-skill')).toHaveLength(0)

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /Tools/ }))
      })
      expect(await screen.findAllByText('Remote Search')).not.toHaveLength(0)

      const trigger = await screen.findByRole('combobox')

      await act(async () => {
        fireEvent.click(trigger)
      })
      const option = await screen.findByRole('option', { name: 'default — This device' })

      await act(async () => {
        fireEvent.click(option)
      })

      await waitFor(() => expect(getToolsets).toHaveBeenCalledWith(localPin))
      expect(getSkills).toHaveBeenCalledWith(localPin)
      await waitFor(() => expect(getUsageAnalytics).toHaveBeenCalledWith(365, localPin))
      expect(await screen.findAllByText('Local Search')).not.toHaveLength(0)
      expect(screen.queryAllByText('Remote Search')).toHaveLength(0)

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /Skills/ }))
      })
      expect(await screen.findAllByText('local-only-skill')).not.toHaveLength(0)
      expect(screen.queryAllByText('remote-only-skill')).toHaveLength(0)

      const sw = await screen.findByRole('switch', { name: 'local-only-skill' })

      await act(async () => {
        fireEvent.click(sw)
      })
      await waitFor(() => expect(setSkillEnabled).toHaveBeenCalledWith('local-only-skill', false, localPin))
    } finally {
      setPrimaryGatewayConnectionId(null)
      delete (window as { hermesDesktop?: unknown }).hermesDesktop
      resetToolCallsCache()
    }
  })

  it('re-scopes when the app-wide active backend switches from remote default to local default', async () => {
    // The selector is NOT involved: the app-wide route flip (Settings →
    // Gateway apply / statusbar source switch) republishes the active-route
    // snapshot while $activeGatewayProfile stays "default" on both sides —
    // only the exact (connectionId, profile) subscription can re-point the
    // ambient scope, or Skills/Toolsets keep the remote machine's lists.
    const { setPrimaryGatewayConnectionId } = await import('@/store/gateway')
    const { resetToolCallsCache } = await import('./index')

    resetToolCallsCache()
    // Remote default active app-wide (the primary's published registry
    // identity — the same seam gateway-boot's connection-apply publish uses).
    setPrimaryGatewayConnectionId('homelab')

    const remoteScope = { connectionId: 'homelab', profile: 'default' }
    const localScope = { connectionId: 'local', profile: 'default' }
    const remoteSkill = {
      name: 'remote-only-skill',
      description: 'from remote',
      category: 'remote',
      enabled: true,
      usage: 9,
      provenance: 'bundled' as const
    }
    const localSkill = {
      name: 'local-only-skill',
      description: 'from local',
      category: 'local',
      enabled: true,
      usage: 1,
      provenance: 'bundled' as const
    }
    const isLocalScope = (scope: unknown) =>
      !!scope && typeof scope === 'object' && (scope as { connectionId?: string }).connectionId === 'local'

    getSkills.mockImplementation((scope?: unknown) => Promise.resolve(isLocalScope(scope) ? [localSkill] : [remoteSkill]))
    getToolsets.mockImplementation((scope?: unknown) =>
      Promise.resolve([
        isLocalScope(scope)
          ? toolset({ name: 'web', label: 'Local Search', tools: ['local_search'] })
          : toolset({ name: 'web', label: 'Remote Search', tools: ['remote_search'] })
      ])
    )
    getUsageAnalytics.mockImplementation((_days: number, scope?: unknown) =>
      Promise.resolve({
        tools: isLocalScope(scope) ? [{ tool: 'local_search', count: 7 }] : [{ tool: 'remote_search', count: 99 }]
      })
    )

    try {
      const { SkillsView } = await import('./index')

      await act(async () => {
        render(
          <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={['/skills?tab=toolsets']}>
              <SkillsView />
            </MemoryRouter>
          </QueryClientProvider>
        )
      })

      await waitFor(() => expect(getToolsets).toHaveBeenCalledWith(remoteScope))
      expect(await screen.findAllByText('Remote Search')).not.toHaveLength(0)
      expect(screen.queryAllByText('Local Search')).toHaveLength(0)

      // The app-wide switch to the local default: Electron republishes the
      // primary descriptor (connectionId null = the local pool). No selector,
      // no profile change.
      await act(async () => {
        setPrimaryGatewayConnectionId(null)
      })

      await waitFor(() => expect(getToolsets).toHaveBeenCalledWith(localScope))
      expect(await screen.findAllByText('Local Search')).not.toHaveLength(0)
      expect(screen.queryAllByText('Remote Search')).toHaveLength(0)

      // The usage badges reloaded for the new scope too (the analytics mirror
      // resets with the scope key, so the lazy loader re-runs).
      await waitFor(() => expect(getUsageAnalytics).toHaveBeenCalledWith(365, localScope))
      expect((await screen.findAllByText('×7')).length).toBeGreaterThan(0)

      // And writes route to the newly active backend.
      const sw = await screen.findByRole('switch', { name: 'Turn Local Search toolset off' })

      await act(async () => {
        fireEvent.click(sw)
      })
      await waitFor(() => expect(setToolsetEnabled).toHaveBeenCalledWith('web', false, localScope))
    } finally {
      setPrimaryGatewayConnectionId(null)
      resetToolCallsCache()
    }
  })
})

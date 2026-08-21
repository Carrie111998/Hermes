import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Input } from '@/components/ui/input'
import type {
  DesktopConnectionKind,
  DesktopConnectionProbeResult,
  DesktopConnectionsRegistry,
  DesktopRegistryConnection,
  DesktopRegistryConnectionInput
} from '@/global'
import { useI18n } from '@/i18n'
import {
  CONNECTION_SEARCH_THRESHOLD,
  connectionMatchesQuery,
  sortConnectionsForDisplay
} from '@/lib/connection-display'
import { deriveRemoteAuthProviderShape } from '@/lib/desktop-remote-auth'
import { triggerHaptic } from '@/lib/haptics'
import { Cloud, Globe, Loader2, Monitor, Pencil, Plus, RefreshCw, SearchIcon, Terminal, Trash2 } from '@/lib/icons'
import { $activeConnectionId, setConnectionsRegistry } from '@/store/connections'
import { notify, notifyError } from '@/store/notifications'

import { EmptyState, ListRow, Pill, SectionHeading, ToggleRow } from './primitives'

const KIND_ICONS: Record<DesktopConnectionKind, typeof Globe> = {
  cloud: Cloud,
  local: Monitor,
  remote: Globe,
  ssh: Terminal
}

interface EditorState {
  // null id → creating a new connection.
  id: null | string
  kind: DesktopConnectionKind
  label: string
  url: string
  authMode: 'oauth' | 'token'
  token: string
  host: string
  keyPath: string
  // ssh remote profile, hydrated on edit so the duplicate key matches the
  // main-process one (user@host:port + profile); the editor doesn't expose it.
  remoteProfile: string
  // Extra gateway headers (access proxies such as Cloudflare Access).
  // `value: ''` on a row that came from storage means "keep the saved
  // secret" (sent as null); a typed value replaces it; a removed row clears
  // it. `stored` marks rows hydrated from headerNames so the placeholder can
  // say "saved" instead of demanding a value.
  headers: { name: string; stored: boolean; value: string }[]
}

function editorFromConnection(conn: DesktopRegistryConnection): EditorState {
  return {
    id: conn.id,
    kind: conn.kind,
    label: conn.label,
    url: conn.url || '',
    authMode: conn.authMode || 'token',
    token: '',
    // Reconstruct the composite the single ssh host field displays. The save
    // payload sends ONLY this string (never separate user/port), because
    // normalizeSshConfig gives explicit user/port fields precedence over the
    // parsed host string — sending stored user/port alongside a retyped host
    // would silently resurrect the old values.
    host: conn.host ? `${conn.user ? `${conn.user}@` : ''}${conn.host}${conn.port ? `:${conn.port}` : ''}` : '',
    keyPath: conn.keyPath || '',
    remoteProfile: conn.remoteProfile || '',
    headers: (conn.headerNames || []).map(name => ({ name, stored: true, value: '' }))
  }
}

function emptyEditor(kind: DesktopConnectionKind): EditorState {
  return {
    id: null,
    kind,
    label: '',
    url: '',
    authMode: 'token',
    token: '',
    host: '',
    keyPath: '',
    remoteProfile: '',
    headers: []
  }
}

/** Dedupe key for a remote/cloud gateway URL: trim, drop trailing slashes, lowercase. */
export function normalizeGatewayUrl(url: string): string {
  return url.trim().replace(/\/+$/, '').toLowerCase()
}

/**
 * Dedupe key for the composite ssh host string the editor shows
 * (`user@host:port`, user/port optional): normalized to `user@host:port` with
 * the default port made explicit so `box` and `box:22` collide.
 */
export function sshCompositeKey(composite: string): string {
  const raw = composite.trim().toLowerCase()

  if (!raw) {
    return ''
  }

  const at = raw.lastIndexOf('@')
  const user = at > 0 ? raw.slice(0, at) : ''
  const rest = at >= 0 ? raw.slice(at + 1) : raw
  const portMatch = rest.match(/^(.*?):(\d+)$/)
  const host = portMatch ? portMatch[1] : rest
  const port = portMatch ? portMatch[2] : '22'

  if (!host) {
    return ''
  }

  return `${user}@${host}:${port}`
}

export function isConnectionAuthRejection(error: unknown): boolean {
  const kind =
    error && typeof error === 'object' && 'kind' in error ? (error as { kind?: unknown }).kind : null

  const statusCode =
    error && typeof error === 'object' && 'statusCode' in error
      ? Number((error as { statusCode?: unknown }).statusCode)
      : null

  return kind === 'auth-required' || statusCode === 401 || statusCode === 403
}

/**
 * The renderer-side duplicate rule, mirrored by the main process
 * (normalizeConnectionInput enforces the same keys in the save path):
 *  - at most ONE local entry, ever;
 *  - remote/cloud entries are duplicates when their normalized URLs match;
 *  - ssh entries are duplicates on user@host:port + remote profile.
 * Returns the existing entry the candidate collides with, or null.
 */
export function findDuplicateConnection(
  editor: Pick<EditorState, 'host' | 'id' | 'kind' | 'remoteProfile' | 'url'>,
  connections: DesktopRegistryConnection[]
): DesktopRegistryConnection | null {
  if (editor.kind === 'local') {
    return connections.find(c => c.kind === 'local' && c.id !== editor.id) ?? null
  }

  if (editor.kind === 'remote' || editor.kind === 'cloud') {
    const key = normalizeGatewayUrl(editor.url)

    if (!key) {
      return null
    }

    return (
      connections.find(
        c =>
          (c.kind === 'remote' || c.kind === 'cloud') && c.id !== editor.id && normalizeGatewayUrl(c.url || '') === key
      ) ?? null
    )
  }

  const key = sshCompositeKey(editor.host)

  if (!key) {
    return null
  }

  const profile = editor.remoteProfile.trim()

  return (
    connections.find(
      c =>
        c.kind === 'ssh' &&
        c.id !== editor.id &&
        sshCompositeKey(`${c.user ? `${c.user}@` : ''}${c.host ?? ''}${c.port ? `:${c.port}` : ''}`) === key &&
        (c.remoteProfile || '').trim() === profile
    ) ?? null
  )
}

/**
 * Display-only same-backend hint: when two registered connections report the
 * same /api/status install_id, they are one physical backend registered under
 * two addresses (hostname + Tailscale IP). Returns the label of the FIRST
 * earlier sibling sharing this connection's id, or null. Never blocks or
 * deletes — dual routes are legitimate failover config; the roster already
 * collapses the duplicate bot rows.
 */
export function sameBackendPeerLabel(
  conn: Pick<DesktopRegistryConnection, 'id' | 'installId'>,
  connections: Pick<DesktopRegistryConnection, 'id' | 'installId' | 'label'>[]
): null | string {
  if (!conn.installId) {
    return null
  }

  for (const other of connections) {
    if (other.id === conn.id) {
      // Only rows AFTER the first occurrence carry the hint, so exactly one
      // of a same-backend pair is annotated (the later-listed one).
      return null
    }

    if (other.installId === conn.installId) {
      return other.label
    }
  }

  return null
}

function scrollableAncestor(element: HTMLElement): HTMLElement | null {
  let parent = element.parentElement

  while (parent) {
    if (/(auto|scroll)/.test(window.getComputedStyle(parent).overflowY)) {
      return parent
    }

    parent = parent.parentElement
  }

  return null
}

/**
 * The connections registry section of Settings → Gateways: manage the named
 * agent sources (local runtime + any number of remote gateways / Hermes Cloud
 * instances / SSH hosts). Storage-level management — the active/primary
 * switchover UX is the connection-mode controls above this section.
 */
export function ConnectionsRegistrySection() {
  const { t } = useI18n()
  const s = t.settings.connections
  const activeConnectionId = useStore($activeConnectionId)
  const [registry, setRegistry] = useState<DesktopConnectionsRegistry | null>(null)
  const [loading, setLoading] = useState(true)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [saving, setSaving] = useState(false)
  const [busyId, setBusyId] = useState<null | string>(null)
  const [testingId, setTestingId] = useState<null | string>(null)
  const [removeTarget, setRemoveTarget] = useState<DesktopRegistryConnection | null>(null)
  const [plainTextConfirm, setPlainTextConfirm] = useState(false)
  const [launchModeBusy, setLaunchModeBusy] = useState(false)
  const [updatingAll, setUpdatingAll] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [authProbe, setAuthProbe] = useState<DesktopConnectionProbeResult | null>(null)
  const [authProbeBusy, setAuthProbeBusy] = useState(false)
  const [authSignedIn, setAuthSignedIn] = useState(false)
  const [authSigningIn, setAuthSigningIn] = useState(false)
  const [authVerifying, setAuthVerifying] = useState(false)
  const [authError, setAuthError] = useState<null | string>(null)
  const [authProbeGeneration, setAuthProbeGeneration] = useState<null | number>(null)
  const [authReadyGeneration, setAuthReadyGeneration] = useState<null | number>(null)
  const [authScope, setAuthScope] = useState<null | string>(null)

  const authScopeRef = useRef<{
    editorGeneration: number
    normalizedUrl: string
    owned: boolean
    scope: null | string
  }>({ editorGeneration: 0, normalizedUrl: '', owned: false, scope: null })

  const authDraftPromises = useRef(
    new Set<{
      cleanupUrl: string
      editorGeneration: number
      normalizedUrl: string
      promise: Promise<null | string>
    }>()
  )

  const authTargetConnectionId = useRef<null | string>(null)
  const authGeneration = useRef(0)
  const editorRef = useRef<EditorState | null>(null)
  editorRef.current = editor
  const authReadinessCapability = useRef<null | string>(null)
  const [reauthIds, setReauthIds] = useState<Set<string>>(() => new Set())
  const authProbeSeq = useRef(0)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const pendingSearchTopRef = useRef<null | number>(null)
  // Inline duplicate rejection from the save path (dedupe is also enforced in
  // the main process, so a crafted payload can't slip past the UI check).
  const [dupeError, setDupeError] = useState<null | string>(null)

  const bridge = window.hermesDesktop?.connections

  const { isPassword: isPasswordProvider, providerLabel } = deriveRemoteAuthProviderShape(
    authProbe?.providers,
    t.install.identityProvider
  )

  const hasLocal = Boolean(registry?.connections.some(c => c.kind === 'local'))

  const editingExistingTokenEntry = Boolean(
    editor?.id &&
      registry?.connections.some(
        connection =>
          connection.id === editor.id && connection.kind === 'remote' && (connection.authMode ?? 'token') === 'token'
      )
  )

  const originalEditorConnection = editor?.id
    ? registry?.connections.find(connection => connection.id === editor.id)
    : undefined

  const remoteEndpointChanged = Boolean(
    editor?.kind === 'remote' &&
      editor.id &&
      normalizeGatewayUrl(editor.url) !== normalizeGatewayUrl(originalEditorConnection?.url || '')
  )

  const headersChanged = Boolean(
    editor?.kind === 'remote' &&
      (editor.headers.some(row => row.value.trim()) ||
        editor.headers.map(row => row.name.trim()).filter(Boolean).join('\n') !==
          (originalEditorConnection?.headerNames || []).join('\n'))
  )

  const remoteAuthChanged = Boolean(
    editor?.kind === 'remote' &&
      (editor.token.trim() ||
        editor.authMode !== (originalEditorConnection?.authMode || 'token') ||
        headersChanged)
  )

  const remoteNeedsReadiness = Boolean(
    editor?.kind === 'remote' && (!editor.id || remoteEndpointChanged || remoteAuthChanged)
  )

  const authProbeIsCurrent = Boolean(
    editor?.kind === 'remote' &&
      authProbe?.reachable &&
      authProbeGeneration === authGeneration.current &&
      normalizeGatewayUrl(authProbe.baseUrl) === normalizeGatewayUrl(editor.url)
  )

  const authReady = authProbeIsCurrent && authReadyGeneration === authGeneration.current

  const publishRegistry = useCallback((next: DesktopConnectionsRegistry) => {
    setRegistry(next)
    setConnectionsRegistry(next)
  }, [])

  const load = useCallback(async () => {
    if (!bridge) {
      setLoading(false)

      return
    }

    setLoading(true)

    try {
      publishRegistry(await bridge.list())
    } catch (err) {
      notifyError(err, s.loadFailed)
    } finally {
      setLoading(false)
    }
  }, [bridge, publishRegistry, s.loadFailed])

  useEffect(() => {
    void load()
  }, [load])

  const editorAuthHeaders = (current: EditorState): Record<string, null | string> =>
    Object.fromEntries(
      current.headers
        .map(row => [row.name.trim(), row.value.trim() || (row.stored ? null : '')] as const)
        .filter(([name]) => Boolean(name))
    )

  const setAuthScopeState = (
    scope: null | string,
    owned: boolean,
    normalizedUrl = normalizeGatewayUrl(editorRef.current?.url || ''),
    editorGeneration = authGeneration.current
  ) => {
    authScopeRef.current = { editorGeneration, normalizedUrl, owned, scope }
    setAuthScope(scope)
  }

  const invalidateReadiness = (clearProbe = false) => {
    authGeneration.current += 1
    authReadinessCapability.current = null
    setAuthReadyGeneration(null)
    setAuthError(null)
    authScopeRef.current = { ...authScopeRef.current, editorGeneration: authGeneration.current }

    if (clearProbe) {
      authProbeSeq.current += 1
      setAuthProbe(null)
      setAuthProbeGeneration(null)
      setAuthProbeBusy(false)
    }
  }

  const resetEditorAuth = useCallback((signedIn = false) => {
    authProbeSeq.current += 1
    authGeneration.current += 1
    authReadinessCapability.current = null
    setAuthProbe(null)
    setAuthProbeGeneration(null)
    setAuthProbeBusy(false)
    setAuthSignedIn(signedIn)
    setAuthSigningIn(false)
    setAuthVerifying(false)
    setAuthError(null)
    setAuthReadyGeneration(null)
  }, [])

  const clearDraft = async (
    draft: Pick<typeof authScopeRef.current, 'normalizedUrl' | 'owned' | 'scope'>,
    fallbackUrl = ''
  ) => {
    if (!draft.owned || !draft.scope) {
      return
    }

    if (authScopeRef.current.scope === draft.scope) {
      setAuthScopeState(null, false)
    }

    const url = draft.normalizedUrl || normalizeGatewayUrl(fallbackUrl)

    if (url) {
      await bridge?.auth.clear({ scope: draft.scope, url, headers: {} })
    }
  }

  const clearOwnedDraft = async (fallbackUrl: string) => clearDraft(authScopeRef.current, fallbackUrl)

  const createOwnedDraft = async (
    ownerConnectionId?: string,
    normalizedUrl = normalizeGatewayUrl(editorRef.current?.url || ''),
    editorGeneration = authGeneration.current
  ) => {
    const current = authScopeRef.current

    if (
      current.owned &&
      current.scope &&
      current.editorGeneration === editorGeneration &&
      current.normalizedUrl === normalizedUrl
    ) {
      return current.scope
    }

    const existingPending = [...authDraftPromises.current].find(
      pending => pending.editorGeneration === editorGeneration && pending.normalizedUrl === normalizedUrl
    )

    if (existingPending) {
      return existingPending.promise
    }

    const pending = {
      cleanupUrl: '',
      editorGeneration,
      normalizedUrl,
      promise: Promise.resolve<null | string>(null)
    }

    pending.promise = (async () => {
      const result = await bridge?.auth.createDraft(ownerConnectionId ? { ownerConnectionId } : undefined)

      if (!result?.ok) {
        if (result && editorGeneration === authGeneration.current) {
          setAuthError(result.error)
        }

        return null
      }

      const currentEditor = editorRef.current

      const stillCurrent =
        editorGeneration === authGeneration.current &&
        currentEditor?.kind === 'remote' &&
        normalizeGatewayUrl(currentEditor.url) === normalizedUrl

      if (!stillCurrent) {
        await clearDraft(
          { normalizedUrl: pending.cleanupUrl || normalizedUrl, owned: true, scope: result.scope },
          currentEditor?.url || ''
        )

        return null
      }

      setAuthScopeState(result.scope, true, normalizedUrl, editorGeneration)
      authTargetConnectionId.current = result.targetConnectionId

      return result.scope
    })()

    authDraftPromises.current.add(pending)

    try {
      return await pending.promise
    } finally {
      authDraftPromises.current.delete(pending)
    }
  }

  const readDurableAuthStatus = async (next: EditorState, seq: number) => {
    if (!next.id || next.kind !== 'remote') {
      return
    }

    const result = await bridge?.auth.status({
      scope: next.id,
      url: next.url,
      headers: editorAuthHeaders(next)
    })

    if (seq !== authGeneration.current || !result) {
      return
    }

    if (result.ok) {
      setAuthSignedIn(result.connected)
    } else {
      setAuthSignedIn(false)
      setAuthError(result.error)
    }
  }

  const openEditor = (next: EditorState | null) => {
    const previous = authScopeRef.current
    const previousUrl = editorRef.current?.url || ''
    setDupeError(null)
    resetEditorAuth()
    editorRef.current = next
    setAuthScopeState(null, false, normalizeGatewayUrl(next?.url || ''))
    authTargetConnectionId.current = null
    setEditor(next)
    void clearDraft(previous, previousUrl)

    for (const pending of authDraftPromises.current) {
      pending.cleanupUrl ||= normalizeGatewayUrl(previousUrl || next?.url || '')
    }

    if (!next || next.kind !== 'remote') {
      return
    }

    if (next.id) {
      setAuthScopeState(next.id, false, normalizeGatewayUrl(next.url))
      authTargetConnectionId.current = next.id
      const seq = authGeneration.current
      void readDurableAuthStatus(next, seq)
    } else {
      void createOwnedDraft(undefined, normalizeGatewayUrl(next.url), authGeneration.current)
    }
  }

  const ensureScopeForUrl = async (current: EditorState, nextUrl: string) => {
    const previousNormalizedUrl = normalizeGatewayUrl(current.url)
    const normalizedUrl = normalizeGatewayUrl(nextUrl)

    if (normalizedUrl === previousNormalizedUrl) {
      return authScopeRef.current.scope
    }

    const previous = authScopeRef.current

    for (const pending of authDraftPromises.current) {
      pending.cleanupUrl ||= previousNormalizedUrl || normalizedUrl
    }

    void clearDraft(previous, previousNormalizedUrl || normalizedUrl)
    setAuthScopeState(null, false, normalizedUrl)

    if (!current.id) {
      return createOwnedDraft(undefined, normalizedUrl, authGeneration.current)
    }

    const original = registry?.connections.find(connection => connection.id === current.id)?.url || ''
    const changed = normalizedUrl !== normalizeGatewayUrl(original)

    if (changed) {
      return createOwnedDraft(current.id, normalizedUrl, authGeneration.current)
    }

    setAuthScopeState(current.id, false, normalizedUrl)
    authTargetConnectionId.current = current.id
    const seq = authGeneration.current
    void readDurableAuthStatus({ ...current, url: nextUrl }, seq)

    return current.id
  }

  const probeEditorAuth = async () => {
    const url = editor?.url.trim()

    if (!editor || editor.kind !== 'remote' || !url || !bridge?.auth) {
      return
    }

    const scope = authScopeRef.current.scope || (await createOwnedDraft())

    if (!scope) {
      return
    }

    const seq = ++authProbeSeq.current
    const generation = authGeneration.current
    setAuthProbeBusy(true)
    setAuthError(null)

    try {
      const result = await bridge.auth.probe({ scope, url, headers: editorAuthHeaders(editor) })

      if (seq !== authProbeSeq.current || generation !== authGeneration.current) {
        return
      }

      if (!('reachable' in result)) {
        setAuthProbe(null)
        setAuthError(result.error)

        return
      }

      setAuthProbe(result)
      setAuthProbeGeneration(generation)

      if (!result.reachable) {
        setAuthError(result.error || t.settings.gateway.probeError)

        return
      }

      if (normalizeGatewayUrl(result.baseUrl) !== normalizeGatewayUrl(url)) {
        setAuthError('The gateway response did not match the current URL. Detect authentication again.')

        return
      }

      setEditor(current =>
        current && normalizeGatewayUrl(current.url) === normalizeGatewayUrl(url)
          ? { ...current, authMode: result.authMode === 'oauth' ? 'oauth' : 'token' }
          : current
      )
    } catch (err) {
      if (seq === authProbeSeq.current) {
        const message = err instanceof Error ? err.message : t.settings.gateway.probeError
        setAuthError(message)
        notifyError(err, t.settings.gateway.probeError)
      }
    } finally {
      if (seq === authProbeSeq.current) {
        setAuthProbeBusy(false)
      }
    }
  }

  const verifyEditorAuth = async (current = editor) => {
    const url = current?.url.trim()
    const scope = authScopeRef.current.scope

    if (!current || current.kind !== 'remote' || !url || !scope || !bridge?.auth) {
      return false
    }

    const generation = authGeneration.current
    setAuthVerifying(true)
    setAuthError(null)

    try {
      const result = await bridge.auth.verify({
        authMode: current.authMode,
        scope,
        token: current.token.trim() || undefined,
        url,
        headers: editorAuthHeaders(current)
      })

      if (generation !== authGeneration.current) {
        return false
      }

      if (!result.ok) {
        authReadinessCapability.current = null
        setAuthReadyGeneration(null)
        setAuthError(result.error)

        if (result.kind === 'auth-required' && current.id) {
          setReauthIds(ids => new Set(ids).add(current.id!))
          setAuthSignedIn(false)
        }

        return false
      }

      authReadinessCapability.current = result.readinessCapability
      setAuthReadyGeneration(generation)

      return true
    } catch (err) {
      if (generation === authGeneration.current) {
        authReadinessCapability.current = null
        setAuthReadyGeneration(null)
        setAuthError(err instanceof Error ? err.message : s.testFailed)
      }

      return false
    } finally {
      if (generation === authGeneration.current) {
        setAuthVerifying(false)
      }
    }
  }

  const signInEditorAuth = async () => {
    const url = editor?.url.trim()
    const scope = authScopeRef.current.scope

    if (!editor || editor.kind !== 'remote' || !url || !scope || !bridge?.auth) {
      return
    }

    const generation = authGeneration.current
    setAuthSigningIn(true)
    setAuthError(null)

    try {
      const result = await bridge.auth.login({ scope, url, headers: editorAuthHeaders(editor) })

      if (generation !== authGeneration.current) {
        return
      }

      if (!result.ok) {
        setAuthError(result.error)

        return
      }

      if (!result.connected) {
        notify({
          kind: 'warning',
          title: t.boot.failure.signInIncompleteTitle,
          message: t.boot.failure.signInIncompleteMessage
        })

        return
      }

      setAuthSignedIn(true)
      const verified = await verifyEditorAuth(editor)

      if (verified && editor.id) {
        setReauthIds(current => {
          const next = new Set(current)
          next.delete(editor.id!)

          return next
        })
      }
    } catch (err) {
      if (generation === authGeneration.current) {
        setAuthError(err instanceof Error ? err.message : s.signInFailed)
        notifyError(err, s.signInFailed)
      }
    } finally {
      if (generation === authGeneration.current) {
        setAuthSigningIn(false)
      }
    }
  }

  const signOutEditorAuth = async () => {
    const url = editor?.url.trim()
    const scope = authScopeRef.current.scope

    if (!editor || !url || !scope || !bridge?.auth) {
      return
    }

    invalidateReadiness()
    setAuthSignedIn(false)
    const result = await bridge.auth.clear({ scope, url, headers: editorAuthHeaders(editor) })

    if (!result.ok) {
      setAuthError(result.error)
    }
  }

  const save = useCallback(
    async (allowPlainTextToken = false) => {
      if (!bridge || !editor) {
        return
      }

      // Duplicate prevention lives in the save path (not just a disabled
      // button): reject a candidate that collides with an existing entry with
      // an inline error before anything crosses the IPC boundary.
      const dupe = findDuplicateConnection(editor, registry?.connections ?? [])

      if (dupe) {
        setDupeError(
          editor.kind === 'local'
            ? s.duplicateLocal
            : editor.kind === 'ssh'
              ? s.duplicateSsh(dupe.label)
              : s.duplicateUrl(dupe.label)
        )

        return
      }

      if (
        !allowPlainTextToken &&
        registry?.secureTokenStorage === false &&
        editor.kind === 'remote' &&
        editor.authMode === 'token' &&
        editor.token.trim()
      ) {
        setPlainTextConfirm(true)

        return
      }

      setDupeError(null)
      setSaving(true)

      try {
        const payload: DesktopRegistryConnectionInput = {
          kind: editor.kind,
          label: editor.label
        }

        if (editor.id) {
          payload.id = editor.id
        } else if (editor.kind === 'remote' && authTargetConnectionId.current) {
          payload.id = authTargetConnectionId.current
        }

        if (editor.kind === 'remote' || editor.kind === 'cloud') {
          payload.url = editor.url
          payload.authMode = editor.authMode

          if (authScopeRef.current.owned && authScopeRef.current.scope) {
            payload.authDraftScope = authScopeRef.current.scope
          }

          if (editor.kind === 'remote' && authReadinessCapability.current) {
            payload.authReadinessCapability = authReadinessCapability.current
          }

          if (editor.token.trim()) {
            payload.token = editor.token.trim()
          }

          if (allowPlainTextToken) {
            payload.allowPlainTextToken = true
          }

          // Authoritative header map: typed value → new secret; empty value on
          // a stored row → null (keep saved secret); a removed or blank-name row
          // is absent. Always send the map (including {}) so Electron cannot
          // inherit credentials from the previously stored connection.
          const headerEntries = editor.headers
            .map(row => ({ name: row.name.trim(), stored: row.stored, value: row.value.trim() }))
            .filter(row => row.name)

          payload.headers = Object.fromEntries(
            headerEntries.map(row => [row.name, row.value ? row.value : row.stored ? null : ''])
          )
        } else if (editor.kind === 'ssh') {
          // The composite host string (user@host:port) is the single source
          // of truth — never send separate user/port (see editorFromConnection).
          payload.host = editor.host
          payload.keyPath = editor.keyPath || undefined
        }

        // Readiness proof is one-shot renderer state: forward the value captured
        // above exactly once and burn the local copy before crossing IPC.
        authReadinessCapability.current = null
        setAuthReadyGeneration(null)
        const result = await bridge.save(payload)
        publishRegistry(result.registry)
        setAuthScopeState(null, false)
        resetEditorAuth()
        setEditor(null)
        setPlainTextConfirm(false)
      } catch (err) {
        notifyError(err, s.saveFailed)
      } finally {
        setSaving(false)
      }
    },
    [bridge, editor, publishRegistry, registry?.connections, registry?.secureTokenStorage, resetEditorAuth, s]
  )

  const remove = useCallback(async () => {
    if (!bridge || !removeTarget) {
      return
    }

    setBusyId(removeTarget.id)

    try {
      const result = await bridge.remove(removeTarget.id)
      publishRegistry(result.registry)
    } catch (err) {
      notifyError(err, s.removeFailed)
    } finally {
      setBusyId(null)
      setRemoveTarget(null)
    }
  }, [bridge, publishRegistry, removeTarget, s.removeFailed])

  const makePrimary = useCallback(
    async (id: string) => {
      if (!bridge) {
        return
      }

      setBusyId(id)

      try {
        const result = await bridge.setPrimary(id)
        publishRegistry(result.registry)
      } catch (err) {
        notifyError(err, s.saveFailed)
      } finally {
        setBusyId(null)
      }
    },
    [bridge, publishRegistry, s.saveFailed]
  )

  const setLaunchMode = useCallback(
    async (mode: 'last-used' | 'primary') => {
      if (!bridge?.setLaunchMode) {
        return
      }

      setLaunchModeBusy(true)

      try {
        const result = await bridge.setLaunchMode(mode)
        publishRegistry(result.registry)
      } catch (err) {
        notifyError(err, s.saveFailed)
      } finally {
        setLaunchModeBusy(false)
      }
    },
    [bridge, publishRegistry, s.saveFailed]
  )

  const test = useCallback(
    async (conn: DesktopRegistryConnection) => {
      if (!bridge) {
        return
      }

      setTestingId(conn.id)

      try {
        const result = await bridge.test(conn.id)
        const reachable = result.ok === true || result.reachable === true

        if (reachable) {
          notify({ title: conn.label, message: s.testOk })
        } else {
          if (isConnectionAuthRejection(result)) {
            setReauthIds(current => new Set(current).add(conn.id))
          }

          notifyError(new Error(result.error || conn.label), s.testFailed)
        }
      } catch (err) {
        if (isConnectionAuthRejection(err)) {
          setReauthIds(current => new Set(current).add(conn.id))
        }

        notifyError(err, s.testFailed)
      } finally {
        setTestingId(null)
      }
    },
    [bridge, s.testFailed, s.testOk]
  )

  // Fan out `hermes update` to every eligible source; per-connection results
  // land as individual toasts so one dead box doesn't hide the others.
  const updateAll = useCallback(async () => {
    if (!bridge?.updateAll) {
      return
    }

    setUpdatingAll(true)

    try {
      const { results } = await bridge.updateAll()

      for (const row of results) {
        if (row.ok) {
          notify({ title: row.label, message: row.detail || s.updateAllDone })
        } else if (row.skipped && row.reason === 'cloud-managed') {
          notify({ title: row.label, message: s.updateSkippedCloud })
        } else {
          notifyError(new Error(row.error || row.detail || row.reason || row.label), s.updateAllFailed)
        }
      }
    } catch (err) {
      notifyError(err, s.updateAllFailed)
    } finally {
      setUpdatingAll(false)
    }
  }, [bridge, s.updateAllDone, s.updateAllFailed, s.updateSkippedCloud])

  const kindMeta: Record<DesktopConnectionKind, { label: string; desc: string }> = {
    cloud: { desc: s.kindCloudDesc, label: s.kindCloud },
    local: { desc: s.kindLocalDesc, label: s.kindLocal },
    remote: { desc: s.kindRemoteDesc, label: s.kindRemote },
    ssh: { desc: s.kindSshDesc, label: s.kindSsh }
  }

  const sortedConnections = useMemo(
    () => sortConnectionsForDisplay(registry?.connections ?? []),
    [registry?.connections]
  )

  const showSearch = sortedConnections.length >= CONNECTION_SEARCH_THRESHOLD
  const effectiveSearchQuery = showSearch ? searchQuery : ''

  const displayedConnections = sortedConnections.filter(connection =>
    connectionMatchesQuery(connection, effectiveSearchQuery, [kindMeta[connection.kind].label])
  )

  useLayoutEffect(() => {
    const previousTop = pendingSearchTopRef.current
    const input = searchInputRef.current

    pendingSearchTopRef.current = null

    if (previousTop == null || !input) {
      return
    }

    const scroller = scrollableAncestor(input)

    if (!scroller) {
      return
    }

    const delta = input.getBoundingClientRect().top - previousTop

    if (Math.abs(delta) > 0.5) {
      scroller.scrollTop += delta
    }
  }, [displayedConnections.length, effectiveSearchQuery])

  const updateSearchQuery = (nextQuery: string) => {
    pendingSearchTopRef.current = searchInputRef.current?.getBoundingClientRect().top ?? null
    setSearchQuery(nextQuery)
  }

  if (!bridge) {
    return null
  }

  return (
    <div className="mt-8 border-t border-border/60 pt-6">
      <SectionHeading icon={Globe} title={s.title} />
      <p className="mb-1 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{s.intro}</p>
      {/* Source selection lives in Sessions. Primary is the registry fallback,
          not an immediate workspace switch. */}
      <p className="mb-4 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
        {s.stagedNote}
      </p>

      {!loading && showSearch && (
        <Input
          aria-label={s.searchPlaceholder}
          containerClassName="mt-3 mb-0 w-full max-w-sm"
          onChange={event => updateSearchQuery(event.target.value)}
          placeholder={s.searchPlaceholder}
          prefix={<SearchIcon className="size-3.5" />}
          ref={searchInputRef}
          size="sm"
          type="search"
          value={searchQuery}
        />
      )}

      {loading ? (
        <div className="flex items-center gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          <Loader2 className="size-4 animate-spin" />
        </div>
      ) : !registry || registry.connections.length === 0 ? (
        <EmptyState title={s.empty} />
      ) : displayedConnections.length === 0 ? (
        <EmptyState title={s.noSearchResults} />
      ) : (
        displayedConnections.map(conn => {
          const Icon = KIND_ICONS[conn.kind]
          const isCurrent = activeConnectionId === conn.id
          const isPrimary = registry.primary === conn.id
          const busy = busyId === conn.id
          // Display-only: this connection is a second address for a backend
          // already registered under another entry (same install_id).
          const sameBackendPeer = sameBackendPeerLabel(conn, sortedConnections)

          const baseDescription =
            conn.kind === 'ssh'
              ? `${kindMeta[conn.kind].label} · ${conn.user ? `${conn.user}@` : ''}${conn.host}${conn.port ? `:${conn.port}` : ''}`
              : conn.url
                ? `${kindMeta[conn.kind].label} · ${conn.url}`
                : kindMeta[conn.kind].desc

          return (
            <ListRow
              action={
                <div className="flex items-center gap-2">
                  <Button
                    disabled={testingId === conn.id}
                    onClick={() => {
                      triggerHaptic('selection')
                      void test(conn)
                    }}
                    size="sm"
                    variant="outline"
                  >
                    {testingId === conn.id ? <Loader2 className="size-3.5 animate-spin" /> : s.testConnection}
                  </Button>
                  {!isPrimary && (
                    <Button disabled={busy} onClick={() => void makePrimary(conn.id)} size="sm" variant="outline">
                      {s.makePrimary}
                    </Button>
                  )}
                  {conn.kind !== 'local' && (
                    <>
                      <Button
                        aria-label={s.editConnection}
                        onClick={() => openEditor(editorFromConnection(conn))}
                        size="icon-sm"
                        variant="ghost"
                      >
                        <Pencil className="size-3.5" />
                      </Button>
                      <Button
                        aria-label={s.removeConnection}
                        disabled={busy}
                        onClick={() => setRemoveTarget(conn)}
                        size="icon-sm"
                        variant="ghost"
                      >
                        {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                      </Button>
                    </>
                  )}
                </div>
              }
              description={
                sameBackendPeer ? `${baseDescription} · ${s.sameBackendHint(sameBackendPeer)}` : baseDescription
              }
              key={conn.id}
              title={
                <span className="flex items-center gap-2">
                  <Icon className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{conn.label}</span>
                  {isCurrent && <Pill tone="primary">{s.currentPill}</Pill>}
                  {isPrimary && <Pill>{s.primaryPill}</Pill>}
                  {conn.kind === 'local' && <Pill>{s.managedPill}</Pill>}
                </span>
              }
            />
          )
        })
      )}

      {editor ? (
        <div className="mt-4 space-y-3 rounded-lg border border-border/60 p-4">
          <div className="grid grid-cols-2 gap-2 @2xl:grid-cols-4">
            {/* Kind is fixed once created (buttons disable on edit). On create
                every kind is offered; Local is disabled while the managed
                local entry exists (the registry holds at most one). */}
            {(editor.id ? ([editor.kind] as const) : (['local', 'cloud', 'remote', 'ssh'] as const)).map(kind => (
              <Button
                disabled={Boolean(editor.id) || (kind === 'local' && hasLocal)}
                key={kind}
                onClick={() => {
                  const next = { ...editor, kind }
                  const previous = authScopeRef.current
                  setDupeError(null)
                  resetEditorAuth()
                  editorRef.current = next
                  setAuthScopeState(null, false, normalizeGatewayUrl(next.url))
                  setEditor(next)
                  void clearDraft(previous, editor.url)

                  for (const pending of authDraftPromises.current) {
                    pending.cleanupUrl ||= normalizeGatewayUrl(editor.url)
                  }

                  if (kind === 'remote') {
                    void createOwnedDraft(undefined, normalizeGatewayUrl(next.url), authGeneration.current)
                  }
                }}
                size="sm"
                variant={editor.kind === kind ? 'default' : 'outline'}
              >
                {kindMeta[kind].label}
              </Button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">{kindMeta[editor.kind].desc}</p>
          {!editor.id && hasLocal ? <p className="text-xs text-muted-foreground">{s.localAddHint}</p> : null}
          {!editor.id && editor.kind === 'cloud' ? (
            <p className="text-xs text-muted-foreground">{s.cloudAddHint}</p>
          ) : null}

          <ListRow
            action={
              <Input
                onChange={e => setEditor({ ...editor, label: e.target.value })}
                placeholder={s.labelPlaceholder}
                value={editor.label}
              />
            }
            description={s.labelDesc}
            title={s.labelTitle}
          />

          {(editor.kind === 'remote' || editor.kind === 'cloud') && (
            <ListRow
              action={
                <Input
                  onChange={e => {
                    const url = e.target.value
                    const next = { ...editor, url }
                    setDupeError(null)
                    resetEditorAuth()
                    editorRef.current = next
                    setEditor(next)
                    void ensureScopeForUrl(editor, url)
                  }}
                  placeholder="http://homelab.lan:9119"
                  value={editor.url}
                />
              }
              title={s.urlTitle}
            />
          )}

          {editor.kind === 'remote' && (
            <>
              {!authProbeIsCurrent && (
                <ListRow
                  action={
                    <Button
                      disabled={authProbeBusy || !editor.url.trim()}
                      onClick={() => void probeEditorAuth()}
                      size="sm"
                      variant="outline"
                    >
                      {authProbeBusy ? t.settings.gateway.probing : s.detectAuthentication}
                    </Button>
                  }
                  title={t.settings.gateway.authTitle}
                />
              )}
              {editor.authMode === 'token' && (
                <ListRow
                  action={
                    <div className="flex items-center gap-2">
                      <Input
                        onChange={e => {
                          invalidateReadiness()
                          setEditor({ ...editor, token: e.target.value })
                        }}
                        placeholder={t.settings.gateway.pasteSessionToken}
                        type="password"
                        value={editor.token}
                      />
                      {authProbeIsCurrent ? (
                        <Button
                          disabled={
                            authVerifying || (!editor.token.trim() && (!editingExistingTokenEntry || remoteAuthChanged))
                          }
                          onClick={() => void verifyEditorAuth()}
                          size="sm"
                          variant="outline"
                        >
                          {authVerifying ? t.common.connecting : 'Verify connection'}
                        </Button>
                      ) : null}
                    </div>
                  }
                  description={t.settings.gateway.tokenDesc}
                  title={t.settings.gateway.tokenTitle}
                />
              )}
              {authProbeIsCurrent && editor.authMode === 'oauth' && (!editor.id || remoteEndpointChanged) && (
                <ListRow
                  action={
                    <Button disabled={authSigningIn || authSignedIn} onClick={() => void signInEditorAuth()} size="sm">
                      {authSignedIn
                        ? t.settings.gateway.signedIn
                        : authSigningIn
                          ? t.common.connecting
                          : isPasswordProvider
                            ? t.settings.gateway.signIn
                            : t.settings.gateway.signInWith(providerLabel)}
                    </Button>
                  }
                  title={t.settings.gateway.authTitle}
                />
              )}
              {editor.id && editor.authMode === 'oauth' && !remoteEndpointChanged ? (
                <div className="flex justify-end gap-2">
                  <Button
                    disabled={authSigningIn || authVerifying}
                    onClick={() => void signInEditorAuth()}
                    size="sm"
                    variant="outline"
                  >
                    Reauthenticate
                  </Button>
                  {authSignedIn ? (
                    <Button
                      disabled={authSigningIn || authVerifying}
                      onClick={() => void signOutEditorAuth()}
                      size="sm"
                      variant="outline"
                    >
                      {t.settings.gateway.signOut}
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </>
          )}

          {(editor.kind === 'remote' || editor.kind === 'cloud') && (
            <div className="grid gap-2">
              <div>
                <div className="text-sm font-medium">{s.headersTitle}</div>
                <p className="mt-1 text-xs text-muted-foreground">{s.headersDesc}</p>
              </div>
              {editor.headers.map((row, index) => (
                <div className="flex items-center gap-2" key={index}>
                  <Input
                    className="flex-1"
                    onChange={e => {
                      invalidateReadiness()
                      setEditor({
                        ...editor,
                        headers: editor.headers.map((h, i) => (i === index ? { ...h, name: e.target.value } : h))
                      })
                    }}
                    placeholder="CF-Access-Client-Id"
                    value={row.name}
                  />
                  <Input
                    className="flex-1"
                    onChange={e => {
                      invalidateReadiness()
                      setEditor({
                        ...editor,
                        headers: editor.headers.map((h, i) => (i === index ? { ...h, value: e.target.value } : h))
                      })
                    }}
                    placeholder={row.stored ? s.headerValueSaved : s.headerValuePlaceholder}
                    type="password"
                    value={row.value}
                  />
                  <Button
                    onClick={() => {
                      invalidateReadiness()
                      setEditor({ ...editor, headers: editor.headers.filter((_, i) => i !== index) })
                    }}
                    size="sm"
                    variant="ghost"
                  >
                    {s.headerRemove}
                  </Button>
                </div>
              ))}
              <div>
                <Button
                  onClick={() => {
                    invalidateReadiness()
                    setEditor({ ...editor, headers: [...editor.headers, { name: '', stored: false, value: '' }] })
                  }}
                  size="sm"
                  variant="outline"
                >
                  {s.headerAdd}
                </Button>
              </div>
            </div>
          )}

          {editor.kind === 'ssh' && (
            <ListRow
              action={
                <Input
                  onChange={e => {
                    setDupeError(null)
                    setEditor({ ...editor, host: e.target.value })
                  }}
                  placeholder="user@host:22"
                  value={editor.host}
                />
              }
              title={s.sshHostTitle}
            />
          )}

          {dupeError ? <p className="text-xs text-destructive">{dupeError}</p> : null}
          {authError ? <p className="text-xs text-destructive">{authError}</p> : null}

          <div className="flex justify-end gap-2">
            <Button disabled={saving} onClick={() => openEditor(null)} size="sm" variant="ghost">
              {s.cancel}
            </Button>
            <Button
              disabled={
                saving ||
                !editor.label.trim() ||
                ((editor.kind === 'remote' || editor.kind === 'cloud') && !editor.url.trim()) ||
                (remoteNeedsReadiness && !authReady) ||
                (editor.kind === 'remote' && editor.authMode === 'oauth' && !authSignedIn) ||
                (editor.kind === 'remote' &&
                  authProbe?.authMode === 'token' &&
                  !editor.token.trim() &&
                  !editingExistingTokenEntry)
              }
              onClick={() => void save()}
              size="sm"
            >
              {saving ? s.saving : s.save}
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex items-center gap-2">
          <Button
            onClick={() => {
              triggerHaptic('selection')
              openEditor(emptyEditor('remote'))
            }}
            size="sm"
            variant="outline"
          >
            <Plus className="size-3.5" /> {s.addConnection}
          </Button>
          {bridge?.updateAll && (registry?.connections.length ?? 0) > 1 && (
            <Button
              disabled={updatingAll}
              onClick={() => {
                triggerHaptic('selection')
                void updateAll()
              }}
              size="sm"
              variant="outline"
            >
              {updatingAll ? (
                <>
                  <Loader2 className="size-3.5 animate-spin" /> {s.updateAllRunning}
                </>
              ) : (
                <>
                  <RefreshCw className="size-3.5" /> {s.updateAll}
                </>
              )}
            </Button>
          )}
        </div>
      )}

      {!loading && registry && registry.connections.length > 1 && (
        <div className="mt-6 border-t border-border/60 pt-4">
          <ToggleRow
            checked={registry.launchMode === 'last-used'}
            description={s.launchModeDesc}
            disabled={launchModeBusy || !bridge?.setLaunchMode}
            label={s.launchModeTitle}
            onChange={enabled => void setLaunchMode(enabled ? 'last-used' : 'primary')}
          />
        </div>
      )}

      <ConfirmDialog
        confirmLabel={s.removeConnection}
        description={removeTarget ? s.removeConfirmDesc(removeTarget.label) : ''}
        destructive
        onClose={() => setRemoveTarget(null)}
        onConfirm={() => remove()}
        open={Boolean(removeTarget)}
        title={s.removeConfirmTitle}
      />

      {/* Keyring-less opt-in: same consent flow as the connection-mode form. */}
      <ConfirmDialog
        confirmLabel={t.settings.gateway.plainTextConfirmAction}
        description={t.settings.gateway.plainTextConfirmDesc}
        destructive
        onClose={() => setPlainTextConfirm(false)}
        onConfirm={() => save(true)}
        open={plainTextConfirm}
        title={t.settings.gateway.plainTextConfirmTitle}
      />
    </div>
  )
}

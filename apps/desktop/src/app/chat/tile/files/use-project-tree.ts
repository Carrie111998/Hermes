import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react'

import { $connection } from '@/store/session'
import { $workspaceChangeTick, consumeWorkspaceChange } from '@/store/workspace-events'

import { clearProjectDirCache, type ProjectTreeEntry, readProjectDir } from './ipc'

export interface TreeNode {
  /** Absolute filesystem path. Doubles as react-arborist node id. */
  id: string
  name: string
  /** Drives arborist's leaf-vs-expandable decision via childrenAccessor. */
  isDirectory: boolean
  /** `undefined` = directory, children not yet loaded. `[]` = loaded empty. */
  children?: TreeNode[]
  /** True while a readDir for this folder is in flight. */
  loading?: boolean
  /** Synthetic loading/error rows are not real filesystem entries. */
  placeholder?: 'error' | 'loading'
  /** Last error code from readDir (e.g. EACCES). Cleared on next successful load. */
  error?: string
}

const PLACEHOLDER_ID = '__loading__'
const ERROR_PLACEHOLDER_ID = '__error__'

function makeNode(path: string, name: string, isDirectory: boolean): TreeNode {
  return { id: path, isDirectory, name }
}

function patchNode(nodes: TreeNode[] | undefined | null, id: string, patch: (n: TreeNode) => TreeNode): TreeNode[] {
  if (!nodes) {
    return []
  }

  return nodes.map(n => {
    if (n.id === id) {
      return patch(n)
    }

    if (n.children && n.children.length > 0) {
      return { ...n, children: patchNode(n.children, id, patch) }
    }

    return n
  })
}

function findNode(nodes: TreeNode[], id: string): null | TreeNode {
  for (const node of nodes) {
    if (node.id === id) {
      return node
    }

    if (node.children?.length) {
      const hit = findNode(node.children, id)

      if (hit) {
        return hit
      }
    }
  }

  return null
}

// Merge a freshly-read dir's entries into its existing children: keep surviving
// nodes (subtrees intact), add new, drop deleted. Non-recursive — a grandchild
// dir only re-reads when it's itself in the change set.
function mergeChildren(existing: TreeNode[], entries: ProjectTreeEntry[]): TreeNode[] {
  const byId = new Map(existing.filter(node => !node.placeholder).map(node => [node.id, node]))

  return entries.map(entry => byId.get(entry.path) ?? makeNode(entry.path, entry.name, entry.isDirectory))
}

function placeholderChild(parentId: string): TreeNode {
  return { id: `${parentId}::${PLACEHOLDER_ID}`, isDirectory: false, name: 'Loading…', placeholder: 'loading' }
}

function errorChild(parentId: string, error: string | undefined): TreeNode {
  return {
    id: `${parentId}::${ERROR_PLACEHOLDER_ID}`,
    isDirectory: false,
    name: `Unable to read (${error || 'read-error'})`,
    placeholder: 'error'
  }
}

export interface UseProjectTreeResult {
  /** Bumped by collapseAll so callers can remount the tree fully collapsed. */
  collapseNonce: number
  data: TreeNode[]
  /** Directory actually displayed — differs from the requested cwd when the
   *  session's recorded cwd no longer exists and we fell back to the default
   *  workspace dir. */
  effectiveCwd: string
  openState: Record<string, boolean>
  rootError: string | null
  rootLoading: boolean
  collapseAll: () => void
  loadChildren: (id: string) => Promise<void>
  refreshRoot: () => Promise<void>
  setNodeOpen: (id: string, open: boolean) => void
}

interface ProjectTreeState {
  collapseNonce: number
  cwd: string
  data: TreeNode[]
  loaded: boolean
  openState: Record<string, boolean>
  requestId: number
  /** Directory the displayed entries were read from ('' until first load). */
  resolvedCwd: string
  rootError: string | null
  rootLoading: boolean
}

const initialState: ProjectTreeState = {
  collapseNonce: 0,
  cwd: '',
  data: [],
  loaded: false,
  openState: {},
  requestId: 0,
  resolvedCwd: '',
  rootError: null,
  rootLoading: false
}

const inflight = new Set<string>()
let nextRootRequestId = 0
let lastConnectionKey = ''

// Per-cwd tree states. A tree belongs to ONE directory (a session/tile's
// workspace), so the old single shared atom could only serve one consumer:
// opening a second tile's tree clobbered the first's state. Now each cwd gets
// its own bucket and hooks subscribe to the version atom, so N tiles can
// browse N directories simultaneously with fully independent state.
const treeStates = new Map<string, ProjectTreeState>()
const $treeVersion = atom(0)

// Reset generation. `resetProjectTreeState()` (gateway scope change / connection
// switch) clears every tree state and bumps the root-request id, which makes any
// in-flight readDir's results stale and drops them. A mounted `useProjectTree`
// that subscribed before the reset must re-read its root — otherwise the tree
// strands on the loading/empty state until the user manually refreshes. This
// generation counter is bumped on every reset, and every hooked tree watches it
// so it self-heals by force-reloading its root immediately after a reset.
const $treeResetGeneration = atom(0)

function bumpTreeVersion() {
  $treeVersion.set($treeVersion.get() + 1)
}

// useSyncExternalStore subscription for the per-cwd tree states. Every
// setTreeState/clearTreeState bumps $treeVersion, so subscribers re-read their
// snapshot exactly when the buckets change.
function subscribeTreeVersion(onStoreChange: () => void): () => void {
  return $treeVersion.subscribe(onStoreChange)
}

function treeStateFor(cwd: string): ProjectTreeState {
  return treeStates.get(cwd) ?? initialState
}

function setTreeState(cwd: string, updater: (current: ProjectTreeState) => ProjectTreeState) {
  const next = updater(treeStateFor(cwd))

  treeStates.set(cwd, next)
  bumpTreeVersion()
}

function clearTreeState(cwd: string) {
  treeStates.delete(cwd)
  bumpTreeVersion()
}

// While the root is errored (ENOENT during a session's cwd race, a folder that
// reappears after a checkout, a remote that wasn't ready), keep retrying on a
// slow cadence so the tree self-heals instead of staying "UNREADABLE" forever.
const ROOT_ERROR_RETRY_MS = 3_000

/** Sessions record their launch cwd; deleted worktrees and remote-backend
 *  paths arrive here as directories that don't exist on this machine. Rather
 *  than bricking the tree, display the sanitized workspace fallback (main
 *  prefers the configured default project dir). Local connections only —
 *  remote trees are read through the remote bridge. */
async function fallbackRootFor(cwd: string): Promise<string | null> {
  if ($connection.get()?.mode === 'remote') {
    return null
  }

  const sanitize = window.hermesDesktop?.sanitizeWorkspaceCwd

  if (!sanitize) {
    return null
  }

  try {
    const { cwd: fallback, sanitized } = await sanitize(cwd)

    return sanitized && fallback && fallback !== cwd ? fallback : null
  } catch {
    return null
  }
}

async function loadRoot(cwd: string, { force = false }: { force?: boolean } = {}) {
  if (!cwd) {
    clearTreeState('')

    return
  }

  const current = treeStateFor(cwd)

  if (!force && current.cwd === cwd && (current.loaded || current.rootLoading)) {
    return
  }

  const requestId = nextRootRequestId + 1
  nextRootRequestId = requestId
  inflight.clear()

  if (force || current.cwd !== cwd) {
    clearProjectDirCache(cwd)
  }

  setTreeState(cwd, latest => ({
    collapseNonce: latest.collapseNonce,
    cwd,
    data: [],
    loaded: false,
    openState: latest.cwd === cwd ? latest.openState : {},
    requestId,
    resolvedCwd: '',
    rootError: null,
    rootLoading: true
  }))

  let resolvedCwd = cwd
  let entries: ProjectTreeEntry[] = []
  let error: string | null | undefined

  // A rejecting readDesktopDir (bridge/preload not yet ready on cold start,
  // transient IPC failure) must NOT strand the tree on its loading state —
  // catch it and surface it as a rootError so the self-heal retry re-probes.
  try {
    const result = await readProjectDir(cwd, cwd)

    entries = result.entries
    error = result.error
  } catch (err) {
    error = err instanceof Error ? err.message : 'read-error'
  }

  if (error) {
    const fallback = await fallbackRootFor(cwd)

    if (fallback) {
      try {
        const retry = await readProjectDir(fallback, fallback)

        if (!retry.error) {
          resolvedCwd = fallback
          entries = retry.entries
          error = undefined
        }
      } catch (err) {
        // Keep the original primary-cwd error; the fallback read failed too.
        if (!error) {
          error = err instanceof Error ? err.message : 'read-error'
        }
      }
    }
  }

  setTreeState(cwd, latest => {
    // Only the requestId guards staleness here. The `latest.cwd !== cwd` check
    // is deliberately NOT used: `resetProjectTreeState()` clears `treeStates`,
    // so a loadRoot that started before a reset resolves against the cleared
    // bucket (initialState, cwd='') — and the cwd check would then drop a
    // perfectly fresh result, stranding the tree on its loading state until the
    // user remounts it. requestId is bumped on every reset, so it alone
    // distinguishes a stale in-flight read from the current one.
    if (latest.requestId !== requestId) {
      return latest
    }
    return {
      ...latest,
      data: error ? [] : entries.map(e => makeNode(e.path, e.name, e.isDirectory)),
      loaded: true,
      resolvedCwd,
      rootError: error || null,
      rootLoading: false
    }
  })
}

export function resetProjectTreeState() {
  lastConnectionKey = ''
  nextRootRequestId += 1
  inflight.clear()
  treeStates.clear()
  clearProjectDirCache()
  bumpTreeVersion()
  // Every subscribed tree force-reloads its root on the next render so a reset
  // can never strand a tree on its old (now-dropped) loading/empty state.
  $treeResetGeneration.set($treeResetGeneration.get() + 1)
}

// Non-destructive live refresh as the agent edits: preserves expansion + loaded
// subtrees (stable absolute-path ids let rows animate in/out), never collapses.
// Targeted by default — re-reads only the changed dirs in `change`; the root and
// untouched folders never touch the filesystem or re-render. Falls back to
// re-reading every loaded dir only when the mutation is opaque (a terminal
// command / a path we couldn't resolve) — see store/workspace-events.
async function revalidateTree(cwd: string, change: { dirs: string[]; full: boolean }): Promise<void> {
  const state = treeStateFor(cwd)

  if (!cwd || state.cwd !== cwd || !state.loaded) {
    return
  }

  const rootPath = state.resolvedCwd || cwd

  if (!change.full && change.dirs.length) {
    // Only re-read changed dirs that are actually loaded (root, or an expanded
    // folder); a change inside a collapsed/absent dir isn't visible → skip.
    const targets = change.dirs.filter(dir => dir === rootPath || findNode(state.data, dir)?.children)

    if (!targets.length) {
      return
    }

    const reads = await Promise.all(targets.map(async dir => ({ dir, ...(await readProjectDir(dir, rootPath)) })))

    setTreeState(cwd, latest => {
      if (latest.cwd !== cwd || !latest.loaded) {
        return latest
      }

      let data = latest.data

      for (const { dir, entries, error } of reads) {
        if (error) {
          continue // keep last-known children on a transient read error
        }

        data =
          dir === rootPath
            ? mergeChildren(data, entries)
            : patchNode(data, dir, node =>
                node.children ? { ...node, children: mergeChildren(node.children, entries) } : node
              )
      }

      return data === latest.data ? latest : { ...latest, data }
    })

    return
  }

  // Opaque fallback: reconcile every loaded dir. Siblings read concurrently
  // (Promise.all keeps order); loaded subfolders recurse.
  const reconcile = async (dirPath: string, existing: TreeNode[]): Promise<TreeNode[]> => {
    const { entries, error } = await readProjectDir(dirPath, rootPath)

    if (error) {
      return existing
    }

    const byId = new Map(existing.filter(node => !node.placeholder).map(node => [node.id, node]))

    return Promise.all(
      entries.map(async entry => {
        const prev = byId.get(entry.path)

        if (prev?.isDirectory && prev.children) {
          return { ...prev, children: await reconcile(prev.id, prev.children) }
        }

        return prev ?? makeNode(entry.path, entry.name, entry.isDirectory)
      })
    )
  }

  const nextData = await reconcile(rootPath, state.data)

  setTreeState(cwd, latest => (latest.cwd === cwd && latest.loaded ? { ...latest, data: nextData } : latest))
}

/**
 * Lazy-loads a directory tree rooted at `cwd`. Children are fetched on first
 * expand and cached in this feature-owned atom so unrelated chat rerenders or
 * remounts cannot reset the browser. A placeholder leaf renders so the
 * disclosure caret shows for unloaded folders. `refreshRoot` invalidates the
 * whole tree (used after cwd change or manual refresh).
 */
export function useProjectTree(cwd: string): UseProjectTreeResult {
  useStore($treeVersion)
  // Read the per-cwd bucket through useSyncExternalStore, NOT a plain function
  // call. The React Compiler memoizes plain helper calls by their arguments
  // (`t[0]===e ? cached : recompute`), and `treeStateFor` reads the module-level
  // mutable `treeStates` map — invisible to the compiler, so the FIRST snapshot
  // taken for a cwd (an empty initialState on cold start, before loadRoot's
  // bucket exists) gets cached and returned forever while cwd stays unchanged.
  // The tree then strands on its loading skeleton even though the data layer
  // applied its result — exactly the cold-start bug. A hook call is never
  // memoized away, and getSnapshot re-reads the Map on every render.
  const state = useSyncExternalStore(subscribeTreeVersion, () => treeStateFor(cwd))
  const connection = useStore($connection)
  const workspaceTick = useStore($workspaceChangeTick)
  const connectionKey = `${connection?.mode || 'local'}:${connection?.profile || ''}:${connection?.baseUrl || ''}`

  const refreshRoot = useCallback(() => loadRoot(cwd, { force: true }), [cwd])

  const setNodeOpen = useCallback(
    (id: string, open: boolean) => {
      setTreeState(cwd, current => {
        if (current.cwd !== cwd || current.openState[id] === open) {
          return current
        }

        return {
          ...current,
          openState: {
            ...current.openState,
            [id]: open
          }
        }
      })
    },
    [cwd]
  )

  // Clears the recorded open state and bumps the nonce; the tree is keyed on
  // the nonce so it remounts with everything collapsed (loaded children stay
  // cached in `data`, just hidden).
  const collapseAll = useCallback(() => {
    setTreeState(cwd, current => {
      if (current.cwd !== cwd) {
        return current
      }

      return { ...current, collapseNonce: current.collapseNonce + 1, openState: {} }
    })
  }, [cwd])

  const loadChildren = useCallback(
    async (id: string) => {
      if (!cwd || inflight.has(id)) {
        return
      }

      inflight.add(id)

      setTreeState(cwd, current => {
        if (current.cwd !== cwd) {
          return current
        }

        return {
          ...current,
          data: patchNode(current.data, id, n => ({ ...n, loading: true, children: [placeholderChild(n.id)] }))
        }
      })

      const rootPath = treeStateFor(cwd).resolvedCwd || cwd
      const { entries, error } = await readProjectDir(id, rootPath)

      inflight.delete(id)

      setTreeState(cwd, current => {
        if (current.cwd !== cwd) {
          return current
        }

        return {
          ...current,
          data: patchNode(current.data, id, n => ({
            ...n,
            loading: false,
            error: error || undefined,
            children: error ? [errorChild(n.id, error)] : entries.map(e => makeNode(e.path, e.name, e.isDirectory))
          }))
        }
      })
    },
    [cwd]
  )

  // Live, non-destructive refresh when the agent touches the tree (skip the
  // very first render: tick 0 is the initial value, not a real change).
  useEffect(() => {
    if (workspaceTick > 0) {
      void revalidateTree(cwd, consumeWorkspaceChange())
    }
  }, [workspaceTick, cwd])

  useEffect(() => {
    const connectionChanged = lastConnectionKey !== '' && lastConnectionKey !== connectionKey
    lastConnectionKey = connectionKey

    if (connectionChanged) {
      clearProjectDirCache()
      void loadRoot(cwd, { force: true })

      return
    }

    void loadRoot(cwd)
  }, [connectionKey, cwd])

  // Self-heal across `resetProjectTreeState()`: a reset clears every tree state
  // and bumps the root-request id, which drops any in-flight readDir result. A
  // tree mounted before the reset would otherwise strand on its old
  // loading/empty state until the user manually refreshes. Watch the reset
  // generation and force-reload the root each time it moves, but skip the very
  // first value of the current mount (the connectionKey effect already seeds it).
  const treeResetGeneration = useStore($treeResetGeneration)
  const lastResetGeneration = useRef($treeResetGeneration.get())

  useEffect(() => {
    if (treeResetGeneration === lastResetGeneration.current) {
      return
    }

    lastResetGeneration.current = treeResetGeneration
    void loadRoot(cwd, { force: true })
  }, [cwd, treeResetGeneration])

  // Self-heal: an errored root re-probes every few seconds while the tree is
  // mounted. Each attempt bumps requestId, so a persistent error re-arms the
  // timer; a success clears rootError and stops it.
  useEffect(() => {
    if (!cwd || state.cwd !== cwd || !state.rootError) {
      return
    }

    const timer = window.setTimeout(() => void loadRoot(cwd, { force: true }), ROOT_ERROR_RETRY_MS)

    return () => window.clearTimeout(timer)
  }, [cwd, state.cwd, state.requestId, state.rootError])

  // While showing the fallback root, quietly re-probe the session's real cwd
  // (a worktree re-created, a checkout restored) and switch back when it
  // reappears. The probe never touches state, so there's no flicker.
  const usingFallback = state.cwd === cwd && Boolean(state.resolvedCwd) && state.resolvedCwd !== cwd

  useEffect(() => {
    if (!cwd || !usingFallback) {
      return
    }

    let cancelled = false

    const timer = window.setInterval(() => {
      void readProjectDir(cwd, cwd).then(({ error }) => {
        if (!cancelled && !error) {
          void loadRoot(cwd, { force: true })
        }
      })
    }, ROOT_ERROR_RETRY_MS)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [cwd, usingFallback])

  return useMemo(
    () => ({
      collapseAll,
      collapseNonce: state.cwd === cwd ? state.collapseNonce : 0,
      data: state.cwd === cwd ? state.data : [],
      effectiveCwd: state.cwd === cwd && state.resolvedCwd ? state.resolvedCwd : cwd,
      loadChildren,
      openState: state.cwd === cwd ? state.openState : {},
      refreshRoot,
      rootError: state.cwd === cwd ? state.rootError : null,
      rootLoading: state.cwd === cwd ? state.rootLoading : Boolean(cwd),
      setNodeOpen
    }),
    [
      collapseAll,
      cwd,
      loadChildren,
      refreshRoot,
      setNodeOpen,
      state.collapseNonce,
      state.cwd,
      state.data,
      state.openState,
      state.resolvedCwd,
      state.rootError,
      state.rootLoading
    ]
  )
}

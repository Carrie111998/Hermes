import { useCallback, useRef } from 'react'

import { getCronJobs, listAllProfileSessions, listSidebarSessions, type SessionInfo } from '@/hermes'
import { translateNow } from '@/i18n'
import { sameCronSignature } from '@/lib/session-signatures'
import {
  isMessagingSource,
  LOCAL_SESSION_SOURCE_IDS,
  MESSAGING_SESSION_SOURCE_IDS,
  normalizeSessionSource
} from '@/lib/session-source'
import { setCronJobs } from '@/store/cron'
import {
  $pinnedSessionIds,
  $sessionsLimit,
  bumpSessionsLimit,
  SIDEBAR_SESSIONS_INITIAL_LIMIT,
  SIDEBAR_SESSIONS_PAGE_SIZE
} from '@/store/layout'
import { notify } from '@/store/notifications'
import { ALL_PROFILES, normalizeProfileKey, setShowAllProfiles } from '@/store/profile'
import {
  $messagingSessions,
  $selectedStoredSessionId,
  $sessions,
  CRON_SECTION_LIMIT,
  mergeSessionPage,
  MESSAGING_SECTION_LIMIT,
  setCronSessions,
  setMessagingPlatformTotals,
  setMessagingSessions,
  setMessagingTruncated,
  setSessionProfileTotals,
  setSessions,
  setSessionsLoading,
  setSessionsTotal
} from '@/store/session'
import { $workingSessionIds, getRecentlySettledSessionIds } from '@/store/session-states'

// The recents list is local-only: cron rows have their own section, and each
// messaging platform (telegram, discord, …) is fetched separately into its own
// self-managed sidebar section (refreshMessagingSessions). Excluding both here
// keeps "Load more" paging through interactive local chats instead of
// interleaving gateway threads that bury them.
const SIDEBAR_EXCLUDED_SOURCES = ['cron', 'subagent', 'tool', ...MESSAGING_SESSION_SOURCE_IDS]
// The messaging slice is the inverse: drop cron + every local source so only
// external-platform conversations remain, then split per platform in the UI.
const MESSAGING_EXCLUDED_SOURCES = ['cron', ...LOCAL_SESSION_SOURCE_IDS]

// Rows a session refresh must preserve even if the aggregator omits them:
// in-flight first turns (message_count 0), pinned rows aged off the page, the
// actively-viewed chat (its "working" flag clears a beat before the aggregator
// sees the persisted row), and sessions whose turn just settled (same race, but
// for a chat the user has already navigated away from). Pass `scope` to only
// keep the active row when it belongs to the profile being paged.
function sessionsToKeep(scope?: string): Set<string> {
  const keep = new Set<string>([
    ...$workingSessionIds.get(),
    ...$pinnedSessionIds.get(),
    ...getRecentlySettledSessionIds()
  ])

  const active = $selectedStoredSessionId.get()

  if (active) {
    const session = scope ? $sessions.get().find(s => s.id === active) : null

    if (!scope || !session || normalizeProfileKey(session.profile) === scope) {
      keep.add(active)
    }
  }

  return keep
}

interface UseSessionListActionsArgs {
  profileScope: string
}

/** Owns the sidebar's session-list fetching + paging: recents, cron runs/jobs,
 *  and the per-platform messaging slices. Returns the callbacks the controller
 *  wires into the sidebar and refresh effects. */
export function useSessionListActions({ profileScope }: UseSessionListActionsArgs) {
  const refreshSessionsRequestRef = useRef(0)
  const hydratedProfileTotalsRef = useRef<Map<string, number>>(new Map())
  // Ghost scope we already told the user about, so the fallback notice fires
  // once per scope value rather than once per refresh tick.
  const ghostScopeNotifiedRef = useRef<null | string>(null)

  // Messaging-platform sessions as their own slice, fetched separately from
  // local recents so each platform renders a self-managed section and never
  // competes with local chats for the recents page budget. One combined fetch
  // seeds every platform; the sidebar splits the rows per source.
  const refreshMessagingSessions = useCallback(async () => {
    try {
      const result = await listAllProfileSessions(MESSAGING_SECTION_LIMIT, 1, 'exclude', 'recent', 'all', {
        excludeSources: MESSAGING_EXCLUDED_SOURCES
      })

      // Drop any non-messaging source the broad exclude didn't catch (custom
      // sources) — those stay in local recents, not a platform section.
      const rows = result.sessions.filter(s => isMessagingSource(s.source))

      setMessagingSessions(prev => (sameCronSignature(prev, rows) ? prev : rows))
      // Hit the cap → at least one platform may have more on disk than loaded,
      // so platform sections offer their own per-platform "load more".
      setMessagingTruncated(result.sessions.length >= MESSAGING_SECTION_LIMIT)
    } catch {
      // Non-fatal: the messaging sections just stay empty/stale.
    }
  }, [])

  // Page a single platform's section independently (mirrors the per-profile
  // pager): fetch that source's next window and merge it back in place, leaving
  // every other platform's rows untouched. Resolves the platform's exact total.
  const loadMoreMessagingForPlatform = useCallback(async (platform: string) => {
    const inPlatform = (s: SessionInfo) => normalizeSessionSource(s.source) === platform
    const loaded = $messagingSessions.get().filter(inPlatform).length

    const result = await listAllProfileSessions(loaded + SIDEBAR_SESSIONS_PAGE_SIZE, 1, 'exclude', 'recent', 'all', {
      source: platform
    })

    const incoming = result.sessions.filter(s => normalizeSessionSource(s.source) === platform)

    setMessagingSessions(prev => [
      ...prev.filter(s => !inPlatform(s)),
      ...mergeSessionPage(prev.filter(inPlatform), incoming, sessionsToKeep())
    ])

    const total = result.total ?? incoming.length
    setMessagingPlatformTotals(prev => ({ ...prev, [platform]: Math.max(total, incoming.length) }))
  }, [])

  // Cron *jobs* drive the sidebar "Cron jobs" section. Jobs are created
  // synchronously (agent tool call or the cron UI), so refreshing here right
  // after an agent turn surfaces a new job immediately; the interval poll keeps
  // next-run/state fresh as the scheduler advances them. Jobs live per-profile
  // on disk and the list endpoint aggregates 'all' by default, so scope the
  // fetch to the sidebar's profile scope — a concrete profile sees only its
  // own jobs; ALL_PROFILES keeps the unified view.
  const refreshCronJobs = useCallback(async () => {
    try {
      const jobs = await getCronJobs(profileScope === ALL_PROFILES ? 'all' : profileScope)

      setCronJobs(jobs)
    } catch {
      // Non-fatal: the cron section just keeps its last-known jobs.
    }
  }, [profileScope])

  const refreshSessions = useCallback(async () => {
    const requestId = refreshSessionsRequestRef.current + 1
    refreshSessionsRequestRef.current = requestId
    const sessionsEmpty = $sessions.get().length === 0

    // A soft gateway switch clears gateway-bound stores without remounting this
    // hook. Empty cache means the per-profile hydration memo belongs to the old
    // backend and cannot suppress a same-name, same-total catalog fetch.
    if (sessionsEmpty) {
      hydratedProfileTotalsRef.current.clear()
    }

    // The loading flag exists to drive the initial skeletons (they only render
    // while the list is empty). Turn-complete / reconnect refreshes over a
    // populated list used to flip it true→false anyway, churning every
    // $sessionsLoading subscriber twice per turn for no visible change.
    const showLoading = sessionsEmpty

    if (showLoading) {
      setSessionsLoading(true)
    }

    try {
      const limit = Math.min(Math.max(1, $sessionsLimit.get()), SIDEBAR_SESSIONS_INITIAL_LIMIT)

      // Require at least one message so abandoned/empty "Untitled" drafts (one
      // was created per TUI/desktop launch before the lazy-create fix) don't
      // clutter the sidebar.
      // Unified cross-profile list (served read-only off each profile's
      // state.db; no per-profile backend is spawned). Single-profile users get
      // the same rows tagged profile="default".
      // Scope recents to the active profile (not always 'all') so a profile
      // with few recent sessions isn't windowed out of the cross-profile
      // recency page — the empty-history-on-profile-switch bug. Cron + messaging
      // stay cross-profile.
      const sessionProfile = profileScope === ALL_PROFILES ? 'all' : profileScope

      const sidebarRequest = {
        recentsProfile: sessionProfile,
        recentsLimit: limit,
        recentsExclude: SIDEBAR_EXCLUDED_SOURCES,
        cronLimit: CRON_SECTION_LIMIT,
        messagingLimit: MESSAGING_SECTION_LIMIT,
        messagingExclude: MESSAGING_EXCLUDED_SOURCES
      }

      // Batched: one request opens each profile DB once and returns all three
      // source-scoped slices, instead of three separate listAllProfileSessions
      // calls that each reopened + re-counted every profile DB per refresh.
      const result = await listSidebarSessions(sidebarRequest)

      if (refreshSessionsRequestRef.current === requestId) {
        // A concrete scope the backend doesn't recognize (profile deleted on
        // disk, or a stray stored preference adopted at boot) matches zero
        // profile DBs and comes back as an empty recents slice with no error —
        // left alone, the sidebar renders permanently empty. Fall back to the
        // all-profiles view (the scope change re-runs this refresh) and tell
        // the user once. `=== false` keeps older backends and the legacy
        // per-slice fallback (no indicator) on today's behavior.
        if (sessionProfile !== 'all' && result.recents.profile_matched === false) {
          if (ghostScopeNotifiedRef.current !== sessionProfile) {
            ghostScopeNotifiedRef.current = sessionProfile
            notify({
              kind: 'info',
              title: translateNow('desktop.profileScopeMissingTitle'),
              message: translateNow('desktop.profileScopeMissingMessage', sessionProfile)
            })
          }

          setShowAllProfiles(true)

          return
        }

        const recents = result.recents

        // The bounded first page is only additive information. Keep every
        // cached row (plus the normal live/pinned survivors) so a short page or
        // a partially failed batched response can update recent rows without
        // making an older ordinary conversation disappear.
        setSessions(prev => {
          const keep = sessionsToKeep()

          for (const session of prev) {
            keep.add(session.id)
          }

          const next = mergeSessionPage(prev, recents.sessions, keep)

          return sameCronSignature(prev, next) ? prev : next
        })
        setSessionsTotal(typeof recents.total === 'number' ? recents.total : recents.sessions.length)
        setSessionProfileTotals(prev => {
          const next = recents.profile_totals ?? {}
          const prevKeys = Object.keys(prev)

          return prevKeys.length === Object.keys(next).length && prevKeys.every(key => prev[key] === next[key])
            ? prev
            : next
        })

        // Cron section: latest N cron sessions (kept so a pinned cron run still
        // resolves via sessionByAnyId), signature-gated like above.
        setCronSessions(prev => (sameCronSignature(prev, result.cron.sessions) ? prev : result.cron.sessions))

        // Messaging sections: drop any non-messaging source the broad exclude
        // didn't catch (custom sources stay in local recents), then split per
        // platform in the UI.
        const messagingRows = result.messaging.sessions.filter(s => isMessagingSource(s.source))

        setMessagingSessions(prev => (sameCronSignature(prev, messagingRows) ? prev : messagingRows))
        // Hit the cap → at least one platform may have more on disk than loaded.
        setMessagingTruncated(result.messaging.sessions.length >= MESSAGING_SECTION_LIMIT)

        // Totals tell us which concrete profile catalogs are stale. A stable
        // total needs only the bounded batched refresh above; a changed total
        // is hydrated sequentially in fixed-size concrete-profile pages.
        const authoritativeTotals = new Map<string, number>()

        for (const [profile, total] of Object.entries(recents.profile_totals ?? {})) {
          if (typeof total === 'number' && Number.isFinite(total) && total >= 0 && Number.isInteger(total)) {
            authoritativeTotals.set(normalizeProfileKey(profile), total)
          }
        }

        const failedProfiles = new Set(
          (result.errors ?? []).map(error => (error.profile === 'all' ? 'all' : normalizeProfileKey(error.profile)))
        )

        for (const [profile, total] of authoritativeTotals) {
          if (refreshSessionsRequestRef.current !== requestId) {
            break
          }

          if (
            failedProfiles.has('all') ||
            failedProfiles.has(profile) ||
            hydratedProfileTotalsRef.current.get(profile) === total
          ) {
            continue
          }

          const hydratedRows: SessionInfo[] = []
          const seenIds = new Set<string>()
          let complete = true
          let offset = 0

          do {
            if (refreshSessionsRequestRef.current !== requestId) {
              complete = false

              break
            }

            let page

            try {
              page = await listAllProfileSessions(
                SIDEBAR_SESSIONS_INITIAL_LIMIT,
                1,
                'exclude',
                'recent',
                profile,
                { excludeSources: SIDEBAR_EXCLUDED_SOURCES, offset }
              )
            } catch {
              complete = false

              break
            }

            if (refreshSessionsRequestRef.current !== requestId || page.errors?.length) {
              complete = false

              break
            }

            const concreteTotal = page.profile_totals?.[profile] ?? page.total

            // Every concrete page must describe the same complete catalog as
            // the batched response that triggered this hydration. A missing,
            // synthetic, malformed, or drifted total makes the page additive
            // only: never reconcile cached rows or memoize this hydration.
            if (
              typeof concreteTotal !== 'number' ||
              !Number.isFinite(concreteTotal) ||
              concreteTotal < 0 ||
              !Number.isInteger(concreteTotal) ||
              concreteTotal !== total
            ) {
              complete = false

              break
            }

            const rows = page.sessions

            // An empty first page authoritatively confirms a zero-total
            // profile. Anywhere else it is a stalled pagination cursor.
            if (rows.length === 0) {
              if (total !== 0 || offset !== 0) {
                complete = false
              }

              break
            }

            // A concrete-profile request must never leak another owner's row;
            // reconciling that response could delete one profile and insert a
            // different one's sessions in its place.
            if (rows.some(session => normalizeProfileKey(session.profile) !== profile)) {
              complete = false

              break
            }

            for (const session of rows) {
              if (seenIds.has(session.id)) {
                complete = false

                break
              }

              seenIds.add(session.id)
              hydratedRows.push(session)
            }

            if (!complete || rows.length < SIDEBAR_SESSIONS_INITIAL_LIMIT || seenIds.size >= total) {
              break
            }

            offset += SIDEBAR_SESSIONS_INITIAL_LIMIT
          } while (offset <= total)

          // A cursor/page failure, total drift that left us short, or a stale
          // request can update nothing authoritatively. Keep the additive first
          // page and every cached row, and retry on a later refresh.
          if (!complete || seenIds.size !== total || refreshSessionsRequestRef.current !== requestId) {
            continue
          }

          const keep = sessionsToKeep(profile)

          setSessions(prev => {
            const inProfile = (session: SessionInfo) => normalizeProfileKey(session.profile) === profile
            const previousForProfile = prev.filter(inProfile)
            const reconciledProfile = mergeSessionPage(previousForProfile, hydratedRows, keep)
            const next = [...prev.filter(session => !inProfile(session)), ...reconciledProfile]

            return sameCronSignature(prev, next) ? prev : next
          })
          hydratedProfileTotalsRef.current.set(profile, total)
        }
      }
    } catch {
      // A failed bounded first-page read is non-destructive. Leave every cache
      // slice intact; the loading flag still settles in finally and a later
      // refresh can retry.
    } finally {
      if (showLoading && refreshSessionsRequestRef.current === requestId) {
        setSessionsLoading(false)
      }
    }

    // Cron *jobs* are a distinct API (getCronJobs), not a session slice.
    void refreshCronJobs()
  }, [profileScope, refreshCronJobs])

  const loadMoreSessions = useCallback(async () => {
    bumpSessionsLimit()
    await refreshSessions()
  }, [refreshSessions])

  // ALL-profiles view pages one profile at a time: fetch that profile's next
  // page and merge it in place, leaving every other profile's rows untouched.
  const loadMoreSessionsForProfile = useCallback(async (profile: string) => {
    const key = normalizeProfileKey(profile)
    const inKey = (s: SessionInfo) => normalizeProfileKey(s.profile) === key
    const loaded = $sessions.get().filter(inKey).length

    const result = await listAllProfileSessions(SIDEBAR_SESSIONS_INITIAL_LIMIT, 1, 'exclude', 'recent', key, {
      excludeSources: SIDEBAR_EXCLUDED_SOURCES,
      offset: loaded
    })

    setSessions(prev => {
      const previousForProfile = prev.filter(inKey)
      const keep = sessionsToKeep(key)

      for (const session of previousForProfile) {
        keep.add(session.id)
      }

      return [...prev.filter(s => !inKey(s)), ...mergeSessionPage(previousForProfile, result.sessions, keep)]
    })

    const total = result.profile_totals?.[key] ?? result.total ?? result.sessions.length
    setSessionProfileTotals(prev => ({ ...prev, [key]: Math.max(total, loaded + result.sessions.length) }))
  }, [])

  return {
    loadMoreMessagingForPlatform,
    loadMoreSessions,
    loadMoreSessionsForProfile,
    refreshCronJobs,
    refreshMessagingSessions,
    refreshSessions
  }
}

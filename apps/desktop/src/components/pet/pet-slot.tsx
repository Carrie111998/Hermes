import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { leaseProfileGateway, releaseProfileGateway } from '@/store/gateway'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { derivePetState, type PetInfo, type PetState } from '@/store/pet'
import { $profilePets, setProfilePetInfo } from '@/store/pet-multi'
import { petRequestFor } from '@/store/pet-transport'

import { backgroundPollLane, petPollInterval, staggerOffset } from './pet-poll'
import { PetSprite } from './pet-sprite'

interface PetSlotProps {
  profile: string
  /** Position in the enabled roster — staggers background polls so pinned
   *  profiles don't all hit their backend on the same tick. */
  index?: number
  /** Highest precedence; when absent the slot derives state from the profile's
   *  own (session-derived) activity slice. */
  stateOverride?: PetState
}

/** Cheap `pet.info.meta` payload — no spritesheet, just revision/signature. */
interface PetInfoMeta {
  enabled: boolean
  slug?: string
  displayName?: string
  scale?: number
  spritesheetRevision?: string
}

/** True when a loaded spritesheet already matches the meta signature, so a
 *  background poll can skip the full (spritesheet-bearing) `pet.info` fetch. */
function matchesMeta(info: PetInfo, meta: PetInfoMeta): boolean {
  return (
    info.enabled &&
    Boolean(info.spritesheetBase64) &&
    info.slug === meta.slug &&
    info.displayName === meta.displayName &&
    info.scale === meta.scale &&
    info.spritesheetRevision === meta.spritesheetRevision
  )
}

/**
 * One profile's in-window pet sprite — the multi-pet building block. No bubble
 * (bubbles are overlay-only). Leases the profile's gateway for the slot's
 * lifetime so its socket stays alive to stream activity into the profile's
 * slice, and polls the profile-addressed pet RPCs (connection ownership resolved
 * by Electron, never the active gateway) on the Layer-8 budget: meta-first for
 * background profiles, full `pet.info` only when the revision changes. Renders
 * nothing until the pet is enabled with a loaded spritesheet.
 */
export function PetSlot({ profile, index = 0, stateOverride }: PetSlotProps) {
  const pets = useStore($profilePets)
  const entry = pets.get(profile)
  const info = entry?.info ?? { enabled: false }
  const connection = entry?.connection ?? 'open'
  const offline = connection === 'offline' || connection === 'reauth-required'
  const active = normalizeProfileKey(profile) === normalizeProfileKey($activeGatewayProfile.get())
  const loaded = Boolean(info.spritesheetBase64)

  useEffect(() => {
    leaseProfileGateway(profile)

    return () => {
      releaseProfileGateway(profile)
    }
  }, [profile])

  // Meta-first polling on the Layer-8 budget. The cadence keys off the profile's
  // role (active vs background), load state, and connection; offline/reauth
  // profiles skip polling entirely (petPollInterval → null). Background profiles
  // poll the cheap `pet.info.meta` through the shared single lane and only fetch
  // the full spritesheet when the revision changes; the active profile polls the
  // full payload directly (never queued behind background work).
  useEffect(() => {
    if (offline) {
      return
    }

    const interval = petPollInterval({ active, blurred: false, loaded, offline })

    if (interval === null) {
      return
    }

    const request = petRequestFor(profile)
    let cancelled = false

    const pull = async () => {
      const isForeground = normalizeProfileKey(profile) === normalizeProfileKey($activeGatewayProfile.get())
      const run = async () => {
        // Meta-first for background profiles: skip the spritesheet payload when
        // the revision is unchanged.
        if (!isForeground) {
          try {
            const meta = await request<PetInfoMeta>('pet.info.meta', { profile })

            if (cancelled) {
              return
            }

            if (meta && !meta.enabled) {
              setProfilePetInfo(profile, { enabled: false })

              return
            }

            const current = $profilePets.get().get(profile)?.info

            if (meta && current && matchesMeta(current, meta)) {
              return
            }
          } catch {
            // Older gateways may lack pet.info.meta — fall through to pet.info.
          }
        }

        const next = await request<PetInfo>('pet.info', { profile })

        if (!cancelled && next) {
          setProfilePetInfo(profile, next)
        }
      }

      try {
        if (isForeground) {
          await backgroundPollLane.runForeground(run)
        } else {
          await backgroundPollLane.runBackground(run)
        }
      } catch {
        // Cosmetic feature — never surface gateway errors on the slot.
      }
    }

    void pull()
    // Stagger only the initial background poll; the steady interval is even.
    const startDelay = active ? 0 : staggerOffset(index)
    let pollTimer: ReturnType<typeof setInterval> | undefined
    const startTimer = setTimeout(() => {
      pollTimer = setInterval(() => void pull(), interval)
    }, startDelay)

    return () => {
      cancelled = true
      clearTimeout(startTimer)

      if (pollTimer) {
        clearInterval(pollTimer)
      }
    }
  }, [profile, active, loaded, offline, index])

  if (!info.enabled || !info.spritesheetBase64) {
    // An unloaded pet renders nothing until its spritesheet arrives.
    return null
  }

  const state = stateOverride ?? derivePetState(entry?.activity ?? {})

  const sprite = <PetSprite info={info} stateOverride={state} />

  if (!offline) {
    return sprite
  }

  // Layer 8 offline treatment: a dead/reauth profile desaturates its pet and
  // badges a disconnect glyph (with the reason on hover) instead of animating
  // idle forever — "backend down" must read as down, not happy.
  const reason = connection === 'reauth-required' ? 'Needs sign-in' : 'Backend unreachable'

  return (
    <span style={{ display: 'inline-block', position: 'relative' }} title={reason}>
      <span style={{ filter: 'grayscale(1) opacity(0.55)', display: 'inline-block' }}>{sprite}</span>
      <span
        aria-label={reason}
        style={{
          bottom: 6,
          color: connection === 'reauth-required' ? '#f59e0b' : '#9ca3af',
          fontSize: 12,
          lineHeight: 1,
          position: 'absolute',
          right: 2
        }}
      >
        {connection === 'reauth-required' ? '🔑' : '⏻'}
      </span>
    </span>
  )
}

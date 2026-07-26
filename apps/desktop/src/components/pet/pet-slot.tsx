import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { leaseProfileGateway, releaseProfileGateway, requestGatewayForProfile } from '@/store/gateway'
import { derivePetState, type PetInfo, type PetState } from '@/store/pet'
import { $profilePets, setProfilePetInfo } from '@/store/pet-multi'

import { PetSprite } from './pet-sprite'

interface PetSlotProps {
  profile: string
  /** Highest precedence; when absent the slot derives state from the profile's
   *  own (session-derived) activity slice. */
  stateOverride?: PetState
}

/**
 * One profile's in-window pet sprite — the multi-pet building block. No bubble
 * (bubbles are overlay-only). Leases the profile's gateway for the slot's
 * lifetime so its socket stays alive to stream activity into the profile's
 * slice, and loads the spritesheet through the profile-addressed `pet.info`
 * RPC (connection ownership resolved by Electron, never the active gateway).
 * Renders nothing until the pet is enabled with a loaded spritesheet.
 */
export function PetSlot({ profile, stateOverride }: PetSlotProps) {
  const pets = useStore($profilePets)
  const entry = pets.get(profile)
  const info = entry?.info ?? { enabled: false }

  useEffect(() => {
    leaseProfileGateway(profile)

    let cancelled = false
    void requestGatewayForProfile<PetInfo>(profile, 'pet.info', { profile })
      .then(next => {
        if (!cancelled && next) {
          setProfilePetInfo(profile, next)
        }
      })
      .catch(() => {
        // Cosmetic feature — never surface gateway errors on the slot.
      })

    return () => {
      cancelled = true
      releaseProfileGateway(profile)
    }
  }, [profile])

  if (!info.enabled || !info.spritesheetBase64) {
    // An unloaded pet renders nothing until its spritesheet arrives.
    return null
  }

  const state = stateOverride ?? derivePetState(entry?.activity ?? {})
  const connection = entry?.connection ?? 'open'
  const offline = connection === 'offline' || connection === 'reauth-required'

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

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
    // Layer 8 renders an offline/degraded treatment here when the profile's
    // connection is down; until then an unloaded pet simply renders nothing.
    return null
  }

  const state = stateOverride ?? derivePetState(entry?.activity ?? {})

  return <PetSprite info={info} stateOverride={state} />
}

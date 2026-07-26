import { useStore } from '@nanostores/react'

import { $observedPetProfiles, $petRoster } from '@/store/pet-roster'

import { PetSlot } from './pet-slot'

/**
 * In-window multi-pet row for `pinned` mode: one `PetSlot` per enabled,
 * available roster entry — 0, 1, or N. No roaming, no in-window bubbles
 * (sprites only). Each slot owns its own lease + `pet.info` lifecycle.
 *
 * Rendering is branched on MODE, not cardinality: a single pinned non-active
 * profile still renders through this container, never the legacy `FloatingPet`.
 */
export function MultiPetContainer() {
  const roster = useStore($petRoster)
  const observed = useStore($observedPetProfiles)

  // Roster order is the deterministic tie-breaker; the observed set (pinned
  // mode = enabled + available entries) gates membership.
  const profiles = roster.entries.filter(entry => observed.has(entry.profile)).map(entry => entry.profile)

  if (profiles.length === 0) {
    return null
  }

  return (
    <div
      style={{
        alignItems: 'flex-end',
        bottom: 24,
        display: 'flex',
        gap: 12,
        left: 24,
        pointerEvents: 'none',
        position: 'fixed',
        zIndex: 60
      }}
    >
      {profiles.map((profile, index) => (
        <PetSlot index={index} key={profile} profile={profile} />
      ))}
    </div>
  )
}

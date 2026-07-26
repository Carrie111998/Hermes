import { useEffect } from 'react'

import { $profiles, $profilesLoaded } from '@/store/profile'
import { reconcilePetRoster } from '@/store/pet-roster'

/**
 * Keep the pinned pet roster reconciled with the live profile catalog (Layer 7).
 * useEffect-owned (not a bare module-level subscribe) so it survives HMR without
 * duplicating lease releases, and returns a disposer on unmount. Gates on
 * `$profilesLoaded` so the initial empty catalog is never read as "every pinned
 * profile was deleted".
 */
export function usePetRosterReconciliation(): void {
  useEffect(() => {
    const reconcile = () => {
      if ($profilesLoaded.get()) {
        reconcilePetRoster($profiles.get())
      }
    }

    reconcile()

    const offProfiles = $profiles.listen(reconcile)
    const offLoaded = $profilesLoaded.listen(reconcile)

    return () => {
      offProfiles()
      offLoaded()
    }
  }, [])
}

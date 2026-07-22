import { useEffect, useRef } from 'react'

import { $activeGatewayProfile } from '@/store/profile'

/** Run `onSwitch` when the active gateway profile changes — never on first
 *  mount. For dropping per-profile view state (probes, cached usage, drafts)
 *  when the backend the app talks to swaps underneath a still-mounted view. */
export function useOnProfileSwitch(onSwitch: () => void): void {
  const onSwitchRef = useRef(onSwitch)
  onSwitchRef.current = onSwitch

  useEffect(() => {
    let currentProfile = $activeGatewayProfile.get()

    return $activeGatewayProfile.subscribe(profile => {
      if (profile === currentProfile) {
        return
      }

      currentProfile = profile
      onSwitchRef.current()
    })
  }, [])
}

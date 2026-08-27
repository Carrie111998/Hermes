import { useEffect } from 'react'
import type { ReactNode } from 'react'

import { loadMobileDisplayScale } from './mobile-display-scale'

/**
 * The one safe viewport child for the mobile app. Toolbar and drawer nodes live
 * inside it, so global viewport sizing applies once to the shell—not to each
 * independently rendered button.
 */
export function MobileRootShell({ children }: { children: ReactNode }) {
  useEffect(() => {
    void loadMobileDisplayScale()
  }, [])

  return <div data-mobile-root-shell="">{children}</div>
}

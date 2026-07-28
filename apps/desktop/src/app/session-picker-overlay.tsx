import { useStore } from '@nanostores/react'

import { SessionPickerDialog } from '@/components/session-picker'
import { $gatewayState, $selectedSessionIdentityKey, $sessionPickerOpen, setSessionPickerOpen } from '@/store/session'

interface SessionPickerOverlayProps {
  onResume: (storedSessionId: string, profile?: null | string) => void
}

/**
 * Mounts the session picker that `/resume` (and `/sessions`, `/switch`) opens —
 * the desktop equivalent of the TUI's sessions overlay. Resuming runs through
 * the same `resumeSession` path the sidebar uses.
 */
export function SessionPickerOverlay({ onResume }: SessionPickerOverlayProps) {
  const open = useStore($sessionPickerOpen)
  const gatewayOpen = useStore($gatewayState) === 'open'
  const activeSessionIdentityKey = useStore($selectedSessionIdentityKey)

  if (!gatewayOpen) {
    return null
  }

  return (
    <SessionPickerDialog
      activeSessionIdentityKey={activeSessionIdentityKey}
      onOpenChange={setSessionPickerOpen}
      onResume={onResume}
      open={open}
    />
  )
}

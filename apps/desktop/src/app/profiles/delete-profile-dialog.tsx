import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { deleteProfile } from '@/hermes'
import { useI18n } from '@/i18n'
import { retireAgentGateways } from '@/store/gateway'
import { $attachedConnectionId, $primaryConnectionId, LOCAL_CONNECTION_ID } from '@/store/gateway-separation'
import { $activeGatewayProfile, normalizeProfileKey, selectProfile, setActiveProfile } from '@/store/profile'

// Thin wrapper over ConfirmDialog: owns the deleteProfile call, inherits
// Enter-to-confirm + busy/done/error from the shared dialog. The single choke
// point for every delete entry point (rail + Profiles view).
export function DeleteProfileDialog({
  connectionId,
  profile,
  onClose,
  onDeleted,
  open
}: {
  /** The machine that owns this profile.
   *  Manage Profiles lists every registered gateway's profiles, so an action
   *  has to run on the box the row came from. Omitted / '' = the primary, so
   *  single-gateway callers are unchanged. */
  connectionId?: null | string
  profile: { name: string; path: string } | null
  onClose: () => void
  onDeleted?: () => Promise<void> | void
  open: boolean
}) {
  const { t } = useI18n()
  const p = t.profiles

  return (
    <ConfirmDialog
      busyLabel={p.deleting}
      confirmLabel={t.common.delete}
      description={
        profile ? (
          <>
            {p.deleteDescPrefix}
            <span className="font-medium text-foreground">{profile.name}</span>
            {p.deleteDescMid}
            <span className="font-mono text-xs">{profile.path}</span>
            {p.deleteDescSuffix}
          </>
        ) : null
      }
      destructive
      doneLabel={p.deleted}
      onClose={onClose}
      onConfirm={async () => {
        if (!profile) {
          return
        }

        // Deleting the profile the live gateway is on strands it on a dead
        // backend. Capture that before the delete; reset *after* the host's
        // onDeleted refresh so our reset is the last write — a refreshActiveProfile
        // racing the (still-dying) backend can't clobber the pill back to it.
        // "Was the deleted profile the live one?" is a question about an
        // AGENT, not a name. Every registered machine serves a `default`, so a
        // name-only comparison said yes while the live gateway sat on a
        // different box entirely — and the reset below then swapped that
        // innocent machine's gateway and pill to `default`.
        const owner = (connectionId ?? '').trim() || $primaryConnectionId.get() || LOCAL_CONNECTION_ID

        const wasActive =
          normalizeProfileKey(profile.name) === normalizeProfileKey($activeGatewayProfile.get()) &&
          owner === $attachedConnectionId.get()

        // Retire the sockets THIS agent owns. The local-only seam would have
        // torn down a same-named local profile instead (#88638).
        retireAgentGateways(connectionId ?? null, profile.name)
        // Pass the owning machine ONLY when there is one, so a
        // single-gateway install issues the byte-identical upstream call.
        const scope: [] | [string] = connectionId ? [connectionId] : []
        await deleteProfile(profile.name, ...scope)
        await onDeleted?.()

        if (wasActive) {
          // Swap gateway/sidebar to default and set the pill now — the primary
          // backend is always default, so this is correct, not just optimistic.
          selectProfile('default')
          setActiveProfile('default')
        }
      }}
      open={open}
      title={p.deleteTitle}
    />
  )
}

import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { useI18n } from '@/i18n'
import {
  $bulkSessionActions,
  $selectedSessionIds,
  $selectionModeActive,
  clearSessionSelection
} from '@/store/session-selection'

// The sidebar's compact bar for the explicit multi-select mode (entered via a
// row's "Select chats" menu item). Always mounted at a fixed spot in the
// sidebar; it paints nothing until selection mode is active, which keeps the
// mount point stable rather than conditional in the parent.
export function SidebarSelectionActionBar() {
  const { t } = useI18n()
  const s = t.sidebar.selection
  const active = useStore($selectionModeActive)
  const selectedIds = useStore($selectedSessionIds)
  const bulkActions = useStore($bulkSessionActions)
  const [deleteOpen, setDeleteOpen] = useState(false)

  if (!active) {
    return null
  }

  const count = selectedIds.length
  const disabled = !bulkActions || count === 0

  return (
    // data-session-selection-bar: the sidebar's click-out-to-cancel handler
    // excludes this element, so clicking Archive/Delete/Cancel never also
    // fires the click-out that would cancel the very action being clicked.
    <div className="flex shrink-0 items-center gap-2 px-2 py-1.5" data-session-selection-bar="">
      <span className="min-w-0 flex-1 truncate text-xs text-(--ui-text-secondary)">{s.count(count)}</span>
      <Button
        disabled={disabled}
        onClick={() => bulkActions?.archive(selectedIds)}
        size="xs"
        type="button"
        variant="secondary"
      >
        {s.archive}
      </Button>
      <Button disabled={disabled} onClick={() => setDeleteOpen(true)} size="xs" type="button" variant="destructive">
        {s.delete}
      </Button>
      <Button onClick={clearSessionSelection} size="xs" type="button" variant="text">
        {s.cancel}
      </Button>
      <ConfirmDialog
        confirmLabel={s.delete}
        description={s.deleteDesc(count)}
        destructive
        onClose={() => setDeleteOpen(false)}
        onConfirm={() => bulkActions?.remove(selectedIds)}
        open={deleteOpen}
        title={s.deleteTitle(count)}
      />
    </div>
  )
}

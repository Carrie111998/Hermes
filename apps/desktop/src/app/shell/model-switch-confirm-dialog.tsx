import { useStore } from '@nanostores/react'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { useI18n } from '@/i18n'
import { $pendingModelSwitchConfirm, setPendingModelSwitchConfirm } from '@/store/model-switch-confirm'
import { useModelControlsContext } from './use-model-controls-context'

/**
 * Selection-guard confirmation for composer/picker model switches.
 *
 * When the gateway bounces a pick with `confirm_required` (expensive-model or
 * data-training-tier guard, e.g. Meta's contributor tier), the pending switch
 * parks in `$pendingModelSwitchConfirm` and this dialog — mounted once at the
 * shell root, like the other global confirms — renders the backend's warning
 * verbatim. Confirm resends the identical config.set value flagged with
 * `confirm_expensive_model`; cancel clears the pending state and the composer
 * keeps its previous selection.
 */
export function ModelSwitchConfirmDialog() {
  const { t } = useI18n()
  const copy = t.modelSwitchConfirm
  const pending = useStore($pendingModelSwitchConfirm)
  const controls = useModelControlsContext()

  const close = () => setPendingModelSwitchConfirm(null)

  return (
    <ConfirmDialog
      confirmLabel={copy.confirm}
      description={pending?.message}
      dismissOnConfirm
      onClose={close}
      onConfirm={async () => {
        if (!pending || !controls) {
          return
        }

        const ok = await controls.selectModel({
          model: pending.model,
          provider: pending.provider,
          sessionId: pending.sessionId,
          confirmExpensiveModel: true
        })

        // The retry went through — clear the parked warning. A failure inside
        // selectModel already surfaced via notifyError; keep the dialog's own
        // path quiet by throwing only when the switch reports failure without
        // an error (defensive: selectModel returns false on rollback).
        if (!ok) {
          throw new Error(copy.failed)
        }

        setPendingModelSwitchConfirm(null)
      }}
      open={pending !== null}
      title={copy.title}
    />
  )
}

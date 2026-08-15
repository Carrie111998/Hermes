import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { BackendTargetPickerDialog } from '@/components/backend-target-picker'
import { useI18n } from '@/i18n'
import {
  $windowBackendPickerOpen,
  closeWindowBackendPicker
} from '@/store/window-backend-picker'
import { listWindowBackendTargets, openNewWindow } from '@/store/windows'

export function WindowBackendPickerOverlay() {
  const open = useStore($windowBackendPickerOpen)
  const { t } = useI18n()
  const [choices, setChoices] = useState<WindowBackendTargetChoice[]>([])
  const [loading, setLoading] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)

  useEffect(() => {
    if (!open) {
      setChoices([])
      setLoading(false)
      setLoadFailed(false)

      return
    }

    let cancelled = false
    setChoices([])
    setLoading(true)
    setLoadFailed(false)

    void listWindowBackendTargets()
      .then(nextChoices => {
        if (!cancelled) {
          setChoices(nextChoices)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setLoadFailed(true)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [open])

  const currentChoiceId = choices.find(choice => choice.current)?.id ?? null

  const handleSelect = (choiceId: string) => {
    void openNewWindow(choiceId).then(ok => {
      if (ok) {
        closeWindowBackendPicker()
      }
    })
  }

  return (
    <BackendTargetPickerDialog
      choices={choices}
      copy={{
        title: t.windowBackend.title,
        description: t.windowBackend.description,
        searchPlaceholder: t.windowBackend.search,
        currentBadge: t.windowBackend.current,
        emptyLabel: loading
          ? t.windowBackend.loading
          : loadFailed
            ? t.windowBackend.loadFailed
            : t.windowBackend.empty,
        noMatchLabel: t.windowBackend.noMatch,
        cancelLabel: t.common.cancel
      }}
      currentChoiceId={currentChoiceId}
      onOpenChange={nextOpen => {
        if (!nextOpen) {
          closeWindowBackendPicker()
        }
      }}
      onSelect={handleSelect}
      open={open}
    />
  )
}

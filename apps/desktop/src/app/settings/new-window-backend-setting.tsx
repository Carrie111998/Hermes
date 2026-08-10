import { useStore } from '@nanostores/react'

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { cn } from '@/lib/utils'
import {
  $newWindowBackendPolicy,
  type NewWindowBackendPolicy,
  setNewWindowBackendPolicy
} from '@/store/window-backend-picker'

import { CONTROL_TEXT } from './constants'
import { ListRow } from './primitives'

export function NewWindowBackendSetting() {
  const { t } = useI18n()
  const policy = useStore($newWindowBackendPolicy)
  const copy = t.windowBackend.newWindowPolicy

  return (
    <ListRow
      action={
        <Select
          onValueChange={value => {
            setNewWindowBackendPolicy(value as NewWindowBackendPolicy)
            triggerHaptic('selection')
          }}
          value={policy}
        >
          <SelectTrigger aria-label={copy.title} className={cn('min-w-56', CONTROL_TEXT)}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="inherit">{copy.inherit}</SelectItem>
            <SelectItem value="primary">{copy.primary}</SelectItem>
            <SelectItem value="ask">{copy.ask}</SelectItem>
          </SelectContent>
        </Select>
      }
      description={copy.description}
      title={copy.title}
    />
  )
}

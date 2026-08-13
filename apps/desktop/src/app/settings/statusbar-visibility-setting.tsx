import { useStore } from '@nanostores/react'

import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { $statusbarVisible } from '@/store/statusbar-prefs'
import { isAuxiliaryWindow } from '@/store/windows'

import { ListRow } from './primitives'

export function StatusbarVisibilitySetting() {
  const { t } = useI18n()
  const visible = useStore($statusbarVisible)
  const copy = t.settings.appearance

  if (isAuxiliaryWindow()) {
    return null
  }

  return (
    <ListRow
      action={
        <SegmentedControl
          onChange={id => {
            triggerHaptic('selection')
            $statusbarVisible.set(id === 'on')
          }}
          options={[
            { id: 'off', label: t.common.off },
            { id: 'on', label: t.common.on }
          ]}
          value={visible ? 'on' : 'off'}
        />
      }
      description={copy.statusBarDesc}
      title={copy.statusBarTitle}
    />
  )
}

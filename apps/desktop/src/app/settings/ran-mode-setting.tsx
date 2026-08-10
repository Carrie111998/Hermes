import { useStore } from '@nanostores/react'

import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { $ranModeEnabled, disableRanMode, enableRanMode } from '@/store/ran-mode'
import { isAuxiliaryWindow } from '@/store/windows'

import { ListRow } from './primitives'

export function RanModeSetting() {
  const { t } = useI18n()
  const enabled = useStore($ranModeEnabled)

  if (isAuxiliaryWindow()) {
    return null
  }

  return (
    <ListRow
      action={
        <div data-testid="ran-mode-toggle">
          <SegmentedControl
            onChange={id => {
              triggerHaptic('selection')

              if (id === 'on') {
                enableRanMode()
              } else {
                disableRanMode()
              }
            }}
            options={[
              { id: 'off', label: t.common.off },
              { id: 'on', label: t.common.on }
            ]}
            value={enabled ? 'on' : 'off'}
          />
        </div>
      }
      description={t.settings.appearance.ranModeDesc}
      title={t.settings.appearance.ranModeTitle}
    />
  )
}

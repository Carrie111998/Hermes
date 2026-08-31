import * as React from 'react'

import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { dateSectionLabel, type SessionDateBucket } from './session-date-sections'

interface SidebarDateSectionHeaderProps extends React.ComponentProps<'div'> {
  bucket: SessionDateBucket
}

// A quiet in-list date divider (Today / Yesterday / …), deliberately softer
// than the section header above it: lowercase-tertiary rather than the themed
// uppercase SidebarPanelLabel, so date rhythm never competes with the SESSIONS
// label itself.
export const SidebarDateSectionHeader = React.forwardRef<HTMLDivElement, SidebarDateSectionHeaderProps>(
  ({ bucket, className, ...props }, ref) => {
    const { t } = useI18n()

    return (
      <div
        className={cn(
          'select-none px-2 pb-1 pt-2.5 text-[0.6875rem] font-medium leading-none text-(--ui-text-tertiary) first:pt-1',
          className
        )}
        ref={ref}
        {...props}
      >
        {dateSectionLabel(bucket, t.sidebar.dateSections)}
      </div>
    )
  }
)

SidebarDateSectionHeader.displayName = 'SidebarDateSectionHeader'

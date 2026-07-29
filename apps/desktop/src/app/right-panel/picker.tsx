import { useStore } from '@nanostores/react'
import type { ReactNode } from 'react'

import { $artifactsPaneOpen, openArtifactsPane } from '@/app/artifacts/pane-state'
import { $terminalTakeover, setTerminalTakeover } from '@/app/right-sidebar/store'
import { findGroupOfPane } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { cn } from '@/lib/utils'
import { $fileBrowserOpen, setFileBrowserOpen } from '@/store/layout'
import { $paneOpen } from '@/store/panes'
import { $previewTabs, showPreviewPane } from '@/store/preview'
import { $reviewOpen, openReview } from '@/store/review'
import { $rightPanelOpen, type RightPanelPaneId, setRightPanelOpen } from '@/store/right-panel'
import { $currentCwd } from '@/store/session'

interface RightPanelPickerProps {
  className?: string
  compact?: boolean
  trigger?: ReactNode
}

interface PickerItem {
  disabled?: boolean
  icon: string
  label: string
  open: boolean
  paneId: RightPanelPaneId
  show: () => void
}

export function RightPanelPicker({ className, compact = false, trigger }: RightPanelPickerProps) {
  const { t } = useI18n()
  const triggerLabel = compact ? t.rightSidebar.addPanel : t.rightSidebar.choosePanel
  const cwd = useStore($currentCwd)
  const filesOpen = useStore($fileBrowserOpen)
  const reviewOpen = useStore($reviewOpen)
  const artifactsOpen = useStore($artifactsPaneOpen)
  const previewOpen = useStore($paneOpen('preview'))
  const previewTabs = useStore($previewTabs)
  const terminalOpen = useStore($terminalTakeover)
  const panelOpen = useStore($rightPanelOpen)
  const hasWorkspace = Boolean(cwd.trim())

  const items: PickerItem[] = [
    {
      disabled: !hasWorkspace,
      icon: 'git-compare',
      label: t.rightSidebar.review,
      open: reviewOpen,
      paneId: 'review',
      show: openReview
    },
    {
      icon: 'archive',
      label: t.rightSidebar.artifacts,
      open: artifactsOpen,
      paneId: 'artifacts-pane',
      show: openArtifactsPane
    },
    {
      disabled: previewTabs.length === 0,
      icon: 'preview',
      label: t.rightSidebar.preview,
      open: previewOpen && previewTabs.length > 0,
      paneId: 'preview',
      show: showPreviewPane
    },
    {
      icon: 'terminal',
      label: t.rightSidebar.terminal,
      open: terminalOpen,
      paneId: 'terminal',
      show: () => setTerminalTakeover(true)
    }
  ]

  const choose = (item: PickerItem) => {
    item.show()
    const tree = $layoutTree.get()

    noteActiveTreeGroup(tree ? (findGroupOfPane(tree, item.paneId)?.id ?? null) : null)
    triggerHaptic('open')
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {trigger ?? (
          <Button
            aria-label={triggerLabel}
            className={cn('size-7 rounded-none text-(--ui-text-tertiary)', className)}
            size="icon"
            title={triggerLabel}
            type="button"
            variant="ghost"
          >
            <Codicon name="add" size="0.8125rem" />
          </Button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48" sideOffset={compact ? 2 : 6}>
        <DropdownMenuLabel>{t.rightSidebar.choosePanel}</DropdownMenuLabel>
        <DropdownMenuCheckboxItem
          checked={filesOpen}
          disabled={!hasWorkspace}
          onCheckedChange={checked => setFileBrowserOpen(checked === true)}
          // This is a visibility preference, not a one-shot command. Keep the
          // picker open so the checkmark visibly follows the user's choice.
          onSelect={event => event.preventDefault()}
        >
          <Codicon name="files" />
          <span className="min-w-0 flex-1 truncate">{t.rightSidebar.files}</span>
        </DropdownMenuCheckboxItem>
        <DropdownMenuSeparator />
        {items.map(item => (
          <DropdownMenuItem disabled={item.disabled} key={item.paneId} onSelect={() => choose(item)}>
            <Codicon name={item.icon} />
            <span className="min-w-0 flex-1 truncate">{item.label}</span>
            {item.open && <Codicon className="text-(--ui-accent)" name="check" size="0.75rem" />}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() => {
            setRightPanelOpen(!panelOpen)
            triggerHaptic('tap')
          }}
        >
          <Codicon name="layout-sidebar-right" />
          {panelOpen ? t.rightSidebar.hidePanel : t.rightSidebar.showPanel}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

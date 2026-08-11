import type { CSSProperties } from 'react'

import { getBaseColors } from '@/themes/context'

type UsageDarkStyle = CSSProperties & Record<`--${string}`, string>

export function usageDarkStyle(themeName: string): UsageDarkStyle {
  const colors = getBaseColors(themeName, 'dark')
  const accent = colors.midground ?? colors.primary
  const sidebar = colors.sidebarBackground ?? colors.background
  const sidebarBorder = colors.sidebarBorder ?? colors.border
  const userBubble = colors.userBubble ?? colors.card

  return {
    '--background': colors.background,
    '--chrome-action-hover': colors.accent,
    '--dt-accent': colors.accent,
    '--dt-accent-foreground': colors.accentForeground,
    '--dt-background': colors.background,
    '--dt-border': colors.border,
    '--dt-card': colors.card,
    '--dt-card-foreground': colors.cardForeground,
    '--dt-destructive': colors.destructive,
    '--dt-destructive-foreground': colors.destructiveForeground,
    '--dt-foreground': colors.foreground,
    '--dt-input': colors.input,
    '--dt-midground': accent,
    '--dt-muted': colors.muted,
    '--dt-muted-foreground': colors.mutedForeground,
    '--dt-popover': colors.popover,
    '--dt-popover-foreground': colors.popoverForeground,
    '--dt-primary': colors.primary,
    '--dt-primary-foreground': colors.primaryForeground,
    '--dt-ring': colors.ring,
    '--dt-secondary': colors.secondary,
    '--dt-secondary-foreground': colors.secondaryForeground,
    '--foreground': colors.foreground,
    '--sidebar': sidebar,
    '--sidebar-border': sidebarBorder,
    '--sidebar-foreground': colors.foreground,
    '--theme-accent-soft': colors.accent,
    '--theme-background-seed': colors.background,
    '--theme-bubble-seed': userBubble,
    '--theme-card-seed': colors.card,
    '--theme-elevated-seed': colors.popover,
    '--theme-foreground': colors.foreground,
    '--theme-midground': accent,
    '--theme-neutral-card': colors.card,
    '--theme-neutral-chrome': colors.background,
    '--theme-neutral-sidebar': sidebar,
    '--theme-primary': colors.primary,
    '--theme-secondary': colors.secondary,
    '--ui-accent': accent,
    '--ui-accent-secondary': colors.primary,
    '--ui-base': colors.foreground,
    '--ui-bg-card': colors.card,
    '--ui-bg-chrome': colors.background,
    '--ui-bg-editor': colors.card,
    '--ui-bg-elevated': colors.popover,
    '--ui-bg-input': colors.card,
    '--ui-bg-primary': colors.accent,
    '--ui-bg-quaternary': colors.muted,
    '--ui-bg-quinary': colors.muted,
    '--ui-bg-secondary': colors.secondary,
    '--ui-bg-sidebar': sidebar,
    '--ui-bg-tertiary': colors.muted,
    '--ui-chat-surface-background': colors.background,
    '--ui-control-active-background': colors.secondary,
    '--ui-control-hover-background': colors.accent,
    '--ui-editor-surface-background': colors.background,
    '--ui-row-active-background': colors.secondary,
    '--ui-row-hover-background': colors.accent,
    '--ui-stroke-primary': colors.input,
    '--ui-stroke-quaternary': colors.border,
    '--ui-stroke-secondary': colors.border,
    '--ui-stroke-tertiary': colors.border,
    '--ui-surface-background': colors.card,
    '--ui-text-primary': colors.foreground,
    '--ui-text-quaternary': colors.mutedForeground,
    '--ui-text-secondary': colors.secondaryForeground,
    '--ui-text-tertiary': colors.mutedForeground,
    background: colors.background,
    color: colors.foreground,
    colorScheme: 'dark'
  }
}

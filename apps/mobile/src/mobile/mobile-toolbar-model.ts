export interface MobileToolbarContextCandidate {
  disabled?: boolean
  hidden?: boolean
  href?: string
  id: string
  /** Desktop contributions carry a React icon; mobile text rows do not render it. */
  icon?: unknown
  label: string
  onSelect?: () => unknown
  to?: string
}

export interface MobileToolbarContextAction {
  disabled?: boolean
  href?: string
  id: string
  label: string
  onSelect?: () => unknown
  to?: string
}

/**
 * The overflow sheet is the complete mobile home for contextual Desktop
 * titlebar tools. Preserve every executable route/callback/link, omit only
 * intentionally hidden tools, and deduplicate contribution overlap by id.
 */
export function mobileToolbarContextActions(
  tools: readonly MobileToolbarContextCandidate[]
): MobileToolbarContextAction[] {
  const seen = new Set<string>()

  return tools.flatMap(tool => {
    if (tool.hidden || seen.has(tool.id)) return []
    seen.add(tool.id)

    return [
      {
        disabled: tool.disabled,
        href: tool.href,
        id: tool.id,
        label: tool.label,
        onSelect: tool.onSelect,
        to: tool.to
      }
    ]
  })
}

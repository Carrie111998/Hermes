interface SidebarNavLabelInput {
  fallback: string
  id: string
  sidebarNav: Record<string, string>
  usageLabel: string
}

export function resolveSidebarNavLabel({
  fallback,
  id,
  sidebarNav,
  usageLabel
}: SidebarNavLabelInput): string {
  return sidebarNav[id] ?? (id === 'usage' ? usageLabel : fallback)
}

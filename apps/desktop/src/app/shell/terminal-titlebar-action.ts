interface TerminalTitlebarActionDeps {
  create: () => unknown
  isVisible: () => boolean
  toggle: () => unknown
}

let openedFreshTerminal = false

export function toggleTerminalFromTitlebar({ create, isVisible, toggle }: TerminalTitlebarActionDeps): void {
  if (!isVisible() && !openedFreshTerminal) {
    create()
    openedFreshTerminal = true
  }

  toggle()
}

export function resetTerminalTitlebarActionForTests(): void {
  openedFreshTerminal = false
}

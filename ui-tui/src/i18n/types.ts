// TUI i18n type contract — simplified copy of apps/desktop/src/i18n/types.ts
// Covers ~25 keys that are actually visible in the terminal UI.

export type Locale = 'en' | 'ru'

export interface Translations {
  common: {
    loading: string
    error: string
    retry: string
    cancel: string
    close: string
    save: string
    copy: string
    copied: string
    noSessions: string
    pressEnter: string
  }
  session: {
    availableTools: string
    availableSkills: string
    systemPrompt: string
    mcpServers: string
    noSystemPrompt: string
    scanningSkills: string
    sessionLabel: string
    helpHint: string
    toolsCount: (count: number) => string
    skillsCount: (count: number) => string
  }
  composer: {
    placeholder: string
    interruptHint: string
    backgroundTasks: (count: number) => string
    queued: (text: string) => string
  }
  help: {
    quickHelp: string
    commonCommands: string
    hotkeys: string
    fullHelpHint: string
    dismissHint: string
  }
  status: {
    loadingSessions: string
    noOtherSessions: string
    moreAbove: (count: number) => string
    moreBelow: (count: number) => string
    selectNewHint: string
  }
}

import type { Translations } from './types.js'

export const en: Translations = {
  common: {
    loading: 'Loading…',
    error: 'Error',
    retry: 'Retry',
    cancel: 'Cancel',
    close: 'Close',
    save: 'Save',
    copy: 'Copy',
    copied: 'Copied',
    noSessions: 'No sessions',
    pressEnter: 'Press Enter to send',
  },
  session: {
    availableTools: 'Available Tools',
    availableSkills: 'Available Skills',
    systemPrompt: 'System Prompt',
    mcpServers: 'MCP Servers',
    noSystemPrompt: 'No system prompt loaded.',
    scanningSkills: 'scanning skills',
    sessionLabel: 'Session:',
    helpHint: '/help for commands',
    toolsCount: count => `${count} tool${count === 1 ? '' : 's'}`,
    skillsCount: count => `${count} skill${count === 1 ? '' : 's'}`,
  },
  composer: {
    placeholder: 'Ask me anything…',
    interruptHint: 'Ctrl+C to interrupt…',
    backgroundTasks: count => `${count} background ${count === 1 ? 'task' : 'tasks'} running`,
    queued: text => `queued: "${text}"`,
  },
  help: {
    quickHelp: '? quick help',
    commonCommands: 'Common commands',
    hotkeys: 'Hotkeys',
    fullHelpHint: 'type /help for the full panel',
    dismissHint: 'backspace to dismiss',
  },
  status: {
    loadingSessions: 'loading sessions…',
    noOtherSessions: 'no other sessions — Enter on +new to start one',
    moreAbove: count => `↑ ${count} more`,
    moreBelow: count => `↓ ${count} more`,
    selectNewHint: 'Select +new to type a prompt',
  },
}

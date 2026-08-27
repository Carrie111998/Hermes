import { en } from './en.js'
import type { Translations } from './types.js'

// Simple deep merge — mirrors apps/desktop/src/i18n/define-locale.ts
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function merge<T>(base: T, overrides: Record<string, unknown>): T {
  const result: Record<string, unknown> = { ...(base as Record<string, unknown>) }
  for (const [k, v] of Object.entries(overrides)) {
    if (v === undefined) continue
    const bv = result[k]
    result[k] = isRecord(bv) && isRecord(v) ? merge(bv as Record<string, unknown>, v as Record<string, unknown>) : v
  }
  return result as T
}

type Overrides = {
  [K in keyof Translations]?: {
    [P in keyof Translations[K]]?: Translations[K][P]
  }
}

function defineLocale(overrides: Overrides): Translations {
  return merge<Translations>(en, overrides as Record<string, unknown>)
}

export const ru = defineLocale({
  common: {
    loading: 'Загрузка…',
    error: 'Ошибка',
    retry: 'Повторить',
    cancel: 'Отмена',
    close: 'Закрыть',
    save: 'Сохранить',
    copy: 'Копировать',
    copied: 'Скопировано',
    noSessions: 'Нет сессий',
    pressEnter: 'Нажмите Enter для отправки',
  },
  session: {
    availableTools: 'Доступные инструменты',
    availableSkills: 'Доступные навыки',
    systemPrompt: 'Системный промпт',
    mcpServers: 'MCP-серверы',
    noSystemPrompt: 'Системный промпт не загружен.',
    scanningSkills: 'сканирование навыков',
    sessionLabel: 'Сессия:',
    helpHint: '/help — команды',
    toolsCount: count => `${count} инструмент${count === 1 ? '' : count < 5 ? 'а' : 'ов'}`,
    skillsCount: count => `${count} навык${count === 1 ? '' : count < 5 ? 'а' : 'ов'}`,
  },
  composer: {
    placeholder: 'Спросите что угодно…',
    interruptHint: 'Ctrl+C для прерывания…',
    backgroundTasks: count => `${count} фоновых задач выполняется`,
    queued: text => `в очереди: "${text}"`,
  },
  help: {
    quickHelp: '? быстрая помощь',
    commonCommands: 'Частые команды',
    hotkeys: 'Горячие клавиши',
    fullHelpHint: 'наберите /help для всех команд',
    dismissHint: 'backspace — закрыть',
  },
  status: {
    loadingSessions: 'загрузка сессий…',
    noOtherSessions: 'нет других сессий — Enter на +new чтобы начать',
    moreAbove: count => `↑ ещё ${count}`,
    moreBelow: count => `↓ ещё ${count}`,
    selectNewHint: 'Выберите +new чтобы ввести запрос',
  },
})

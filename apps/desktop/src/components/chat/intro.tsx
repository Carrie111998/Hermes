import { useState } from 'react'

import { type Locale, useI18n } from '@/i18n'
import { capitalize, normalize } from '@/lib/text'

import introCopyJsonl from './intro-copy.jsonl?raw'
import { Wordmark } from './wordmark'

type IntroCopy = {
  headline: string
  body: string
}

type IntroCopyRecord = IntroCopy & {
  personality: string
}

export type IntroProps = {
  personality?: string
  seed?: number
}

const NEUTRAL_PERSONALITIES = new Set(['', 'default', 'none', 'neutral'])

const FALLBACK_COPY: IntroCopy[] = [
  {
    headline: 'What are we moving today?',
    body: "Send a bug, branch, plan, or rough idea. I'll inspect the repo and turn it into the next concrete step."
  },
  {
    headline: "What's on your mind?",
    body: "Bring the code, question, or stuck part. I'll read the room before making changes."
  },
  {
    headline: 'What should Hermes look at?',
    body: "Send the task, failing path, or half-formed plan. I'll help turn it into action."
  },
  {
    headline: 'Where should we start?',
    body: "Bring the problem, goal, or file. I'll inspect first and keep the next step concrete."
  },
  {
    headline: 'What needs attention?',
    body: "Send the context you have. I'll help sort it into a plan or a fix."
  }
]

const PT_BR_NEUTRAL_COPY: IntroCopy[] = [
  {
    headline: 'O que vamos construir hoje?',
    body: 'Descreva a tarefa com suas palavras. Vou escolher as ferramentas certas, explicar o plano e confirmar antes de qualquer etapa arriscada.'
  },
  {
    headline: 'Comece por qualquer lugar.',
    body: 'Envie o caminho de um arquivo, um erro ou uma ideia inicial. Vou investigar, sugerir os próximos passos e manter tudo reversível.'
  },
  {
    headline: 'Seu espaço de trabalho, a uma mensagem de distância.',
    body: 'Buscar no projeto, editar arquivos, rodar testes ou preparar uma revisão: diga o objetivo e eu cuido da parte mecânica.'
  },
  {
    headline: 'Pronto quando você estiver.',
    body: 'Digite uma tarefa, pergunta ou trecho. Eu lembro da sessão, cito minhas fontes e paro para perguntar quando não tenho certeza.'
  }
]

function normalizeKey(value?: string): string {
  return normalize(value)
}

function titleize(value: string): string {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(capitalize)
    .join(' ')
}

function isIntroCopyRecord(value: unknown): value is IntroCopyRecord {
  if (!value || typeof value !== 'object') {
    return false
  }

  const record = value as Record<string, unknown>

  return (
    typeof record.personality === 'string' &&
    typeof record.headline === 'string' &&
    typeof record.body === 'string' &&
    Boolean(record.personality.trim()) &&
    Boolean(record.headline.trim()) &&
    Boolean(record.body.trim())
  )
}

function parseIntroCopy(raw: string): Record<string, IntroCopy[]> {
  const byPersonality: Record<string, IntroCopy[]> = {}

  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim()

    if (!trimmed) {
      continue
    }

    try {
      const parsed: unknown = JSON.parse(trimmed)

      if (!isIntroCopyRecord(parsed)) {
        continue
      }

      const key = normalizeKey(parsed.personality)
      byPersonality[key] ??= []
      byPersonality[key].push({
        headline: parsed.headline.trim(),
        body: parsed.body.trim()
      })
    } catch {
      // Bad generated copy should not break the whole desktop app.
    }
  }

  return byPersonality
}

const INTRO_COPY_BY_PERSONALITY = parseIntroCopy(introCopyJsonl)

function neutralCopy(): IntroCopy[] {
  return INTRO_COPY_BY_PERSONALITY.none || INTRO_COPY_BY_PERSONALITY.default || FALLBACK_COPY
}

function fallbackCopyForPersonality(personalityKey: string): IntroCopy[] {
  if (NEUTRAL_PERSONALITIES.has(personalityKey)) {
    return neutralCopy()
  }

  const label = titleize(personalityKey)

  return [
    {
      headline: `${label} mode is on. What should we work on?`,
      body: "Send the task, file, or rough idea. I'll use your configured voice and keep the work grounded in this repo."
    },
    {
      headline: `What does ${label} Hermes need to see?`,
      body: "Bring the context or the stuck part. I'll adapt to your configured personality."
    },
    {
      headline: `${label} mode is ready.`,
      body: "Send the problem, file, or idea. I'll follow the personality you've configured."
    },
    {
      headline: `What should ${label} Hermes tackle?`,
      body: "Drop the task here. I'll keep the work grounded in the repo."
    },
    {
      headline: 'Where should we begin?',
      body: `Give me the context and I'll answer in ${label} mode.`
    }
  ]
}

function ptBrFallbackCopyForPersonality(personalityKey: string): IntroCopy[] {
  const label = titleize(personalityKey)

  return [
    {
      headline: `O modo ${label} está ativo. Em que vamos trabalhar?`,
      body: 'Envie a tarefa, o arquivo ou uma ideia inicial. Vou usar a personalidade configurada e manter o trabalho baseado neste projeto.'
    },
    {
      headline: `O que o Hermes no modo ${label} precisa ver?`,
      body: 'Traga o contexto ou a parte travada. Vou me adaptar à personalidade configurada.'
    },
    {
      headline: `O modo ${label} está pronto.`,
      body: 'Envie o problema, arquivo ou ideia. Vou seguir a personalidade que você configurou.'
    },
    {
      headline: `O que o Hermes no modo ${label} deve resolver?`,
      body: 'Coloque a tarefa aqui. Vou manter o trabalho baseado no projeto.'
    },
    {
      headline: 'Por onde começamos?',
      body: `Dê o contexto e eu responderei no modo ${label}.`
    }
  ]
}

function pickCopy(copies: IntroCopy[], seed = 0): IntroCopy {
  return copies[Math.abs(seed) % copies.length] || FALLBACK_COPY[0]
}

const WORDMARK = 'HERMES AGENT'

export function resolveIntroCopy(personality?: string, seed?: number, locale: Locale = 'en'): IntroCopy {
  const personalityKey = normalizeKey(personality)

  if (locale === 'pt-br') {
    const copies = NEUTRAL_PERSONALITIES.has(personalityKey)
      ? PT_BR_NEUTRAL_COPY
      : ptBrFallbackCopyForPersonality(personalityKey)

    return pickCopy(copies, seed)
  }

  const copies = NEUTRAL_PERSONALITIES.has(personalityKey)
    ? INTRO_COPY_BY_PERSONALITY[personalityKey] || neutralCopy()
    : INTRO_COPY_BY_PERSONALITY[personalityKey] || fallbackCopyForPersonality(personalityKey)

  return pickCopy(copies, seed)
}

export function Intro({ personality, seed }: IntroProps) {
  const { locale } = useI18n()
  const [mountSeed] = useState(() => Math.floor(Math.random() * 100000))
  const copy = resolveIntroCopy(personality, mountSeed + (seed ?? 0), locale)

  return (
    <div
      className="pointer-events-none flex w-full min-w-0 flex-col items-center justify-center px-0.5 py-6 text-center text-muted-foreground sm:px-6 lg:px-8"
      data-slot="aui_intro"
    >
      <div className="w-full min-w-0">
        <Wordmark className="mb-1" text={WORDMARK} />

        <p className="m-0 text-center leading-normal tracking-tight">{copy.body}</p>
      </div>
    </div>
  )
}


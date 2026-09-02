import type { HermesGateway } from '@/hermes'

export type PromptRewriteMode = 'basic' | 'brief' | 'detailed' | 'enhance'

const COMMON_INSTRUCTIONS = `Rewrite the user's draft into a stronger prompt for an AI agent.

Preserve the user's intent, facts, technical identifiers, paths, host names, commands, time limits, and constraints. Preserve every @file:, @folder:, @url:, @git:, and similar inline reference exactly. Do not solve the task. Never invent facts, credentials, technologies, deadlines, or decisions. Only the Enhance mode may propose additional engineering considerations, and it must distinguish them from requirements the user actually stated. When something important is genuinely unknown, state it as an explicit question, assumption, or recommendation instead of presenting it as fact. Return only the rewritten prompt, with no preamble, commentary, quotation marks, or code fence.`

const MODE_INSTRUCTIONS: Record<PromptRewriteMode, string> = {
  basic:
    'Rewrite the draft for clarity without expanding its scope. Fix grammar, remove repetition, resolve obvious phrasing ambiguity, and make the request direct while preserving every material requirement. Keep roughly the same length and level of detail; return one concise paragraph.',
  brief:
    'Expand the draft only enough to make it actionable while keeping it genuinely brief. Return at most 120 words as one compact paragraph or no more than four bullets. Include the objective, essential requirements or constraints, and a concise expected outcome or verification statement. Do not create a full specification, multiple sections, or a separate questions list. Mention an unknown inline only when proceeding without it would be unsafe.',
  detailed:
    'Expand the draft into an implementation-ready specification. Organize the objective, context, functional requirements, constraints, end-to-end flow, edge cases, failure handling, testing, acceptance criteria, and definition of done when relevant. Surface missing decisions as questions or assumptions instead of inventing answers.',
  enhance:
    "Enhance the draft as a coding task using the user's intended outcome and any supplied codebase facts. Preserve a clear distinction between stated requirements and recommended additions. Add only strongly relevant engineering considerations, such as fitting existing project conventions, compatibility, safety, observability, tests, rollout or rollback, documentation, and verification commands. Omit categories that do not help this task. Never claim to have inspected source files or architecture that are not present in the supplied facts."
}

const MAX_TOKENS: Record<PromptRewriteMode, number> = {
  basic: 320,
  brief: 240,
  detailed: 1800,
  enhance: 2200
}

// Prompt rewrite must honor the profile's selected model, including reasoning
// defaults that legitimately take longer than the generic one-shot deadline.
// The backend timeout is explicit and bounded; keep the RPC alive slightly
// longer so it can return the answer (or the real provider error).
const PROMPT_REWRITE_MODEL_TIMEOUT_SECONDS = 180
const PROMPT_REWRITE_RPC_GRACE_MS = 15_000

const PROMPT_REWRITE_RPC_TIMEOUT_MS =
  PROMPT_REWRITE_MODEL_TIMEOUT_SECONDS * 1_000 + PROMPT_REWRITE_RPC_GRACE_MS

const PROJECT_FACT_ENTRY_LIMIT = 160

interface ProjectFacts {
  contextFiles?: unknown
  manifests?: unknown
  packageManagers?: unknown
  verifyCommands?: unknown
}

function boundedProjectFact(value: string): string {
  const normalized = value.trim().replace(/\s+/g, ' ')

  return normalized.length <= PROJECT_FACT_ENTRY_LIMIT
    ? normalized
    : `${normalized.slice(0, PROJECT_FACT_ENTRY_LIMIT - 1)}…`
}

function cleanRewriteResult(value: string): string {
  const trimmed = value.trim()
  const outerFence = /^```[^\r\n]*\r?\n([\s\S]*?)\r?\n```$/.exec(trimmed)

  return (outerFence?.[1] ?? trimmed).trim()
}

function projectFactsContext(facts: ProjectFacts | null | undefined): string {
  if (!facts) {
    return 'No project context was supplied. Enhance from the draft alone and do not invent project-specific details.'
  }

  const rows: [string, unknown][] = [
    ['Manifests', facts.manifests],
    ['Package managers', facts.packageManagers],
    ['Verification commands', facts.verifyCommands],
    ['Context files', facts.contextFiles]
  ]

  const lines = rows.flatMap(([label, value]) => {
    if (!Array.isArray(value)) {
      return []
    }

    const entries = value
      .filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
      .map(boundedProjectFact)
      .slice(0, 12)

    return entries.length > 0 ? [`- ${label}: ${entries.join(', ')}`] : []
  })

  return lines.length > 0
    ? `Detected codebase facts (use only when relevant):\n${lines.join('\n')}`
    : 'No project context was supplied. Enhance from the draft alone and do not invent project-specific details.'
}

export function promptRewriteInstructions(mode: PromptRewriteMode, facts?: ProjectFacts | null): string {
  const context = mode === 'enhance' ? `\n\n${projectFactsContext(facts)}` : ''

  return `${COMMON_INSTRUCTIONS}\n\nRewrite style:\n${MODE_INSTRUCTIONS[mode]}${context}`
}

interface RequestPromptRewriteArgs {
  cwd?: null | string
  gateway: HermesGateway
  mode: PromptRewriteMode
  sessionId?: null | string
  text: string
}

/**
 * Rewrite through the exact chat gateway so profile and remote routing cannot
 * drift. A live session additionally carries a Bot or per-session model
 * override; before the first message, the profile-scoped gateway resolves its
 * configured main model. llm.oneshot never appends to conversation history.
 */
export async function requestPromptRewrite({
  cwd,
  gateway,
  mode,
  sessionId,
  text
}: RequestPromptRewriteArgs): Promise<string> {
  let facts: ProjectFacts | null | undefined

  if (mode === 'enhance' && cwd?.trim()) {
    try {
      facts = (await gateway.request<{ facts?: ProjectFacts | null }>('project.facts', { cwd }))?.facts
    } catch {
      // Project facts are an optional enhancement. A stale cwd, an older
      // remote gateway, or project detection failure must not make prompt
      // rewriting unavailable — the draft alone is still valid context.
      facts = undefined
    }
  }

  const result = await gateway.request<{ text?: string }>(
    'llm.oneshot',
    {
      input: text,
      instructions: promptRewriteInstructions(mode, facts),
      max_tokens: MAX_TOKENS[mode],
      session_id: sessionId || undefined,
      task: 'prompt_rewrite',
      temperature: 0.2,
      timeout: PROMPT_REWRITE_MODEL_TIMEOUT_SECONDS
    },
    PROMPT_REWRITE_RPC_TIMEOUT_MS
  )

  return cleanRewriteResult(result?.text ?? '')
}

import type { AsyncDelegationRecord } from '../gatewayTypes.js'
import type { SubagentProgress } from '../types.js'

import { shortAgentId } from './agentRows.js'
import { compactPreview } from './text.js'

// Parse + resolve the composer's `@<id> steer text` shorthand. Kept pure and
// ink-free so it's unit testable and the submit hot-path only calls into it.

export interface SteerCommand {
  body: string
  token: string
}

// `@<token> <text>` where token is a run of non-space chars and text is the
// rest (dotall so multi-line steers survive). Returns null for anything that
// isn't shaped like a steer so ordinary prompts pass straight through.
const STEER_RE = /^@(\S+)\s+([\s\S]+)$/

export const parseSteerCommand = (text: string): SteerCommand | null => {
  const m = STEER_RE.exec(text.trimStart())

  if (!m) {
    return null
  }

  const body = m[2]!.trim()

  return body ? { body, token: m[1]! } : null
}

/** Resolve a steer token to a live subagent id. Only live in-turn subagents
 * (which are actually addressable in the backend registry) are candidates.
 * Matches an exact id first, then a unique id-prefix; returns null when the
 * token is ambiguous or matches nothing so the caller falls back to a normal
 * turn. Ambiguity resolving to nothing is deliberate — never steer a guess. */
export const resolveSteerTargetId = (token: string, subagents: SubagentProgress[]): null | string => {
  const running = subagents.filter(s => s.status === 'running' || s.status === 'queued')

  const exact = running.find(s => s.id === token)

  if (exact) {
    return exact.id
  }

  const prefixed = running.filter(s => s.id.startsWith(token))

  return prefixed.length === 1 ? prefixed[0]!.id : null
}

export const resolveAsyncSteerTargetId = (
  token: string,
  delegations: readonly AsyncDelegationRecord[]
): null | string => {
  const running = delegations.filter(d => d.status === 'running')
  const exact = running.find(d => d.delegation_id === token)

  if (exact) {
    return exact.delegation_id
  }

  const prefixed = running.filter(d => d.delegation_id.startsWith(token))

  return prefixed.length === 1 ? prefixed[0]!.delegation_id : null
}

/** A completion candidate for the `@` id position. Structurally a
 * `CompletionItem`, but declared here so this module stays free of app types. */
export interface SteerCompletion {
  display: string
  meta: string
  text: string
}

/** True while the composer holds only `@` + a partial id (no steer text yet) —
 * the one moment where completing an agent id is what the user wants. Once a
 * space is typed the input is a steer body and must be left alone. */
const STEER_TOKEN_RE = /^@(\S*)$/

export const steerTokenPrefix = (text: string): null | string => STEER_TOKEN_RE.exec(text)?.[1] ?? null

/** Completion candidates for `@<id>`. Only agents that `resolveSteerTargetId` /
 * `resolveAsyncSteerTargetId` would actually accept are offered, so a completion
 * can never insert a token that then falls through as an ordinary prompt. Ids
 * are abbreviated exactly like the panel prints them — what you read is what you
 * can type, and what completion inserts. */
export const steerCompletions = (
  prefix: string,
  subagents: SubagentProgress[],
  delegations: readonly AsyncDelegationRecord[]
): SteerCompletion[] => {
  const live = subagents.filter(s => s.status === 'running' || s.status === 'queued')
  const background = delegations.filter(d => d.status === 'running')
  // Abbreviate against *every* id the panel knows about, finished ones included
  // — `buildAgentRows` does the same, and an id shortened against a narrower set
  // would print one way in the panel and complete another way here.
  const all = [...subagents.map(s => s.id), ...delegations.map(d => d.delegation_id)]

  const item = (id: string, goal: string, kind: string): SteerCompletion => {
    const short = shortAgentId(id, all)

    return {
      display: `@${short}`,
      meta: goal ? `${kind} · ${compactPreview(goal, 48)}` : kind,
      // Trailing space: the steer text always follows the id, so completing
      // straight into `@b7c2 ` saves the keystroke and can't be mistaken for a
      // complete input.
      text: `@${short} `
    }
  }

  return [
    ...live.map(s => item(s.id, s.goal, 'live subagent')),
    ...background.map(d => item(d.delegation_id, d.goal ?? '', 'background'))
  ].filter(c => c.display.slice(1).startsWith(prefix))
}

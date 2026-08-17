import { atom } from 'nanostores'
import { useSyncExternalStore } from 'react'

import { isTodoDone } from '../lib/liveProgress.js'
import { TOOL_ICON_FALLBACK } from '../lib/text.js'
import type { ActiveTool, ActivityItem, Msg, SubagentProgress, TodoItem } from '../types.js'

const buildTurnState = (): TurnState => ({
  activity: [],
  outcome: '',
  reasoning: '',
  reasoningActive: false,
  reasoningStreaming: false,
  reasoningTokens: 0,
  streamPendingTools: [],
  streamSegments: [],
  streaming: '',
  subagents: [],
  todoCollapsed: false,
  todos: [],
  toolTokens: 0,
  tools: [],
  turnTrail: []
})

export const $turnState = atom<TurnState>(buildTurnState())

export const getTurnState = () => $turnState.get()

const subscribeTurn = (cb: () => void) => $turnState.listen(() => cb())

export const useTurnSelector = <T>(selector: (state: TurnState) => T): T =>
  useSyncExternalStore(
    subscribeTurn,
    () => selector($turnState.get()),
    () => selector($turnState.get())
  )

export const patchTurnState = (next: Partial<TurnState> | ((state: TurnState) => TurnState)) =>
  $turnState.set(typeof next === 'function' ? next($turnState.get()) : { ...$turnState.get(), ...next })

export const toggleTodoCollapsed = () => patchTurnState(state => ({ ...state, todoCollapsed: !state.todoCollapsed }))

export const archiveDoneTodos = () => archiveTodosAtTurnEnd()

export const archiveTodosAtTurnEnd = () => {
  const state = $turnState.get()

  if (!state.todos.length) {
    return []
  }

  const done = isTodoDone(state.todos)

  const msg: Msg = {
    kind: 'trail',
    role: 'system',
    text: '',
    todos: state.todos,
    ...(done ? { todoCollapsedByDefault: true } : { todoIncomplete: true })
  }

  patchTurnState({ todoCollapsed: false, todos: [] })

  return [msg]
}

// ── Tool icon registry ───────────────────────────────────────────────
//
// NOT a tool→icon table: it holds nothing but glyphs the gateway already
// resolved through `agent.display.get_tool_emoji` and shipped on the
// tool.start / tool.complete frames. It exists because the completed trail is
// persisted as plain strings (`Msg.tools`, `TurnState.turnTrail`) — there is
// nowhere on a `string` to hang the icon, and widening that type would ripple
// through every segment-merge path.
//
// Deliberately NOT reactive: a nanostore here would re-subscribe every
// memoised history row to the turn atom and re-render the whole transcript on
// each streamed token. Reads are safe without a subscription because
// tool.start always lands (and files the icon) before the row it feeds can
// exist, and the state patch that creates the row is what triggers the paint.
const toolIcons = new Map<string, string>()

/** File the server-resolved glyph under a `toolTrailLabel` key. */
export const rememberToolIcon = (label: string, icon?: string) => {
  if (label && icon) {
    toolIcons.set(label, icon)
  }
}

/** Look a glyph up, or fall back to the same default the backend uses. */
export const resolveToolIcon = (label: string, fallback = TOOL_ICON_FALLBACK) =>
  toolIcons.get(label) ?? fallback

export const resetTurnState = () => {
  // Session boundary (fullReset only, never a per-turn reset): the next
  // session replays its own tool.start frames.
  toolIcons.clear()
  $turnState.set(buildTurnState())
}

export interface TurnState {
  activity: ActivityItem[]
  outcome: string
  reasoning: string
  reasoningActive: boolean
  reasoningStreaming: boolean
  reasoningTokens: number
  streamPendingTools: string[]
  streamSegments: Msg[]
  streaming: string
  subagents: SubagentProgress[]
  todoCollapsed: boolean
  todos: TodoItem[]
  toolTokens: number
  tools: ActiveTool[]
  turnTrail: string[]
}

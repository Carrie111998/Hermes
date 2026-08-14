/**
 * Per-turn tool summary — TS port of `agent/turn_summary.py` (CLI parity).
 *
 * A tiny pure module that tallies what a turn actually did and renders one
 * dim line, e.g.:
 *
 *     ⋯ 12.4s · edited 2 files +18 -3 · read 4 files · ran 3 commands
 *
 * Ported from the Python CLI module so every surface renders byte-identical
 * output. Holds no agent state: the caller feeds completed tool events via
 * `recordTool` and renders when the turn ends.
 */

export interface TurnTally {
  /** verb -> plural-noun -> count; insertion order preserved for stable rendering. */
  verbs: Record<string, Record<string, number>>
  /** Tools with no curated verb, counted together. */
  otherTools: number
  /** Aggregated unified-diff line deltas across edit tools, when reported. */
  linesAdded: number
  linesRemoved: number
  /** True once at least one edit tool reported a countable diff. */
  hasLineDeltas: boolean
  /** Sum of all verb counts + otherTools. */
  totalTools: number
}

const VERB_TABLE: Record<string, [string, string, string]> = {
  write_file: ['edited', 'file', 'files'],
  patch: ['edited', 'file', 'files'],
  read_file: ['read', 'file', 'files'],
  web_extract: ['read', 'page', 'pages'],
  terminal: ['ran', 'command', 'commands'],
  execute_code: ['ran', 'script', 'scripts'],
  search_files: ['searched', 'path', 'paths'],
  web_search: ['searched the web', 'time', 'times'],
  session_search: ['searched sessions', 'time', 'times'],
  browser_navigate: ['browsed', 'page', 'pages'],
  skill_view: ['read', 'skill', 'skills'],
  skill_manage: ['updated', 'skill', 'skills'],
  skills_list: ['listed skills', 'time', 'times'],
  todo: ['updated', 'task list', 'task lists'],
  delegate_task: ['delegated', 'task', 'tasks'],
  memory: ['updated', 'memory', 'memories'],
}

const EDIT_VERB = 'edited'

// Render order: edits first (the thing users most want confirmed), then reads,
// then commands. Anything else follows in first-seen order.
const VERB_PRIORITY: readonly string[] = ['edited', 'read', 'ran']

// Tools whose results may report a unified diff we can count lines from.
const DIFF_RESULT_TOOLS = new Set(['patch'])

// Leading glyph for the summary line. Deliberately not an emoji — the line is
// meant to read as terminal chrome, not as agent speech.
const SUMMARY_PREFIX = '⋯ '

// A turn that called no tools and finished this fast has nothing worth
// reporting (plain chat reply). Below the threshold the formatter returns ''.
const MIN_TOOLLESS_SECONDS = 2.0

// Max number of "verb + count" segments rendered before collapsing the rest
// into a "+N more" tail, so a 12-tool turn cannot blow past one line.
const MAX_SEGMENTS = 4

function singularize(pluralNoun: string): string {
  if (pluralNoun.endsWith('ies')) {
    return pluralNoun.slice(0, -3) + 'y'
  }

  if (pluralNoun.endsWith('ses')) {
    return pluralNoun.slice(0, -2)
  }

  if (pluralNoun.endsWith('s')) {
    return pluralNoun.slice(0, -1)
  }

  return pluralNoun
}

function countDiffLines(diff: string): [number, number] {
  let added = 0
  let removed = 0

  for (const line of diff.split(/\r?\n/)) {
    if (line.startsWith('+++') || line.startsWith('---')) {
      continue
    }

    if (line.startsWith('+')) {
      added += 1
    } else if (line.startsWith('-')) {
      removed += 1
    }
  }

  return [added, removed]
}

function extractLineDeltas(toolName: string, result: unknown): [number, number] | null {
  if (!DIFF_RESULT_TOOLS.has(toolName)) {
    return null
  }

  let diffStr: string | undefined

  if (typeof result === 'string') {
    try {
      const parsed = JSON.parse(result) as unknown

      if (parsed && typeof parsed === 'object' && typeof (parsed as { diff?: unknown }).diff === 'string') {
        diffStr = (parsed as { diff: string }).diff
      }
    } catch {
      // strict=False tolerance: retry with control characters escaped so raw
      // newlines inside an embedded diff string don't fail parsing.
      try {
        // eslint-disable-next-line no-control-regex -- intentionally matching control chars to strip from tool results
        const controlChars = new RegExp('[\\x00-\\x1f\\x7f]', 'g')

        const sanitized = result.replace(controlChars, c => {
          if (c === '\n') {return '\\n'}

          if (c === '\r') {return '\\r'}

          if (c === '\t') {return '\\t'}

          return ''
        })

        const parsed = JSON.parse(sanitized) as unknown

        if (parsed && typeof parsed === 'object' && typeof (parsed as { diff?: unknown }).diff === 'string') {
          diffStr = (parsed as { diff: string }).diff
        }
      } catch {
        // ignore invalid JSON
      }
    }
  } else if (result && typeof result === 'object') {
    const obj = result as Record<string, unknown>

    if (typeof obj.diff === 'string') {
      diffStr = obj.diff
    }
  }

  if (!diffStr) {
    return null
  }

  const [added, removed] = countDiffLines(diffStr)

  // A diff that carries no +/- content lines (e.g. a bare hunk header) tells
  // us nothing — report it as unknown rather than rendering a misleading
  // "+0 -0" next to a real edit.
  if (added === 0 && removed === 0) {
    return null
  }

  return [added, removed]
}

function emptyTally(): TurnTally {
  return {
    verbs: {},
    otherTools: 0,
    linesAdded: 0,
    linesRemoved: 0,
    hasLineDeltas: false,
    totalTools: 0,
  }
}

export class TurnSummaryCollector {
  private _tally: TurnTally = emptyTally()

  begin(): void {
    this._tally = emptyTally()
  }

  recordTool(toolName: string | null | undefined, opts?: { result?: unknown; isError?: boolean }): void {
    if (!toolName || opts?.isError === true || toolName.startsWith('_')) {
      return
    }

    const mapping = VERB_TABLE[toolName]

    if (!mapping) {
      this._tally.otherTools += 1

      return
    }

    const [verb, , pluralNoun] = mapping
    const nouns = (this._tally.verbs[verb] ??= {})
    nouns[pluralNoun] = (nouns[pluralNoun] ?? 0) + 1

    if (verb === EDIT_VERB) {
      const deltas = extractLineDeltas(toolName, opts?.result)

      if (deltas) {
        this._tally.linesAdded += deltas[0]
        this._tally.linesRemoved += deltas[1]
        this._tally.hasLineDeltas = true
      }
    }
  }

  get tally(): TurnTally {
    let counted = 0
    const verbsCopy: Record<string, Record<string, number>> = {}

    for (const [verb, nounMap] of Object.entries(this._tally.verbs)) {
      verbsCopy[verb] = { ...nounMap }

      for (const count of Object.values(nounMap)) {
        counted += count
      }
    }

    return {
      verbs: verbsCopy,
      otherTools: this._tally.otherTools,
      linesAdded: this._tally.linesAdded,
      linesRemoved: this._tally.linesRemoved,
      hasLineDeltas: this._tally.hasLineDeltas,
      totalTools: counted + this._tally.otherTools,
    }
  }

  render(elapsedSeconds: number): string {
    return formatTurnSummary(elapsedSeconds, this.tally)
  }
}

export function formatElapsed(seconds: number): string {
  const secs = Math.max(0, seconds)

  if (secs < 60) {
    return `${secs.toFixed(1)}s`
  }

  const mins = Math.floor(secs / 60)
  const remSecs = Math.floor(secs % 60)

  return `${mins}m${String(remSecs).padStart(2, '0')}s`
}

function pluralize(count: number, pluralNoun: string): string {
  if (count === 1) {
    return `1 ${singularize(pluralNoun)}`
  }

  return `${count} ${pluralNoun}`
}

export function formatTurnSummary(
  elapsedSeconds: number,
  tally: TurnTally | null,
  opts?: { maxSegments?: number },
): string {
  const totalTools = tally ? tally.totalTools : 0

  if (totalTools === 0 && elapsedSeconds < MIN_TOOLLESS_SECONDS) {
    return ''
  }

  const maxSegments = opts?.maxSegments ?? MAX_SEGMENTS

  // Verb segments only — the elapsed piece is always prepended and never
  // counts against the cap (Python parity: max_segments trims the verb list).
  const verbSegments: string[] = []

  if (tally) {
    const presentVerbs = Object.keys(tally.verbs)

    const orderedVerbs = [
      ...VERB_PRIORITY.filter(v => presentVerbs.includes(v)),
      ...presentVerbs.filter(v => !VERB_PRIORITY.includes(v)),
    ]

    for (const verb of orderedVerbs) {
      const nounCounts = tally.verbs[verb]

      if (!nounCounts) {
        continue
      }

      const parts = Object.entries(nounCounts)
        .filter(([, count]) => count > 0)
        .map(([pluralNoun, count]) => pluralize(count, pluralNoun))

      if (!parts.length) {
        continue
      }

      let segment = `${verb} ${parts.join(', ')}`

      if (verb === EDIT_VERB && tally.hasLineDeltas) {
        segment += ` +${tally.linesAdded} -${tally.linesRemoved}`
      }

      verbSegments.push(segment)
    }

    if (tally.otherTools > 0) {
      verbSegments.push(
        tally.otherTools === 1 ? 'called 1 tool' : `called ${tally.otherTools} tools`,
      )
    }
  }

  if (maxSegments > 0 && verbSegments.length > maxSegments) {
    const hidden = verbSegments.length - maxSegments
    verbSegments.splice(maxSegments, verbSegments.length - maxSegments, `+${hidden} more`)
  }

  return `${SUMMARY_PREFIX}${[formatElapsed(elapsedSeconds), ...verbSegments].join(' · ')}`
}

export function formatTokenFlow(outputTokens: unknown, opts?: { arrow?: string }): string {
  const arrow = opts?.arrow ?? '↓'

  if (outputTokens === null || outputTokens === undefined) {
    return ''
  }

  const num = typeof outputTokens === 'number' ? outputTokens : Number(outputTokens)

  if (!Number.isFinite(num) || num <= 0) {
    return ''
  }

  let formatted: string

  if (num < 1000) {
    formatted = `${num} tok`
  } else if (num < 1_000_000) {
    formatted = `${(num / 1000).toFixed(1).replace(/\.0$/, '')}k tok`
  } else {
    formatted = `${(num / 1_000_000).toFixed(1).replace(/\.0$/, '')}M tok`
  }

  return `${arrow} ${formatted}`
}

export interface WakeVoiceRouteSession {
  archived?: boolean
  id: string
  preview?: null | string
  profile?: null | string
  title?: null | string
}

export interface WakeVoiceRouteAlternative {
  destination: string
  prompt: string
}

export type WakeVoiceRouteResolution =
  | { kind: 'none' }
  | { destination: string; kind: 'invalid'; reason: 'ambiguous_syntax' | 'missing_prompt' }
  | { destination: string; kind: 'missing' }
  | { candidates: string[]; destination: string; kind: 'ambiguous' }
  | { kind: 'match'; prompt: string; sessionId: string; title: string }

export type WakeVoiceRouteCommand =
  | { kind: 'none' }
  | { destination: string; kind: 'invalid'; reason: 'ambiguous_syntax' | 'missing_prompt' }
  | { alternatives: WakeVoiceRouteAlternative[]; kind: 'command' }

const ROUTE_PREFIX = String.raw`(?:(?:send|route)(?:\s+this)?\s+to|continue\s+in)`
const ROUTE_BODY_RE = new RegExp(String.raw`^\s*${ROUTE_PREFIX}\s+(?:the\s+)?([\s\S]+?)\s*$`, 'iu')
const ROUTE_DELIMITER_RE = /\s+session\s*([:;,—–-]|\band\b)\s*/giu
const INCOMPLETE_ROUTE_RE = /^(.*?)\s+session\s*[:;,—–.!?-]?\s*$/iu

/**
 * Speech-friendly comparison key: case/punctuation/spacing insensitive, with
 * compatibility normalization and combining marks removed so STT output such
 * as "resume" can address a title containing "résumé". The original prompt is
 * never normalized or rewritten.
 */
function voiceLabel(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/\p{M}+/gu, '')
    .toLowerCase()
    .replace(/[\p{P}\p{S}]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

const profileKey = (profile: null | string | undefined): string => profile?.trim() || 'default'
const visibleTitle = (session: WakeVoiceRouteSession): string => session.title?.trim() || session.preview?.trim() || ''

/**
 * Parse only explicit route-looking syntax. Every safe delimiter is retained as
 * an alternative because both titles and prompts can contain the word
 * "session"; the resolver chooses only when the complete session set makes one
 * interpretation unique.
 */
export function parseWakeVoiceRoute(transcript: string): WakeVoiceRouteCommand {
  const bodyMatch = transcript.match(ROUTE_BODY_RE)

  if (!bodyMatch) {
    return { kind: 'none' }
  }

  const body = bodyMatch[1]!.trim()
  const alternatives: WakeVoiceRouteAlternative[] = []

  for (const delimiter of body.matchAll(ROUTE_DELIMITER_RE)) {
    const destination = body.slice(0, delimiter.index).trim()
    const prompt = body.slice(delimiter.index + delimiter[0].length).trim()

    if (destination && prompt) {
      alternatives.push({ destination, prompt })
    }
  }

  if (alternatives.length > 0) {
    return { alternatives, kind: 'command' }
  }

  const incomplete = body.match(INCOMPLETE_ROUTE_RE)

  if (incomplete) {
    return { destination: incomplete[1]!.trim(), kind: 'invalid', reason: 'missing_prompt' }
  }

  return /\bsession\b/iu.test(body)
    ? { destination: body, kind: 'invalid', reason: 'ambiguous_syntax' }
    : { kind: 'none' }
}

interface RankedMatch {
  prompt: string
  sessionId: string
  title: string
}

function matchesForAlternative(
  alternative: WakeVoiceRouteAlternative,
  candidates: Array<{ key: string; session: WakeVoiceRouteSession; title: string }>
): { ambiguous: string[]; matches: RankedMatch[] } {
  const needle = voiceLabel(alternative.destination)
  const exact = candidates.filter(candidate => candidate.key === needle)

  if (exact.length > 0) {
    return {
      ambiguous: exact.length > 1 ? exact.map(candidate => candidate.title) : [],
      matches:
        exact.length === 1
          ? [{ prompt: alternative.prompt, sessionId: exact[0]!.session.id, title: exact[0]!.title }]
          : []
    }
  }

  const prefixes = needle.length >= 3 ? candidates.filter(candidate => candidate.key.startsWith(needle)) : []

  return {
    ambiguous: prefixes.length > 1 ? prefixes.map(candidate => candidate.title) : [],
    matches:
      prefixes.length === 1
        ? [{ prompt: alternative.prompt, sessionId: prefixes[0]!.session.id, title: prefixes[0]!.title }]
        : []
  }
}

/** Resolve all syntactically valid boundaries and fail closed unless one wins. */
export function resolveWakeVoiceRouteCommand(
  command: Extract<WakeVoiceRouteCommand, { kind: 'command' }>,
  sessions: readonly WakeVoiceRouteSession[],
  activeProfile: string
): WakeVoiceRouteResolution {
  const candidates = sessions
    .filter(session => !session.archived && profileKey(session.profile) === profileKey(activeProfile))
    .map(session => ({ session, title: visibleTitle(session) }))
    .filter(candidate => candidate.title)
    .map(candidate => ({ ...candidate, key: voiceLabel(candidate.title) }))

  const resolved = command.alternatives.map(alternative => ({
    alternative,
    ...matchesForAlternative(alternative, candidates)
  }))

  const matches = resolved.flatMap(result => result.matches)

  const uniqueMatches = [
    ...new Map(matches.map(match => [`${match.sessionId}\u0000${match.prompt}`, match])).values()
  ]

  const ambiguous = resolved.flatMap(result => result.ambiguous)

  if (uniqueMatches.length === 1 && ambiguous.length === 0) {
    const match = uniqueMatches[0]!

    return { kind: 'match', prompt: match.prompt, sessionId: match.sessionId, title: match.title }
  }

  if (uniqueMatches.length > 1 || ambiguous.length > 0) {
    return {
      candidates: [...uniqueMatches.map(match => match.title), ...ambiguous],
      destination: command.alternatives[0]!.destination,
      kind: 'ambiguous'
    }
  }

  return { destination: command.alternatives[0]!.destination, kind: 'missing' }
}

export function resolveWakeVoiceRoute(
  transcript: string,
  sessions: readonly WakeVoiceRouteSession[],
  activeProfile: string
): WakeVoiceRouteResolution {
  const command = parseWakeVoiceRoute(transcript)

  return command.kind === 'command' ? resolveWakeVoiceRouteCommand(command, sessions, activeProfile) : command
}

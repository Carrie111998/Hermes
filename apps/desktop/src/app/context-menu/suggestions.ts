/**
 * Edit-distance suggestion engine.
 *
 * Generates candidate words within one edit of the misspelled word and keeps
 * the ones present in the dictionary — Norvig's classic "candidates"
 * approach. Every candidate is a known-word lookup, so it is fast even
 * against a ~77k-word dictionary (no distance matrices).
 *
 * Candidates are ranked by operation type, because the operation is the
 * strongest signal for real typos: a TRANSPOSITION or DELETION fixes the word
 * by moving/removing the typist's own letters, while a SUBSTITUTION or
 * INSERTION invents letters. "teh" -> "the" (transposition) beats "tea"
 * (substitution); "juumps" -> "jumps" (deletion) beats "jumpy".
 */

import { isKnownWord } from './dictionary'

const ALPHA = 'abcdefghijklmnopqrstuvwxyz'

export function transpositionsOf(word: string): Set<string> {
  const out = new Set<string>()
  const n = word.length

  for (let i = 0; i < n - 1; i++) {
    out.add(word.slice(0, i) + word[i + 1] + word[i] + word.slice(i + 2))
  }

  return out
}

export function deletionsOf(word: string): Set<string> {
  const out = new Set<string>()

  for (let i = 0; i < word.length; i++) {
    out.add(word.slice(0, i) + word.slice(i + 1))
  }

  return out
}

export function substitutionsOf(word: string): Set<string> {
  const out = new Set<string>()

  for (let i = 0; i < word.length; i++) {
    for (const c of ALPHA) {
      out.add(word.slice(0, i) + c + word.slice(i + 1))
    }
  }

  return out
}

export function insertionsOf(word: string): Set<string> {
  const out = new Set<string>()

  for (let i = 0; i <= word.length; i++) {
    for (const c of ALPHA) {
      out.add(word.slice(0, i) + c + word.slice(i))
    }
  }

  return out
}

/** All strings within one edit of `word` (used for the two-edit fallback). */
export function edit1(word: string): Set<string> {
  const out = transpositionsOf(word)

  for (const w of deletionsOf(word)) {
    out.add(w)
  }

  for (const w of substitutionsOf(word)) {
    out.add(w)
  }

  for (const w of insertionsOf(word)) {
    out.add(w)
  }

  return out
}

/** All strings within two edits of `word` (only used when one edit is empty). */
export function edit2(word: string): Set<string> {
  const out = new Set<string>()

  for (const e1 of edit1(word)) {
    for (const e2 of edit1(e1)) {
      out.add(e2)
    }
  }

  return out
}

/** True when `a` is a subsequence of `b` (every char of `a`, in order). */
function isSubsequence(a: string, b: string): boolean {
  if (a.length > b.length) {
    return false
  }
  let i = 0

  for (let j = 0; j < b.length && i < a.length; j++) {
    if (a[i] === b[j]) {
      i++
    }
  }

  return i === a.length
}

/**
 * Score a correction candidate. Lower is better. Signals, strongest first:
 *  - the candidate preserves the typed letters in order (a deletion or
 *    insertion fix: "better" ⊃ "beter", "beer" ⊂ "beter") — these use the
 *    typist's own letters, so they beat substitutions that invent letters,
 *  - exact dictionary word,
 *  - common start ("better" shares "bet" with "beter", "betel" shares "bete"),
 *  - the operation that produced it,
 *  - length closeness, so synthetic morphology guesses ("urning") fall far
 *    behind real words.
 */
function scoreCandidate(candidate: string, typed: string, dict: ReadonlySet<string>, op: number): number {
  let score = op

  if (dict.has(candidate)) {
    score -= 3
  } else if (candidate.length >= 3 && /(?:s|es|ing|ed|er|est|ly)$/.test(candidate) && isKnownWord(candidate, dict)) {
    score -= 2
  } // common inflection of a real stem: "books", "running", "nicer"

  if (op === 0) {
    score -= 4
  } // transposition: classic typo ("teh"->"the", "adn"->"and")

  if (candidate.length >= 3 && (isSubsequence(typed, candidate) || isSubsequence(candidate, typed))) {
    score -= 6
  }

  const shared = Math.min(candidate.length, typed.length, 6)
  let prefix = 0

  while (prefix < shared && candidate[prefix] === typed[prefix]) {
    prefix++
  }
  score -= prefix

  if (candidate[0] !== typed[0]) {
    score += 4
  }
  score += Math.abs(candidate.length - typed.length)

  return score
}

/**
 * Suggest corrections for a misspelled word (case-insensitive lookup).
 * Returns at most `max` suggestions (default 5 — matches the menu slice).
 */
export async function suggest(typed: string, dict: ReadonlySet<string>, max = 5): Promise<string[]> {
  const lower = typed.toLowerCase()

  const bestOp = new Map<string, number>()

  for (const [op, bucket] of [
    [0, transpositionsOf(lower)],
    [1, deletionsOf(lower)],
    [2, substitutionsOf(lower)],
    [3, insertionsOf(lower)]
  ] as Array<[number, Set<string>]>) {
    for (const candidate of bucket) {
      if (candidate === lower || candidate.length < 2 || !isKnownWord(candidate, dict)) {
        continue
      }
      const prev = bestOp.get(candidate)

      if (prev === undefined || op < prev) {
        bestOp.set(candidate, op)
      }
    }
  }

  const scored: Array<{ word: string; score: number }> = []

  for (const [candidate, op] of bestOp) {
    scored.push({ word: candidate, score: scoreCandidate(candidate, lower, dict, op) })
  }

  scored.sort((a, b) => a.score - b.score || a.word.localeCompare(b.word))

  if (scored.length) {
    return scored.slice(0, max).map(s => s.word)
  }

  // One edit found nothing (e.g. two substitutions: "seperate" -> "separate").
  const two: Array<{ word: string; score: number }> = []

  for (const candidate of edit2(lower)) {
    if (candidate === lower || candidate.length < 2 || !isKnownWord(candidate, dict)) {
      continue
    }
    two.push({ word: candidate, score: scoreCandidate(candidate, lower, dict, 2) })
  }

  two.sort((a, b) => a.score - b.score || a.word.localeCompare(b.word))

  return two.slice(0, max).map(s => s.word)
}

/** Match the case of the misspelled word so "Teh" -> "The", not "the". */
export function matchCase(suggestion: string, misspelled: string): string {
  if (/^[A-Z]/.test(misspelled)) {
    return suggestion[0].toUpperCase() + suggestion.slice(1)
  }

  return suggestion
}

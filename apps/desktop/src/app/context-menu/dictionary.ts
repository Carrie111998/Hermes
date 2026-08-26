/**
 * Offline English dictionary for spell-check suggestions.
 *
 * The word list is ~77k words and code-split by Vite: it is imported lazily
 * (dynamic import) the first time a context menu opens over an editable, then
 * cached for the session. This avoids the flaky main-process `context-menu`
 * spell-check facts (Chromium reports an empty misspelled word for
 * contenteditable on Linux) and computes suggestions directly in the renderer.
 */

let wordSetPromise: Promise<ReadonlySet<string>> | null = null

function loadWordSet(): Promise<ReadonlySet<string>> {
  wordSetPromise ??= import('./wordlist').then(m => new Set(m.wordList))

  return wordSetPromise
}

export const USER_WORDS_KEY = 'hermes.spellcheck.userWords.v1'

/** The full dictionary (lowercased). Loads on first call; cached after. */
export function getDictionary(): Promise<ReadonlySet<string>> {
  return loadWordSet()
}

/** Words the user chose "Add to dictionary" for. Persisted in localStorage. */
export function getUserWords(): ReadonlySet<string> {
  try {
    const raw = localStorage.getItem(USER_WORDS_KEY)

    if (!raw) {
      return new Set()
    }

    const parsed: unknown = JSON.parse(raw)

    if (!Array.isArray(parsed)) {
      return new Set()
    }

    return new Set(parsed.filter((w): w is string => typeof w === 'string'))
  } catch {
    return new Set()
  }
}

export function addUserWord(word: string): void {
  const lower = word.trim().toLowerCase()

  if (!lower) {
    return
  }

  try {
    const next = new Set(getUserWords())
    next.add(lower)
    localStorage.setItem(USER_WORDS_KEY, JSON.stringify([...next]))
  } catch {
    // storage unavailable — the Chromium-side add still ran; ignore
  }
}

/**
 * True when `word` is a known word: in the dictionary directly, in the
 * user-dictionary, or a common English inflection of a known stem. The
 * hunspell en_US list is a stem dictionary ("jump" but not "jumps",
 * "running", "boxes", "cities", "stopped", "quickly"), so raw Set.has would
 * flag half of normal English as misspelled. These pure suffix rules cover
 * the regular inflections; irregulars (go/went, child/children) are already
 * in the list as their own stems.
 */
/** Candidates the stem of `word` may be, after stripping a suffix of
 *  `strip` characters. Handles: bare stem, stem+e ("nice"+"r"), y-words
 *  ("happ"+"ier" -> happy), and doubled consonants ("big"+"ger"). */
function flexCandidates(word: string, strip: number): string[] {
  const base = word.slice(0, -strip)

  if (!base) {
    return []
  }

  const out = [base]
  out.push(base + 'e')

  if (base.endsWith('i')) {
    out.push(base.slice(0, -1) + 'y')
  } // happier -> happy

  if (base.length >= 2 && base[base.length - 1] === base[base.length - 2]) {
    out.push(base.slice(0, -1))
  }

  return out
}

export function isKnownWord(word: string, dict: ReadonlySet<string>): boolean {
  if (dict.has(word)) {
    return true
  }

  if (word.length < 4) {
    return false
  }

  const anyKnown = (cands: string[]): boolean => cands.some(c => dict.has(c))

  if (word.endsWith("'s")) {
    return anyKnown(flexCandidates(word.slice(0, -2), 1)) || dict.has(word.slice(0, -2))
  }

  if (word.endsWith('s')) {
    return anyKnown(flexCandidates(word, 1)) || anyKnown(flexCandidates(word, 2))
  } // jumps, boxes

  if (word.endsWith('ies')) {
    return anyKnown(flexCandidates(word, 3))
  } // cities -> city

  if (word.endsWith('ing')) {
    return anyKnown(flexCandidates(word, 3))
  } // running, taking, carrying

  if (word.endsWith('ed')) {
    return anyKnown(flexCandidates(word, 2))
  } // jumped, hoped, tried, stopped

  if (word.endsWith('est')) {
    return anyKnown(flexCandidates(word, 3))
  } // happiest, biggest, fastest

  if (word.endsWith('er')) {
    return anyKnown(flexCandidates(word, 2))
  } // faster, nicer, bigger, happier

  if (word.endsWith('ly')) {
    return anyKnown(flexCandidates(word, 2))
  } // quickly, happily

  return false
}

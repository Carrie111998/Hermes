const DELETE = "\x7f";

// How long (ms) after a mobile IME / replacement event we treat subsequent
// terminal input as a candidate line-replacement rather than a plain append.
// Exported so the ChatPage integration and tests share one tunable value.
export const MOBILE_REPLACEMENT_WINDOW_MS = 350;

function chars(text: string): string[] {
  return Array.from(text);
}

function removeLastChar(text: string): string {
  const c = chars(text);
  c.pop();
  return c.join("");
}

function isPlainText(data: string): boolean {
  // eslint-disable-next-line no-control-regex -- terminal data may contain control chars
  return !/[\x00-\x1f\x7f]/.test(data);
}

function lastWordMatch(line: string): RegExpMatchArray | null {
  return line.match(/^(.*?)(\S+)(\s*)$/u);
}

function collapseDuplicatedFinalWord(text: string, previousLine: string): string {
  const match = text.match(/^(.*?)(\S+)(\s+)(\S+)(\s*)$/u);
  if (!match) return text;

  const [, prefix, first, , second, trailing] = match;
  if (first.toLocaleLowerCase() !== second.toLocaleLowerCase()) return text;
  // Only collapse a duplication the tracked line already ended with — i.e.
  // Gboard re-emitted the final word. Requiring a >=2-char word avoids
  // eating legitimate single-letter reduplication ("a a", "i i") that a
  // user may genuinely type inside the replacement window.
  if (first.length < 2) return text;
  if (!previousLine.trimEnd().toLocaleLowerCase().endsWith(first.toLocaleLowerCase())) {
    return text;
  }
  return `${prefix}${first}${trailing}`;
}

/**
 * Strong-match gate for full-line DEL+retype.
 *
 * Only rewrite when the incoming payload is clearly an IME correction of the
 * tracked line (prefix completion / last-word correction), not an unrelated
 * multi-character insert that should append normally.
 */
function replacementLineForMobileInput(
  currentLine: string,
  incoming: string,
): string | null {
  if (!currentLine || currentLine.length < 2 || !incoming) return null;

  const currentLower = currentLine.toLocaleLowerCase();
  const incomingLower = incoming.toLocaleLowerCase();

  // Full-line / extended-line suggestion that still starts with what we
  // already tracked (case-insensitive). Strongest match.
  if (incomingLower.startsWith(currentLower)) {
    return collapseDuplicatedFinalWord(incoming, currentLine);
  }

  // Last-word autocorrect: tracked line ends on a word, IME emits the
  // corrected word (or corrected word + trailing text). Require the
  // incoming first token to share a case-insensitive stem with the last
  // tracked word so unrelated bursts cannot wipe the line.
  const word = lastWordMatch(currentLine);
  if (!word) return null;

  const [, prefix, last, trailing] = word;
  if (trailing) return null;
  if (last.length < 2) return null;

  const incomingFirst = incoming.trimStart().split(/\s+/u)[0] ?? "";
  if (!incomingFirst || incomingFirst.length < 2) return null;

  const lastLower = last.toLocaleLowerCase();
  const firstLower = incomingFirst.toLocaleLowerCase();

  const sameWordDifferentCase = firstLower === lastLower && incomingFirst !== last;
  const correctionOfLast =
    firstLower.startsWith(lastLower) || lastLower.startsWith(firstLower);

  if (!sameWordDifferentCase && !correctionOfLast) {
    return null;
  }

  // Rebuild: keep prefix, apply incoming (which may be just the new word or
  // word + more). Collapse Gboard's duplicated final-word form when present.
  if (firstLower === lastLower || lastLower.startsWith(firstLower) || firstLower.startsWith(lastLower)) {
    // If incoming is only the corrected token (optionally with more words),
    // splice it onto the prefix. If incoming already contains the prefix
    // path via collapse helper, collapseDuplicatedFinalWord handles it.
    if (incomingLower.startsWith(prefix.toLocaleLowerCase())) {
      return collapseDuplicatedFinalWord(incoming, currentLine);
    }
    return `${prefix}${collapseDuplicatedFinalWord(incoming, currentLine)}`;
  }

  return null;
}

/**
 * Mark the replacement window only for confirmed IME replacement / composition
 * events. Multi-character `insertText` (swipe typing, paste-like bursts) must
 * NOT open the full-line DEL+retype path — that was scrambling ordinary mobile
 * typing.
 *
 * `data` and `isMobileLike` are retained for call-site / API compatibility and
 * future heuristics; they no longer promote bare insertText to replacement.
 */
export function shouldTreatInputAsMobileReplacement(
  inputType: string | undefined,
  data?: string | null,
  isMobileLike?: boolean,
): boolean {
  void data;
  void isMobileLike;
  return (
    inputType === "insertReplacementText" ||
    inputType === "insertFromComposition" ||
    inputType === "insertCompositionText"
  );
}

export function updatePtyInputLine(currentLine: string, data: string): string {
  // Escape sequences (arrow keys, home/end, function keys, paste guards)
  // move the cursor or edit the line in ways this flat tracker cannot
  // model — and the per-char loop below would append their printable
  // payload (e.g. the "[D" of a left-arrow) as if it were typed text.
  // Reset instead: an unknown cursor position must disarm replacement
  // normalization until the user starts a fresh, cleanly-tracked line.
  if (data.includes("\x1b")) {
    return "";
  }
  let next = currentLine;
  for (const ch of chars(data)) {
    if (ch === "\r" || ch === "\n") {
      next = "";
    } else if (ch === DELETE || ch === "\b") {
      next = removeLastChar(next);
    } else if (ch === "\x15") {
      next = "";
    } else if (isPlainText(ch)) {
      next += ch;
    }
  }
  return next;
}

export function normalizePtyMobileInput(
  data: string,
  currentLine: string,
  replacementActive: boolean,
): { data: string; nextLine: string; normalized: boolean } {
  if (replacementActive && isPlainText(data)) {
    const replacementLine = replacementLineForMobileInput(currentLine, data);
    if (replacementLine !== null) {
      return {
        data: DELETE.repeat(chars(currentLine).length) + replacementLine,
        nextLine: replacementLine,
        normalized: true,
      };
    }
  }

  return {
    data,
    nextLine: updatePtyInputLine(currentLine, data),
    normalized: false,
  };
}

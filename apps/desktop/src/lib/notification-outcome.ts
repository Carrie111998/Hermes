/** A compact, deterministic completion hook for OS notifications. This is not
 * LLM summarization: it preserves the first useful outcome sentence without
 * sending a whole answer, transcript, or sensitive tool trace to the lockscreen. */
export function notificationOutcomeSummary(value: string, maxLength = 140): string {
  const clean = value
    .replace(/[`*_>#]/g, '')
    .replace(/\s+/g, ' ')
    .trim()

  if (!clean) return ''

  const firstSentence = clean.match(/^.+?(?:[.!?](?:\s|$)|$)/)?.[0]?.trim() || clean
  const clipped = firstSentence.length > maxLength ? `${firstSentence.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…` : firstSentence
  return `Outcome: ${clipped}`
}

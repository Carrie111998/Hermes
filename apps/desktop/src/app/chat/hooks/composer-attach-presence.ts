/**
 * Mounted-composer presence — lets non-React senders (host.attachFileToComposer)
 * choose between the live attachment atom and the per-session draft stash.
 *
 * Counted, not boolean: React strict-mode double-mounts and the brief overlap
 * during route transitions mean mark/unmark pairs can interleave.
 */

const mounted = new Map<string, number>()

export function markComposerMounted(target: string): () => void {
  mounted.set(target, (mounted.get(target) ?? 0) + 1)

  let released = false

  return () => {
    if (released) {
      return
    }

    released = true
    const next = (mounted.get(target) ?? 1) - 1

    if (next <= 0) {
      mounted.delete(target)
    } else {
      mounted.set(target, next)
    }
  }
}

export function isComposerMounted(target: string): boolean {
  return (mounted.get(target) ?? 0) > 0
}

/**
 * Known-benign subprocess stderr noise for the Desktop backend spawner
 * (issue #54833).
 *
 * macOS children of Hermes occasionally print the libmalloc teardown line
 * "MallocStackLogging: can't turn off malloc stack logging because it was
 * not enabled." on exit. It is harmless, but it was reaching desktop.log and
 * boot-failure tails through the backend stderr listeners. This is the
 * Electron mirror of hermes_cli/subprocess_noise.py — same exact-match
 * contract, same Darwin-only rule, stdout is never filtered.
 *
 * Unlike the Python predicate, the sink here is stateful: Node delivers
 * streams as byte chunks that do not align with lines, so a match can be
 * split across two 'data' events. The sink holds back an unterminated
 * trailing line until the next chunk or until the stream ends.
 */

const BENIGN_MALLOC_STACK_LOGGING =
  /^(?:[Pp]ython\([0-9]+\) )?MallocStackLogging: can't turn off malloc stack logging because it was not enabled\.$/

export function isBenignDarwinMallocStackLoggingLine(
  line,
  platform = process.platform
) {
  if (platform !== 'darwin') {
    return false
  }

  return BENIGN_MALLOC_STACK_LOGGING.test(line)
}

/** Whole-line filter over a complete string (test/reference surface). */
export function filterBenignDarwinSubprocessStderr(text, platform = process.platform) {
  if (platform !== 'darwin' || !text || !text.includes('MallocStackLogging')) {
    return text
  }

  return text
    .split(/(?<=\n)/)
    .filter(chunk => !isBenignDarwinMallocStackLoggingLine(chunk.replace(/\r?\n$/, ''), platform))
    .join('')
}

/**
 * Wrap a stderr 'data' consumer in a line-boundary-aware filter.
 *
 * Returns a listener to attach in place of the raw consumer. Lines that end
 * within the delivered chunks are filtered exactly; the unterminated tail is
 * buffered and prepended to the next chunk, and `.flush()` (wire to
 * 'end'/'close') forwards whatever remains so nothing is ever swallowed.
 */
export function createBenignStderrSink(consumer, platform = process.platform) {
  let pending = ''

  const emit = text => {
    if (text) {
      consumer(text)
    }
  }

  const listener = chunk => {
    const text = pending + String(chunk)
    pending = ''

    if (platform !== 'darwin') {
      // Non-Darwin is a byte-for-byte pass-through; skip line work entirely.
      emit(text)
      return
    }

    const lastBreak = Math.max(
      text.lastIndexOf('\n'),
      text.endsWith('\r') ? text.length - 1 : -1
    )

    if (lastBreak < 0) {
      pending = text
      return
    }

    const complete = text.slice(0, lastBreak + 1)
    pending = text.slice(lastBreak + 1)

    const lines = complete.split(/(?<=\n)/)
    let out = ''

    for (const line of lines) {
      if (!isBenignDarwinMallocStackLoggingLine(line.replace(/\r?\n$/, ''), platform)) {
        out += line
      }
    }

    emit(out)
  }

  listener.flush = () => {
    const rest = pending
    pending = ''

    if (rest && platform === 'darwin') {
      const bare = rest.replace(/\r?\n$/, '')

      return isBenignDarwinMallocStackLoggingLine(bare, platform) ? '' : rest
    }

    return rest
  }

  return listener
}

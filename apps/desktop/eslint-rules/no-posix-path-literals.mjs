/**
 * Ban the POSIX-path-literal defect from tests that run on the Windows lane.
 *
 * The same bug was found three times in three files: a hard-coded absolute
 * POSIX path on one side of a comparison whose other side is built with
 * `path.join`. On Windows `path.join` emits backslashes and `path.resolve`
 * prepends a drive letter, so the two sides can never be equal and the
 * assertion is dead on that platform — silently, because the file had only
 * ever run on Linux.
 *
 *   #177  update-relaunch.test.ts     const ROOT = '/home/u/.hermes/hermes-agent'
 *   #177  windows-hermes-path.test.ts p => p === '/venv/lib/python3.12/site-packages'
 *   #180  ssh-connection.test.ts      assert.match(a, /\/[0-9a-f]{16}\.sock$/)
 *
 * Each was found by hand after the lane went red (or, for the third, by
 * reading the file before adding it). This rule is the cheaper version of
 * that search.
 *
 * ── Why these three shapes and not "no leading-slash literals" ──────────────
 *
 * A blanket ban is unusable: the seven lane files contain 71 absolute POSIX
 * string literals, and almost all are *inputs* — a fake `execPath`, a
 * ControlPath handed to ssh, a path the stub filesystem is asked about. Those
 * are fine; the code under test is what has to cope with them. The defect
 * only appears where a literal meets a computed path, so the rule keys on
 * that meeting rather than on the literal alone:
 *
 *   joined-root   `path.join(X, …)` where X is, or is a const bound to, an
 *                 absolute POSIX literal. Joining onto a driveless root gives
 *                 a value that can never equal a `path.resolve`d one.
 *                 Fix: `path.resolve('/…')` — a no-op on POSIX.
 *
 *   comparison    `x === '/a/b'`. Covers both a bare `===` in a stub predicate
 *                 and the expected-value slot of an assertion, since that is
 *                 the same comparison written by a helper.
 *
 *   regex         `assert.match(x, /\/…/)` where the pattern opens with a path
 *                 separator, which only a POSIX rendering can match.
 *                 Fix: assert on `path.basename(x)`, or build with `path.sep`.
 *
 * Single-segment literals ('/tmp', '/') are ignored — a lone root is usually
 * an opaque token rather than a path that gets joined and compared.
 *
 * ── Scope ───────────────────────────────────────────────────────────────────
 *
 * Applied only to the suites named in `test:desktop:win-install`, and that
 * list is read from package.json rather than restated here, so adding a suite
 * to the Windows lane turns the rule on for it in the same edit. Files that
 * run only on Linux keep their POSIX literals, which is the honest outcome:
 * this is a cross-platform-correctness rule, not a style rule.
 *
 * A legitimately-POSIX value in a lane file (a remote shell command — remotes
 * are always POSIX) takes an eslint-disable-next-line with the reason.
 */

// Absolute, with at least one interior separator: '/a/b' yes, '/tmp' no.
const ABSOLUTE_POSIX = /^\/[^/\s]+\//

// A pattern that opens with a separator, optionally behind a start anchor.
const LEADING_SEPARATOR = /^\^?\\?\//

const COMPARISONS = new Set(['===', '!==', '==', '!='])

const ASSERT_EQUAL = new Set([
  'deepEqual',
  'deepStrictEqual',
  'equal',
  'notDeepStrictEqual',
  'notEqual',
  'notStrictEqual',
  'strictEqual',
  'toBe',
  'toEqual',
  'toStrictEqual'
])

const ASSERT_MATCH = new Set(['doesNotMatch', 'match', 'toMatch'])

/**
 * Which argument holds the *expected* value.
 *
 * `assert.equal(actual, expected)` puts it second; `expect(actual).toBe(expected)`
 * puts it first, because the actual value went to `expect()`. Getting this
 * wrong is silent — the rule simply stops firing on one of the two styles.
 */
function expectedArgumentIndex(callee) {
  const isExpectChain =
    callee.object?.type === 'CallExpression' && callee.object.callee?.name === 'expect'

  return isExpectChain ? 0 : 1
}

function isPosixPathLiteral(node) {
  return (
    node?.type === 'Literal' &&
    typeof node.value === 'string' &&
    ABSOLUTE_POSIX.test(node.value)
  )
}

// `path.join(...)` — but not `path.win32.join` / `path.posix.join`, where the
// flavour is stated and a matching literal is deliberate.
function isPathJoin(node) {
  const callee = node.callee

  if (callee?.type === 'Identifier') {
    return callee.name === 'join'
  }

  if (callee?.type !== 'MemberExpression' || callee.computed) {
    return false
  }

  return callee.property.name === 'join' && callee.object.type === 'Identifier' && callee.object.name === 'path'
}

// One hop through a `const` binding, which is how a shared fixture root is
// always written. Deliberately not a general dataflow analysis.
function resolveConstInit(node, scope) {
  if (node?.type !== 'Identifier') {
    return node
  }

  for (let s = scope; s; s = s.upper) {
    const variable = s.variables.find(v => v.name === node.name)

    if (!variable) {
      continue
    }

    if (variable.defs.length !== 1) {
      return node
    }

    const def = variable.defs[0]

    if (def.type === 'Variable' && def.parent?.kind === 'const' && def.node.init) {
      return def.node.init
    }

    return node
  }

  return node
}

export default {
  meta: {
    docs: {
      description:
        'Disallow absolute POSIX path literals where they meet a computed path, in tests that run on Windows'
    },
    messages: {
      comparison:
        'Comparing against the absolute POSIX literal "{{value}}". If the other side is built with path.join it can never match on Windows — build the expectation with path.join too, or assert on path.basename.',
      joinedRoot:
        'path.join() onto the absolute POSIX root "{{value}}". path.resolve prepends a drive on Windows, so a driveless root can never equal a resolved path — wrap the root in path.resolve (a no-op on POSIX).',
      regex:
        'This pattern opens with a path separator, so it can only match a POSIX rendering. Assert on path.basename(), or build the separator with path.sep.'
    },
    schema: [],
    type: 'problem'
  },

  create(context) {
    const { sourceCode } = context

    function report(node, messageId, value) {
      context.report({ data: value === undefined ? {} : { value }, messageId, node })
    }

    return {
      BinaryExpression(node) {
        if (!COMPARISONS.has(node.operator)) {
          return
        }

        for (const side of [node.left, node.right]) {
          if (isPosixPathLiteral(side)) {
            report(side, 'comparison', side.value)
          }
        }
      },

      CallExpression(node) {
        if (isPathJoin(node) && node.arguments.length > 0) {
          const first = resolveConstInit(node.arguments[0], sourceCode.getScope(node))

          if (isPosixPathLiteral(first)) {
            report(node.arguments[0], 'joinedRoot', first.value)
          }
        }

        // assert.equal(actual, '/a/b') and friends: the same comparison, via a
        // helper. Only the expected slot — the actual slot is an input.
        const callee = node.callee

        if (callee?.type === 'MemberExpression' && !callee.computed) {
          const name = callee.property.name
          const expected = node.arguments[expectedArgumentIndex(callee)]

          if (ASSERT_EQUAL.has(name) && isPosixPathLiteral(expected)) {
            report(expected, 'comparison', expected.value)
          }

          if (ASSERT_MATCH.has(name) && expected?.regex && LEADING_SEPARATOR.test(expected.regex.pattern)) {
            report(expected, 'regex')
          }
        }
      }
    }
  }
}

import { RuleTester } from 'eslint'
import tseslint from 'typescript-eslint'
import { describe, it } from 'vitest'

import rule from './no-posix-path-literals.mjs'

// `tseslint.parser`, the same handle the shared config uses, rather than
// reaching for @typescript-eslint/parser — which is only a transitive here.

// RuleTester drives `describe`/`it` off globals; vitest's are imported here
// because this project does not run with `globals: true`.
RuleTester.describe = describe
RuleTester.it = it

const ruleTester = new RuleTester({
  languageOptions: {
    parser: tseslint.parser,
    parserOptions: { ecmaVersion: 'latest', sourceType: 'module' }
  }
})

ruleTester.run('no-posix-path-literals', rule, {
  invalid: [
    // ── The three real defects, in the form they were actually committed ────
    //
    // Reconstructed from the pre-fix files rather than invented, so a change
    // that stops catching them fails here. Full-file positive control: running
    // this rule over `1770376^` and `50e2b2b^` reports exactly these and
    // nothing else.
    {
      // #177, update-relaunch.test.ts — a driveless root, then joined onto.
      code: `
        const ROOT = '/home/u/.hermes/hermes-agent'
        const UNPACKED = path.join(ROOT, 'apps', 'desktop', 'release', 'linux-unpacked')
      `,
      errors: [{ messageId: 'joinedRoot' }]
    },
    {
      // #177, windows-hermes-path.test.ts — a stub predicate comparing against
      // a path the implementation builds with the host separator.
      code: `const deps = { directoryExists: p => p === '/venv/lib/python3.12/site-packages' }`,
      errors: [{ messageId: 'comparison' }]
    },
    {
      // #180, ssh-connection.test.ts — a pattern that opens with a separator.
      code: String.raw`assert.match(a, /\/[0-9a-f]{16}\.sock$/)`,
      errors: [{ messageId: 'regex' }]
    },

    // ── The same shapes, generalised ───────────────────────────────────────
    { code: `const x = path.join('/srv/app', 'bin')`, errors: [{ messageId: 'joinedRoot' }] },
    { code: `const x = join('/srv/app', 'bin')`, errors: [{ messageId: 'joinedRoot' }] },
    { code: `if (p !== '/srv/app/bin') { fail() }`, errors: [{ messageId: 'comparison' }] },
    { code: `assert.equal(actual, '/srv/app/bin')`, errors: [{ messageId: 'comparison' }] },
    { code: `assert.deepEqual(actual, '/srv/app/bin')`, errors: [{ messageId: 'comparison' }] },
    { code: `expect(actual).toBe('/srv/app/bin')`, errors: [{ messageId: 'comparison' }] },
    { code: String.raw`assert.match(p, /^\/srv\/app/)`, errors: [{ messageId: 'regex' }] },

    // Both sides of one comparison are reported, because both are wrong.
    {
      code: `const same = '/a/b/c' === '/a/b/c'`,
      errors: [{ messageId: 'comparison' }, { messageId: 'comparison' }]
    }
  ],

  valid: [
    // ── The fixes that were actually applied ───────────────────────────────
    `
      const ROOT = path.resolve('/home/u/.hermes/hermes-agent')
      const UNPACKED = path.join(ROOT, 'apps', 'desktop', 'release', 'linux-unpacked')
    `,
    `const expected = path.join('/venv', 'lib', 'python3.12', 'site-packages')
     const deps = { directoryExists: p => p === expected }`,
    String.raw`assert.match(path.basename(a), /^[0-9a-f]{16}\.sock$/)`,

    // ── Inputs, which are the overwhelming majority and must stay quiet ────
    //
    // The seven lane files hold 71 absolute POSIX literals; all but the three
    // above are values handed *to* the code under test, which is exactly what
    // a path-handling implementation is supposed to cope with.
    `resolveVenvHermesCommand('/root/venv/Scripts/hermes.exe', [], deps)`,
    `const deps = makeDeps({ hermesHome: '/fake/hermes-home' })`,
    `startSsh({ controlPath: '/tmp/x.sock' })`,

    // A single-segment root is a token, not a path that gets joined.
    `const x = path.join('/tmp', 'a')`,
    `assert.equal(dirname('/a'), '/')`,

    // A stated flavour means the literal is deliberate.
    `const x = path.posix.join('/srv/app', 'bin')`,
    `const x = path.win32.join('/srv/app', 'bin')`,

    // Not paths at all.
    `assert.equal(url, 'https://example.com/a/b')`,
    `const x = path.join(base, '/srv/app/bin')`,

    // A shebang check is content, not a path — the pattern must open with the
    // separator, not merely contain one.
    String.raw`assert.match(script, /^#!\/bin\/bash/)`,
    String.raw`assert.match(cmd, /^cd '\/home\/me\/project' 2>\/dev\/null$/)`,

    // A reassigned binding is not followed: one hop through a const is the
    // whole of the analysis, and guessing past that would misreport.
    `let root = '/srv/app/bin'
     root = process.cwd()
     const x = path.join(root, 'bin')`
  ]
})

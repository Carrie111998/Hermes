import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  REQUIRED_MANAGED_SCENARIOS,
  verifyManagedReports,
} from './verify-managed-reports.mjs'

const HEAD = 'a'.repeat(40)

function scenarioXml(overrides = {}) {
  const names = overrides.names ?? REQUIRED_MANAGED_SCENARIOS
  const summary = {
    tests: names.length,
    pass: names.length,
    fail: 0,
    cancelled: 0,
    skipped: 0,
    todo: 0,
    ...overrides.summary,
  }
  return `<?xml version="1.0" encoding="utf-8"?>
<testsuites>
${names.map(name => `  <testcase name="${name}"/>`).join('\n')}
${Object.entries(summary).map(([key, value]) => `  <!-- ${key} ${value} -->`).join('\n')}
</testsuites>
`
}

function playwrightReport(overrides = {}) {
  const testEntry = {
    status: 'expected',
    annotations: [],
    results: [{ status: 'passed' }],
    ...overrides.testEntry,
  }
  return {
    stats: {
      expected: 1,
      unexpected: 0,
      flaky: 0,
      skipped: 0,
      ...overrides.stats,
    },
    suites: [{ specs: [{ tests: [testEntry] }] }],
  }
}

function fixture(t, overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-report-verifier-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const scenarioReportPath = path.join(directory, 'managed-scenarios.xml')
  const playwrightReportPath = path.join(directory, 'results.json')
  if (overrides.scenario !== null) {
    fs.writeFileSync(scenarioReportPath, overrides.scenario ?? scenarioXml(), 'utf8')
  }
  if (overrides.playwright !== null) {
    fs.writeFileSync(
      playwrightReportPath,
      JSON.stringify(overrides.playwright ?? playwrightReport()),
      'utf8',
    )
  }
  return { playwrightReportPath, scenarioReportPath }
}

test('accepts complete deterministic reports at the exact candidate head', t => {
  const files = fixture(t)
  const result = verifyManagedReports({
    ...files,
    actualHead: HEAD,
    expectedHead: HEAD,
  })
  assert.deepEqual(result.requiredScenarios, REQUIRED_MANAGED_SCENARIOS)
  assert.equal(result.scenarioTests, 4)
  assert.equal(result.playwright.testCount, 1)
})

test('rejects missing reports and a missing required scenario', t => {
  const missingScenario = fixture(t, { scenario: null })
  assert.throws(
    () => verifyManagedReports({ ...missingScenario, actualHead: HEAD, expectedHead: HEAD }),
    /scenario report is missing or unreadable/i,
  )

  const missingPlaywright = fixture(t, { playwright: null })
  assert.throws(
    () => verifyManagedReports({ ...missingPlaywright, actualHead: HEAD, expectedHead: HEAD }),
    /Playwright report is missing or unreadable/i,
  )

  const omitted = fixture(t, {
    scenario: scenarioXml({ names: REQUIRED_MANAGED_SCENARIOS.slice(1) }),
  })
  assert.throws(
    () => verifyManagedReports({ ...omitted, actualHead: HEAD, expectedHead: HEAD }),
    /managed\.signin-assignment-chat executed 0 time/i,
  )
})

test('rejects skipped, fixme, unexpected, and flaky results', t => {
  const cases = [
    {
      label: 'scenario skip',
      files: fixture(t, {
        scenario: scenarioXml({ summary: { pass: 3, skipped: 1 } }),
      }),
      pattern: /contains 1 skipped test/i,
    },
    {
      label: 'Playwright fixme',
      files: fixture(t, {
        playwright: playwrightReport({
          stats: { expected: 0, skipped: 1 },
          testEntry: {
            status: 'skipped',
            annotations: [{ type: 'fixme' }],
            results: [{ status: 'skipped' }],
          },
        }),
      }),
      pattern: /contains no passing test|skipped test/i,
    },
    {
      label: 'Playwright unexpected',
      files: fixture(t, {
        playwright: playwrightReport({
          stats: { expected: 0, unexpected: 1 },
          testEntry: { status: 'unexpected', results: [{ status: 'failed' }] },
        }),
      }),
      pattern: /contains no passing test|unexpected test/i,
    },
    {
      label: 'Playwright flaky',
      files: fixture(t, {
        playwright: playwrightReport({
          stats: { expected: 0, flaky: 1 },
          testEntry: {
            status: 'flaky',
            results: [{ status: 'failed' }, { status: 'passed' }],
          },
        }),
      }),
      pattern: /contains no passing test|flaky test/i,
    },
  ]

  for (const item of cases) {
    assert.throws(
      () => verifyManagedReports({ ...item.files, actualHead: HEAD, expectedHead: HEAD }),
      item.pattern,
      item.label,
    )
  }
})

test('rejects the wrong checkout head and any retry attempt', t => {
  const files = fixture(t)
  assert.throws(
    () =>
      verifyManagedReports({
        ...files,
        actualHead: 'b'.repeat(40),
        expectedHead: HEAD,
      }),
    /expected candidate head/i,
  )

  const retried = fixture(t, {
    playwright: playwrightReport({
      testEntry: {
        status: 'expected',
        results: [{ status: 'failed' }, { status: 'passed' }],
      },
    }),
  })
  assert.throws(
    () => verifyManagedReports({ ...retried, actualHead: HEAD, expectedHead: HEAD }),
    /2 attempt\(s\); retries are forbidden/i,
  )
})

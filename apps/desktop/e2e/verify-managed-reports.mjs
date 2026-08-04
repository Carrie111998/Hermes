import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const REQUIRED_MANAGED_SCENARIOS = Object.freeze([
  'managed.signin-assignment-chat',
  'managed.openai-reauth-profile',
  'managed.ws-voice-plugin-profile',
  'managed.updater-handoff-preservation',
])

function readRequiredFile(filePath, label) {
  try {
    return fs.readFileSync(filePath, 'utf8')
  } catch (error) {
    throw new Error(`${label} is missing or unreadable: ${filePath}`, { cause: error })
  }
}

function requireZero(summary, key) {
  if (summary[key] !== 0) {
    throw new Error(`Managed scenario report contains ${summary[key]} ${key} test(s).`)
  }
}

function verifyScenarioReport(filePath) {
  const xml = readRequiredFile(filePath, 'Managed scenario report')
  const summary = Object.fromEntries(
    [...xml.matchAll(/<!--\s*(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*-->/g)]
      .map(match => [match[1], Number(match[2])]),
  )
  for (const key of ['tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo']) {
    if (!Number.isInteger(summary[key])) {
      throw new Error(`Managed scenario report is missing its ${key} summary.`)
    }
  }
  requireZero(summary, 'fail')
  requireZero(summary, 'cancelled')
  requireZero(summary, 'skipped')
  requireZero(summary, 'todo')
  if (summary.tests !== summary.pass) {
    throw new Error(
      `Managed scenario report is incomplete (tests=${summary.tests}, pass=${summary.pass}).`,
    )
  }

  const names = [...xml.matchAll(/<testcase\b[^>]*\bname="([^"]+)"/g)].map(match => match[1])
  for (const scenario of REQUIRED_MANAGED_SCENARIOS) {
    const occurrences = names.filter(name => name === scenario).length
    if (occurrences !== 1) {
      throw new Error(
        `Required managed scenario ${scenario} executed ${occurrences} time(s); expected exactly once.`,
      )
    }
  }
  return { names, summary }
}

function collectSpecs(suites, specs = []) {
  for (const suite of suites ?? []) {
    specs.push(...(suite.specs ?? []))
    collectSpecs(suite.suites, specs)
  }
  return specs
}

function verifyPlaywrightReport(filePath) {
  const raw = readRequiredFile(filePath, 'Managed Playwright report')
  let report
  try {
    report = JSON.parse(raw)
  } catch (error) {
    throw new Error(`Managed Playwright report is invalid JSON: ${filePath}`, { cause: error })
  }

  const stats = report.stats ?? {}
  for (const key of ['expected', 'unexpected', 'flaky', 'skipped']) {
    if (!Number.isInteger(Number(stats[key]))) {
      throw new Error(`Managed Playwright report is missing numeric stats.${key}.`)
    }
  }
  if (Number(stats.expected) < 1) {
    throw new Error('Managed Playwright report contains no passing test.')
  }
  for (const key of ['unexpected', 'flaky', 'skipped']) {
    if (Number(stats[key]) !== 0) {
      throw new Error(`Managed Playwright report contains ${stats[key]} ${key} test(s).`)
    }
  }

  const tests = collectSpecs(report.suites).flatMap(spec => spec.tests ?? [])
  if (tests.length < 1) {
    throw new Error('Managed Playwright report contains no test results.')
  }
  for (const entry of tests) {
    if (entry.status !== 'expected') {
      throw new Error(`Managed Playwright test has non-passing status ${String(entry.status)}.`)
    }
    const forbiddenAnnotation = (entry.annotations ?? []).find(annotation =>
      ['fixme', 'skip'].includes(String(annotation?.type).toLowerCase()),
    )
    if (forbiddenAnnotation) {
      throw new Error(
        `Managed Playwright test contains forbidden ${forbiddenAnnotation.type} annotation.`,
      )
    }
    if (!Array.isArray(entry.results) || entry.results.length !== 1) {
      throw new Error(
        `Managed Playwright test executed ${entry.results?.length ?? 0} attempt(s); retries are forbidden.`,
      )
    }
    if (entry.results[0]?.status !== 'passed') {
      throw new Error(
        `Managed Playwright test attempt has non-passing status ${String(entry.results[0]?.status)}.`,
      )
    }
  }
  return { stats, testCount: tests.length }
}

export function verifyManagedReports({
  scenarioReportPath,
  playwrightReportPath,
  expectedHead,
  actualHead,
}) {
  if (!expectedHead || !/^[0-9a-f]{40}$/i.test(expectedHead)) {
    throw new Error('MANAGED_EXPECTED_HEAD must be an exact 40-character commit SHA.')
  }
  const resolvedActualHead = actualHead ?? execFileSync('git', ['rev-parse', 'HEAD'], {
    encoding: 'utf8',
  }).trim()
  if (resolvedActualHead !== expectedHead) {
    throw new Error(
      `Managed CI checked out ${resolvedActualHead}; expected candidate head ${expectedHead}.`,
    )
  }

  const scenarios = verifyScenarioReport(scenarioReportPath)
  const playwright = verifyPlaywrightReport(playwrightReportPath)
  return {
    actualHead: resolvedActualHead,
    expectedHead,
    playwright: {
      expected: Number(playwright.stats.expected),
      testCount: playwright.testCount,
    },
    requiredScenarios: REQUIRED_MANAGED_SCENARIOS,
    scenarioTests: scenarios.summary.tests,
  }
}

function main() {
  const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const reportRoot = path.join(desktopRoot, 'playwright-report')
  const verification = verifyManagedReports({
    scenarioReportPath:
      process.env.MANAGED_SCENARIO_REPORT ??
      path.join(reportRoot, 'managed-scenarios.xml'),
    playwrightReportPath:
      process.env.MANAGED_PLAYWRIGHT_REPORT ??
      path.join(reportRoot, 'results.json'),
    expectedHead: process.env.MANAGED_EXPECTED_HEAD,
  })
  const outputPath =
    process.env.MANAGED_VERIFICATION_REPORT ??
    path.join(reportRoot, 'managed-verification.json')
  fs.writeFileSync(outputPath, `${JSON.stringify(verification, null, 2)}\n`, 'utf8')
  console.log(
    `Verified ${verification.requiredScenarios.length} managed behavioral scenarios and ` +
      `${verification.playwright.testCount} Playwright test result(s) at ${verification.actualHead}.`,
  )
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main()
  } catch (error) {
    console.error(`::error::${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  }
}

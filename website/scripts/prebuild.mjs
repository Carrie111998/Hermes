#!/usr/bin/env node
// Runs website/scripts/extract-skills.py and generate-llms-txt.py before
// docusaurus build/start so that:
//   - website/static/api/skills.json (lazy-fetched by src/pages/skills/index.tsx)
//   - website/static/api/skills-meta.json (sidecar metadata for the Skills Hub)
//   - website/static/llms.txt (agent-friendly short docs index)
//   - website/static/llms-full.txt (full docs concat for LLM context)
// all exist without contributors remembering to run Python scripts manually.
// CI workflows still run the extraction explicitly, which is a no-op duplicate
// but matches their historical behaviour.
//
// We also try to pull a fresh copy of skills-index.json (the unified
// multi-source catalog) from the live docs site if it's not already on disk.
// That way local `npm run build` doesn't have to wait on
// scripts/build_skills_index.py crawling every skill source — which takes
// several minutes and burns GitHub API quota — but still gets the same
// 2000+ external skills the deployed site has.
//
// Production generation (`npm run generate`, build, and deploy) passes
// `--strict` and fails closed if required artifacts cannot be refreshed.
// `npm start` deliberately uses the best-effort mode for local preview: it
// preserves an existing artifact, or writes an empty skills.json only when
// none exists, and warns instead of aborting.

import { spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync, existsSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const websiteDir = resolve(scriptDir, '..')
const repoDir = resolve(websiteDir, '..')
const extractScript = join(scriptDir, 'extract-skills.py')
const llmsScript = join(scriptDir, 'generate-llms-txt.py')
const cronBlueprintsScript = join(scriptDir, 'extract-automation-blueprints.py')
const outputFile = join(websiteDir, 'static', 'api', 'skills.json')
const unifiedIndexFile = join(websiteDir, 'static', 'api', 'skills-index.json')
const UNIFIED_INDEX_URL = 'https://hermes-agent.nousresearch.com/docs/api/skills-index.json'
const UNIFIED_INDEX_MAX_AGE_MS = 24 * 60 * 60 * 1000 // 24h
const strict = process.argv.includes('--strict')

function resolvePython() {
  const venvExecutable = process.platform === 'win32' ? 'python.exe' : 'python3'
  const venvBin = process.platform === 'win32' ? 'Scripts' : 'bin'
  const candidates = [
    process.env.PYTHON,
    join(repoDir, '.venv', venvBin, venvExecutable),
    join(repoDir, 'venv', venvBin, venvExecutable),
    'python3',
    'python'
  ].filter((candidate, index, all) => candidate && all.indexOf(candidate) === index)

  for (const candidate of candidates) {
    if (candidate.includes('/') || candidate.includes('\\')) {
      if (!existsSync(candidate)) continue
    }
    const probe = spawnSync(candidate, ['-c', 'import yaml'], { stdio: 'ignore' })
    if (!probe.error && probe.status === 0) return candidate
  }
  return null
}

const pythonCommand = resolvePython()

function failOrWarn(message) {
  if (strict) {
    throw new Error(`[prebuild] ${message}`)
  }

  console.warn(`[prebuild] ${message}`)
  return false
}

function handleExtractFailure(reason) {
  if (strict) {
    return failOrWarn(`extract-skills.py failed (${reason})`)
  }

  mkdirSync(dirname(outputFile), { recursive: true })
  const wroteFallback = !existsSync(outputFile)

  if (wroteFallback) {
    writeFileSync(outputFile, '[]\n')
  }

  console.warn(
    `[prebuild] extract-skills.py skipped (${reason}); ` +
      `${wroteFallback ? 'wrote empty' : 'preserved existing'} skills.json. ` +
      'Install python3 + pyyaml locally for a populated Skills Hub page.'
  )

  return false
}

function skillsOutputIsValid() {
  try {
    const parsed = JSON.parse(readFileSync(outputFile, 'utf8'))
    return Array.isArray(parsed) && parsed.length > 0
  } catch {
    return false
  }
}

function runPython(script, label) {
  if (!existsSync(script)) {
    return failOrWarn(`${label} skipped (script missing)`)
  }
  if (!pythonCommand) {
    return failOrWarn(`${label} skipped (python3 + pyyaml not found)`)
  }
  const r = spawnSync(pythonCommand, [script], { stdio: 'inherit', cwd: websiteDir })
  if (r.error || r.status !== 0) {
    const detail = r.error?.message || (r.signal ? `signal ${r.signal}` : `status ${r.status}`)
    return failOrWarn(`${label} exited with ${detail}`)
  }
  return true
}

function readValidUnifiedIndex() {
  try {
    const parsed = JSON.parse(readFileSync(unifiedIndexFile, 'utf8'))
    return Boolean(parsed && Array.isArray(parsed.skills) && parsed.skills.length > 0)
  } catch {
    return false
  }
}

async function ensureUnifiedIndex() {
  const validLocalCopy = readValidUnifiedIndex()

  // If we have a recent valid copy on disk, trust it.
  if (existsSync(unifiedIndexFile)) {
    try {
      const age = Date.now() - statSync(unifiedIndexFile).mtimeMs
      if (validLocalCopy && age < UNIFIED_INDEX_MAX_AGE_MS) {
        return true
      }

      if (validLocalCopy) {
        console.log(
          `[prebuild] skills-index.json is ${(age / 3600000).toFixed(1)}h old; ` +
            `refreshing from ${UNIFIED_INDEX_URL}`
        )
      } else {
        console.warn('[prebuild] local skills-index.json is invalid; fetching a replacement')
      }
    } catch {
      // fall through to re-fetch
    }
  }

  try {
    const resp = await fetch(UNIFIED_INDEX_URL, {
      headers: { accept: 'application/json' }
    })
    if (!resp.ok) {
      console.warn(
        `[prebuild] skills-index.json fetch returned HTTP ${resp.status}; ` +
          `${strict ? 'strict mode will not accept a stale copy' : 'using a non-empty local copy if any'}`
      )
      return !strict && validLocalCopy
    }
    const text = await resp.text()
    // Sanity check: must be valid JSON with a non-empty skills array.
    try {
      const parsed = JSON.parse(text)
      if (!parsed || !Array.isArray(parsed.skills) || parsed.skills.length === 0) {
        console.warn('[prebuild] skills-index.json from live site has no non-empty skills array; ignoring')
        return !strict && validLocalCopy
      }
    } catch (e) {
      console.warn(`[prebuild] skills-index.json from live site is not valid JSON: ${e}`)
      return !strict && validLocalCopy
    }
    mkdirSync(dirname(unifiedIndexFile), { recursive: true })
    writeFileSync(unifiedIndexFile, text)
    console.log(
      `[prebuild] downloaded skills-index.json from ${UNIFIED_INDEX_URL} ` + `(${(text.length / 1024).toFixed(0)} KB)`
    )
    return true
  } catch (e) {
    console.warn(`[prebuild] skills-index.json fetch failed: ${e}`)
    return !strict && validLocalCopy
  }
}

// 0) Pull unified index if we don't have a fresh one.
if (!(await ensureUnifiedIndex())) {
  failOrWarn('skills-index.json is unavailable or invalid')
}

// 1) skills.json — required for the Skills Hub page.
if (!existsSync(extractScript)) {
  handleExtractFailure('extract script missing')
} else {
  const r = pythonCommand
    ? spawnSync(pythonCommand, [extractScript], {
        stdio: 'inherit',
        cwd: websiteDir
      })
    : null
  if (!r) {
    handleExtractFailure('python3 + pyyaml not found')
  } else if (r.error || r.status !== 0) {
    const detail = r.error?.message || (r.signal ? `signal ${r.signal}` : `status ${r.status}`)
    handleExtractFailure(`extract-skills.py exited with ${detail}`)
  } else if (!skillsOutputIsValid()) {
    handleExtractFailure('output is missing, invalid, or empty')
  }
}

// 2) llms.txt + llms-full.txt — required in strict builds, best-effort for local preview.
runPython(llmsScript, 'generate-llms-txt.py')

// 3) automation-blueprints-index.json — required in strict builds. Best-effort
//    local preview still renders an empty state if the generator cannot run.
runPython(cronBlueprintsScript, 'extract-automation-blueprints.py')

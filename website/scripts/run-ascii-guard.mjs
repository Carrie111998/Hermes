#!/usr/bin/env node

import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const websiteDir = resolve(scriptDir, '..')
const repoDir = resolve(websiteDir, '..')
const executable = process.platform === 'win32' ? 'ascii-guard.exe' : 'ascii-guard'
const binDir = process.platform === 'win32' ? 'Scripts' : 'bin'
const candidates = [
  join(repoDir, '.venv', binDir, executable),
  join(repoDir, 'venv', binDir, executable),
  'ascii-guard'
].filter((candidate, index, all) => candidate && all.indexOf(candidate) === index)

for (const candidate of candidates) {
  const explicitPath = candidate.includes('/') || candidate.includes('\\')
  if (explicitPath && !existsSync(candidate)) continue

  const result = spawnSync(candidate, process.argv.slice(2), {
    cwd: websiteDir,
    env: process.env,
    stdio: 'inherit'
  })

  if (result.error?.code === 'ENOENT') continue
  if (result.error) {
    console.error(`Unable to start ascii-guard: ${result.error.message}`)
    process.exit(1)
  }
  process.exit(result.status ?? 1)
}

console.error(
  'ascii-guard was not found on PATH or in the repository .venv/venv. ' +
    'Install ascii-guard==2.3.0 before running diagram lint.'
)
process.exit(1)

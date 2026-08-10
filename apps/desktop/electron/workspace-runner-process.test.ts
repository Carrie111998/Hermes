import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, test } from 'vitest'

import { WorkspaceRunnerProcess } from './workspace-runner-process'

const repoRoot = path.resolve(import.meta.dirname, '../../..')

const pythonCandidates = [
  process.env.HERMES_TEST_PYTHON,
  process.env.VIRTUAL_ENV && path.join(process.env.VIRTUAL_ENV, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  path.join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
].filter((value): value is string => Boolean(value))

const python = pythonCandidates.find(candidate => fs.existsSync(candidate))
const temporaryDirectories: string[] = []
const runners: WorkspaceRunnerProcess[] = []

function git(cwd: string, ...args: string[]) {
  return execFileSync('git', args, { cwd, encoding: 'utf8' }).trim()
}

afterEach(() => {
  for (const runner of runners.splice(0)) {runner.stop()}

  for (const directory of temporaryDirectories.splice(0)) {
    fs.rmSync(directory, { force: true, recursive: true })
  }
})

test.skipIf(!python)('desktop runner process creates an isolated worktree end to end', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-runner-desktop-'))
  temporaryDirectories.push(root)
  const repository = path.join(root, 'repo')
  const state = path.join(root, 'state')
  fs.mkdirSync(repository)
  git(repository, 'init', '-q')
  git(repository, 'config', 'user.email', 'test@example.com')
  git(repository, 'config', 'user.name', 'Test')
  fs.writeFileSync(path.join(repository, 'README.md'), 'initial\n')
  git(repository, 'add', '-A')
  git(repository, 'commit', '-qm', 'initial')

  const runner = new WorkspaceRunnerProcess({
    backend: {
      command: python!,
      env: { PYTHONPATH: repoRoot },
      root: repoRoot
    },
    stateDirectory: state
  })

  runners.push(runner)

  const created = await runner.worktreeAdd(repository, {
    branch: 'hermes/e2e',
    name: 'e2e'
  })

  expect(created.branch).toBe('hermes/e2e')
  expect(fs.existsSync(created.path)).toBe(true)
  expect(git(repository, 'worktree', 'list', '--porcelain')).toContain(created.path)
})

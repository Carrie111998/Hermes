import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  buildWindowsLockTimeoutMessage,
  classifyLockOwnerCandidates,
  parseWindowsProcessInventory,
  type WindowsLockOwnerCandidate
} from './windows-lock-owner-diagnostics'

test('Windows process inventory excludes command lines and classifies desktop ownership', () => {
  const inventory = parseWindowsProcessInventory(`[
    {"ProcessId":41,"ParentProcessId":7,"Name":"hermes.exe","ExecutablePath":"C:\\\\Hermes\\\\venv\\\\Scripts\\\\hermes.exe","CommandLine":"--token secret"},
    {"ProcessId":42,"ParentProcessId":41,"Name":"python.exe","ExecutablePath":"C:\\\\Hermes\\\\venv\\\\Scripts\\\\python.exe"},
    {"ProcessId":99,"ParentProcessId":7,"Name":"other.exe","ExecutablePath":"C:\\\\Windows\\\\other.exe"}
  ]`)

  assert.deepEqual(classifyLockOwnerCandidates(inventory, 'C:\\Hermes\\venv', new Set([41])), [
    {
      pid: 41,
      parentPid: 7,
      executableName: 'hermes.exe',
      executablePath: 'C:\\Hermes\\venv\\Scripts\\hermes.exe',
      ownership: 'desktop-owned'
    },
    {
      pid: 42,
      parentPid: 41,
      executableName: 'python.exe',
      executablePath: 'C:\\Hermes\\venv\\Scripts\\python.exe',
      ownership: 'desktop-descendant'
    }
  ])
  assert.equal(JSON.stringify(inventory).includes('secret'), false)
})

test('Windows process inventory tolerates single-object and unavailable fields', () => {
  const inventory = parseWindowsProcessInventory(
    '{"ProcessId":55,"ParentProcessId":1,"Name":"python.exe","ExecutablePath":null}'
  )

  assert.deepEqual(inventory, [
    { pid: 55, parentPid: 1, executableName: 'python.exe', executablePath: null }
  ])
  assert.deepEqual(classifyLockOwnerCandidates(inventory, 'C:\\Hermes\\venv', new Set()), [])
  assert.deepEqual(parseWindowsProcessInventory('not json'), [])
})

test('lock timeout reports an external holder ahead of surviving Desktop processes', () => {
  const candidates: WindowsLockOwnerCandidate[] = [
    {
      pid: 41,
      parentPid: 7,
      executableName: 'hermes.exe',
      executablePath: 'C:\\Hermes\\venv\\Scripts\\hermes.exe',
      ownership: 'desktop-owned'
    },
    {
      pid: 77,
      parentPid: 12,
      executableName: 'python.exe',
      executablePath: 'C:\\Hermes\\venv\\Scripts\\python.exe',
      ownership: 'external'
    }
  ]

  const message = buildWindowsLockTimeoutMessage(candidates)

  assert.match(message, /another process is holding the Hermes install open/)
  assert.match(message, /PID 77 .*external/)
  assert.doesNotMatch(message, /PID 41/)
})

test('lock timeout identifies a surviving Desktop-owned backend when no external holder exists', () => {
  const message = buildWindowsLockTimeoutMessage([
    {
      pid: 41,
      parentPid: 7,
      executableName: 'hermes.exe',
      executablePath: 'C:\\Hermes\\venv\\Scripts\\hermes.exe',
      ownership: 'desktop-owned'
    }
  ])

  assert.match(message, /Desktop-owned backend did not release the Hermes install/)
  assert.match(message, /PID 41 .*desktop-owned/)
})

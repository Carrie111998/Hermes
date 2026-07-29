#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { constants } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const websiteDir = dirname(dirname(fileURLToPath(import.meta.url)))
const docusaurusCli = join(websiteDir, 'node_modules', '@docusaurus', 'core', 'bin', 'docusaurus.mjs')

// Node 25+ exposes experimental Web Storage in server-side processes. Without
// a persistence file, merely probing globalThis.localStorage emits a warning
// during Docusaurus SSR. The docs build does not use Node's Web Storage, so
// disable that experiment for the child process instead of suppressing all
// warnings.
const nodeMajor = Number.parseInt(process.versions.node.split('.')[0], 10)
const nodeArgs = nodeMajor >= 25 ? ['--no-experimental-webstorage'] : []

const child = spawn(process.execPath, [...nodeArgs, docusaurusCli, ...process.argv.slice(2)], {
  cwd: websiteDir,
  env: process.env,
  stdio: 'inherit'
})

const forwardedSignals =
  process.platform === 'win32' ? ['SIGINT', 'SIGTERM', 'SIGBREAK'] : ['SIGINT', 'SIGTERM', 'SIGHUP']
const signalHandlers = new Map()
let terminatingSignal

for (const signal of forwardedSignals) {
  const handler = () => {
    terminatingSignal ??= signal
    if (child.exitCode === null && child.signalCode === null) child.kill(signal)
  }
  process.on(signal, handler)
  signalHandlers.set(signal, handler)
}

function removeSignalHandlers() {
  for (const [signal, handler] of signalHandlers) process.off(signal, handler)
}

child.once('error', error => {
  removeSignalHandlers()
  console.error(`Unable to start Docusaurus: ${error.message}`)
  process.exit(1)
})

child.once('exit', (code, signal) => {
  removeSignalHandlers()
  const effectiveSignal = signal ?? terminatingSignal
  if (effectiveSignal && process.platform !== 'win32') {
    process.kill(process.pid, effectiveSignal)
    return
  }
  process.exitCode = effectiveSignal ? 128 + (constants.signals[effectiveSignal] ?? 1) : (code ?? 1)
})

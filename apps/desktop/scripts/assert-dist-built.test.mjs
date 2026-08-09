import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { checkDistBuilt } from '../scripts/assert-dist-built.mjs'

function makeDist(extra) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-assert-dist-'))
  const distDir = path.join(tempRoot, 'dist')
  fs.mkdirSync(distDir, { recursive: true })
  if (extra) extra(distDir)
  return { tempRoot, distDir }
}

test('checkDistBuilt passes when index.html + an assets JS bundle exist', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.writeFileSync(path.join(d, 'index.html'), '<!doctype html><div id=root></div>', 'utf8')
    fs.mkdirSync(path.join(d, 'assets'))
    fs.writeFileSync(
      path.join(d, 'assets', 'index-abc123.js'),
      'function useLocation(){ throw new Error("may be used only in the context of a <Router> component") }',
      'utf8'
    )
  })
  try {
    assert.deepEqual(checkDistBuilt(distDir), { ok: true })
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt fails when the dist directory is absent', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-assert-dist-'))
  try {
    const result = checkDistBuilt(path.join(tempRoot, 'dist'))
    assert.equal(result.ok, false)
    assert.match(result.error, /no dist directory/)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt fails when index.html is missing', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.mkdirSync(path.join(d, 'assets'))
    fs.writeFileSync(path.join(d, 'assets', 'index-abc123.js'), 'console.log(1)', 'utf8')
  })
  try {
    const result = checkDistBuilt(distDir)
    assert.equal(result.ok, false)
    assert.match(result.error, /index\.html is missing/)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt fails when index.html is empty', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.writeFileSync(path.join(d, 'index.html'), '', 'utf8')
    fs.mkdirSync(path.join(d, 'assets'))
    fs.writeFileSync(path.join(d, 'assets', 'index-abc123.js'), 'console.log(1)', 'utf8')
  })
  try {
    const result = checkDistBuilt(distDir)
    assert.equal(result.ok, false)
    assert.match(result.error, /index\.html is empty/)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt fails when assets/ has no JS bundle', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.writeFileSync(path.join(d, 'index.html'), '<!doctype html>', 'utf8')
    fs.mkdirSync(path.join(d, 'assets'))
    // CSS only, no JS — still a blank page at runtime.
    fs.writeFileSync(path.join(d, 'assets', 'index-abc123.css'), 'body{}', 'utf8')
  })
  try {
    const result = checkDistBuilt(distDir)
    assert.equal(result.ok, false)
    assert.match(result.error, /no built JS bundle/)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt passes when react-router invariant is in exactly one JS bundle', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.writeFileSync(path.join(d, 'index.html'), '<!doctype html><div id=root></div>', 'utf8')
    fs.mkdirSync(path.join(d, 'assets'))
    fs.writeFileSync(
      path.join(d, 'assets', 'vendor-react-123.js'),
      'function useLocation(){ throw new Error("may be used only in the context of a <Router> component") }',
      'utf8'
    )
    fs.writeFileSync(path.join(d, 'assets', 'command-456.js'), 'console.log("command")', 'utf8')
  })
  try {
    assert.deepEqual(checkDistBuilt(distDir), { ok: true })
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt fails when react-router invariant is emitted into multiple JS chunks', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.writeFileSync(path.join(d, 'index.html'), '<!doctype html><div id=root></div>', 'utf8')
    fs.mkdirSync(path.join(d, 'assets'))
    fs.writeFileSync(
      path.join(d, 'assets', 'index-123.js'),
      'function useLocation(){ throw new Error("may be used only in the context of a <Router> component") }',
      'utf8'
    )
    fs.writeFileSync(
      path.join(d, 'assets', 'command-456.js'),
      'function useLocation(){ throw new Error("may be used only in the context of a <Router> component") }',
      'utf8'
    )
  })
  try {
    const result = checkDistBuilt(distDir)
    assert.equal(result.ok, false)
    assert.match(result.error, /react-router context invariant emitted into 2 separate chunks/)
    assert.match(result.error, /command-456\.js/)
    assert.match(result.error, /index-123\.js/)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('checkDistBuilt fails when react-router context invariant is missing from JS bundles', () => {
  const { tempRoot, distDir } = makeDist(d => {
    fs.writeFileSync(path.join(d, 'index.html'), '<!doctype html><div id=root></div>', 'utf8')
    fs.mkdirSync(path.join(d, 'assets'))
    fs.writeFileSync(path.join(d, 'assets', 'index-abc123.js'), 'console.log(1)', 'utf8')
  })
  try {
    const result = checkDistBuilt(distDir)
    assert.equal(result.ok, false)
    assert.match(result.error, /react-router context invariant not found in any JS chunk/)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})



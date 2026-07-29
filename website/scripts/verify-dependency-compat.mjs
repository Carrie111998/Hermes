#!/usr/bin/env node

import { createRequire } from 'node:module'
import { dirname } from 'node:path'

const require = createRequire(import.meta.url)
const serveHandlerPath = require.resolve('serve-handler/package.json')
const handlerMinimatchPath = require.resolve('minimatch', {
  paths: [dirname(serveHandlerPath)]
})
const adapterRequire = createRequire(handlerMinimatchPath)
const minimatch = require(handlerMinimatchPath)
const secureMinimatch = adapterRequire('minimatch-secure')
const serializeJavaScript = require('serialize-javascript')
const serveHandler = require('serve-handler')
const sockjs = require('sockjs')
const { v4: uuidv4 } = require('uuid')
const packageLock = require('../package-lock.json')

function dependencyConsumers(dependency) {
  return Object.entries(packageLock.packages ?? {})
    .filter(([, metadata]) =>
      ['dependencies', 'optionalDependencies', 'peerDependencies'].some(field => dependency in (metadata[field] ?? {}))
    )
    .map(([path]) => path)
    .sort()
}

function sameValues(actual, expected) {
  return actual.length === expected.length && actual.every((value, index) => value === expected[index])
}

const serializeConsumers = dependencyConsumers('serialize-javascript')
const uuidConsumers = dependencyConsumers('uuid')

const checks = [
  ['legacy callable export', typeof minimatch === 'function'],
  ['brace expansion', minimatch('page.js', '*.{js,ts}')],
  ['negative brace expansion', !minimatch('page.css', '*.{js,ts}')],
  ['patched implementation delegation', minimatch.Minimatch === secureMinimatch.Minimatch],
  ['Minimatch class export', typeof minimatch.Minimatch === 'function'],
  ['filter helper', ['page.js', 'page.ts'].filter(minimatch.filter('*.js')).join() === 'page.js'],
  ['defaults helper', minimatch.defaults({ nocase: true })('PAGE.JS', '*.js')],
  ['serve-handler export', typeof serveHandler === 'function'],
  ['serialize-javascript export', typeof serializeJavaScript === 'function'],
  ['serialize-javascript escaping', serializeJavaScript({ x: '</script>' }).includes('\\u003C')],
  [
    'serialize-javascript override consumer boundary',
    sameValues(serializeConsumers, ['node_modules/copy-webpack-plugin', 'node_modules/css-minimizer-webpack-plugin'])
  ],
  ['sockjs export', typeof sockjs.createServer === 'function'],
  ['uuid v4 export', /^[0-9a-f-]{36}$/.test(uuidv4())],
  ['uuid override consumer boundary', sameValues(uuidConsumers, ['node_modules/mermaid', 'node_modules/sockjs'])]
]

const failed = checks.filter(([, passed]) => !passed).map(([name]) => name)
if (failed.length > 0) {
  console.error(`Dependency compatibility check failed: ${failed.join(', ')}`)
  process.exit(1)
}

console.log(`Dependency compatibility check passed (${handlerMinimatchPath}).`)

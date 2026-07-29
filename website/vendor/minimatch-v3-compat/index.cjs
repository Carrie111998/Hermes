'use strict'

// minimatch v10 exports an object whose `minimatch` property is the callable
// matcher. minimatch v3 consumers (notably serve-handler 6.x) require the
// package itself to be callable. Preserve that legacy entry shape while using
// the patched v10 implementation and attach the modern named exports.
const upstream = require('minimatch-secure')
const matcher = upstream.minimatch

Object.assign(matcher, upstream)
module.exports = matcher

'use strict'

const extractZip = require('extract-zip')

function extract(zipPath, options) {
  return extractZip(zipPath, options)
}

module.exports = { extract }

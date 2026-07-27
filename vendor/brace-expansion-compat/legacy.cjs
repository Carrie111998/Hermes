"use strict";

// brace-expansion 1.x and 2.x exported a callable CommonJS function, while
// 5.x exports named members. Keep both contracts while using the unmodified
// patched 5.0.8 implementation for all expansion work.
const secure = require("./dist/commonjs/index.js");

function braceExpansion(pattern, options) {
  // Preserve the legacy callable export shape, not its unsafe lack of bounds.
  // Both the result-count and total-character caps from patched 5.0.8 apply.
  return secure.expand(pattern, options);
}

braceExpansion.expand = secure.expand;
braceExpansion.EXPANSION_MAX = secure.EXPANSION_MAX;
braceExpansion.EXPANSION_MAX_LENGTH = secure.EXPANSION_MAX_LENGTH;

module.exports = braceExpansion;

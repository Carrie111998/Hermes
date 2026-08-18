import { strict as assert } from 'node:assert';

import { mediaPayloadForFile } from './bridge_helpers.js';

const cases = [
  ['report.html', 'text/html'],
  ['report.htm', 'text/html'],
  ['notes.txt', 'text/plain'],
  ['data.csv', 'text/csv'],
];

for (const [fileName, expectedMime] of cases) {
  const payload = mediaPayloadForFile({
    buffer: Buffer.from('test'),
    filePath: `/tmp/${fileName}`,
    mediaType: 'document',
  });

  assert.ok(payload.document);
  assert.equal(payload.fileName, fileName);
  assert.equal(payload.mimetype, expectedMime);
}

console.log('  ✓ text/web documents keep a WhatsApp-openable MIME type');

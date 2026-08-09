import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import imageSize from 'image-size';
import { imageSizeFromFile } from 'image-size/fromFile';
import { HEIF } from 'image-size/types/heif';
import { ICNS } from 'image-size/types/icns';
import { JXL } from 'image-size/types/jxl';

const require = createRequire(import.meta.url);
const { default: cjsImageSize } = require('image-size');
const { imageSizeFromFile: cjsImageSizeFromFile } = require('image-size/fromFile');
const { HEIF: CjsHEIF } = require('image-size/types/heif');
const { ICNS: CjsICNS } = require('image-size/types/icns');
const { JXL: CjsJXL } = require('image-size/types/jxl');

const encoder = new TextEncoder();
const putAscii = (buffer, offset, value) => buffer.set(encoder.encode(value), offset);
const putU32BE = (buffer, offset, value) =>
  new DataView(buffer.buffer).setUint32(offset, value, false);

const malformedIcns = new Uint8Array(16);
putAscii(malformedIcns, 0, 'icns');
putU32BE(malformedIcns, 4, malformedIcns.length);
putAscii(malformedIcns, 8, 'ic07');
putU32BE(malformedIcns, 12, 0);

const malformedJxl = new Uint8Array(16);
putU32BE(malformedJxl, 0, 0);
putAscii(malformedJxl, 4, 'jxlp');

const malformedPublicJxl = new Uint8Array(40);
putU32BE(malformedPublicJxl, 0, 12);
putAscii(malformedPublicJxl, 4, 'JXL ');
putU32BE(malformedPublicJxl, 12, 20);
putAscii(malformedPublicJxl, 16, 'ftyp');
putAscii(malformedPublicJxl, 20, 'jxl ');
putU32BE(malformedPublicJxl, 32, 0);
putAscii(malformedPublicJxl, 36, 'jxlp');

const malformedHeif = new Uint8Array(64);
putU32BE(malformedHeif, 0, 48);
putAscii(malformedHeif, 4, 'meta');
putU32BE(malformedHeif, 12, 36);
putAscii(malformedHeif, 16, 'iprp');
putU32BE(malformedHeif, 20, 28);
putAscii(malformedHeif, 24, 'ipco');
putU32BE(malformedHeif, 28, 0);
putAscii(malformedHeif, 32, 'ispe');
putU32BE(malformedHeif, 40, 100);
putU32BE(malformedHeif, 44, 100);

const malformedPublicHeif = new Uint8Array(80);
putU32BE(malformedPublicHeif, 0, 16);
putAscii(malformedPublicHeif, 4, 'ftyp');
putAscii(malformedPublicHeif, 8, 'heic');
malformedPublicHeif.set(malformedHeif, 16);

const parserSets = {
  esm: { HEIF, ICNS, JXL, imageSize, imageSizeFromFile },
  cjs: {
    HEIF: CjsHEIF,
    ICNS: CjsICNS,
    JXL: CjsJXL,
    imageSize: cjsImageSize,
    imageSizeFromFile: cjsImageSizeFromFile,
  },
};

async function assertVendoredPackageProvenance() {
  const manifestUrl = new URL('../vendor/image-size/package.json', import.meta.url);
  const manifest = JSON.parse(await readFile(manifestUrl, 'utf8'));
  for (const requiredFile of ['LICENSE', 'Readme.md', 'SECURITY-PATCH.md']) {
    assert.ok(
      manifest.files.includes(requiredFile),
      `vendored image-size package must include ${requiredFile}`,
    );
  }
}

async function exerciseParsers(moduleKind) {
  const parsers = parserSets[moduleKind];
  assert.ok(parsers, `Unknown module kind: ${moduleKind}`);
  assert.throws(
    () => parsers.ICNS.calculate(malformedIcns),
    /Invalid ICNS entry length/,
    `${moduleKind} ICNS parser must reject a zero-length entry`,
  );
  assert.throws(
    () => parsers.JXL.calculate(malformedJxl),
    /Invalid JXL box length/,
    `${moduleKind} JXL parser must reject a non-advancing box`,
  );
  assert.throws(
    () => parsers.HEIF.calculate(malformedHeif),
    /Invalid HEIF box length/,
    `${moduleKind} HEIF parser must reject a non-advancing box`,
  );
  assert.throws(
    () => parsers.imageSize(malformedIcns),
    /Invalid ICNS entry length/,
    `${moduleKind} public entry point must use the patched ICNS parser`,
  );
  assert.throws(
    () => parsers.imageSize(malformedPublicJxl),
    /Invalid JXL box length/,
    `${moduleKind} public entry point must use the patched JXL parser`,
  );
  assert.throws(
    () => parsers.imageSize(malformedPublicHeif),
    /Invalid HEIF box length/,
    `${moduleKind} public entry point must use the patched HEIF parser`,
  );

  const fixtureDirectory = await mkdtemp(join(tmpdir(), 'image-size-regression-'));
  try {
    for (const [format, fixture, expectedError] of [
      ['icns', malformedIcns, /Invalid ICNS entry length/],
      ['jxl', malformedPublicJxl, /Invalid JXL box length/],
      ['heif', malformedPublicHeif, /Invalid HEIF box length/],
    ]) {
      const fixturePath = join(fixtureDirectory, `malformed.${format}`);
      await writeFile(fixturePath, fixture);
      await assert.rejects(
        parsers.imageSizeFromFile(fixturePath),
        expectedError,
        `${moduleKind} fromFile entry point must use the patched ${format.toUpperCase()} parser`,
      );
    }
  } finally {
    await rm(fixtureDirectory, { recursive: true, force: true });
  }
}

function runIsolated(moduleKind) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fileURLToPath(import.meta.url), moduleKind], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stderr = '';
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });

    const timeout = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`${moduleKind} parser regression timed out after 5 seconds`));
    }, 5_000);

    child.once('error', (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.once('exit', (code, signal) => {
      clearTimeout(timeout);
      if (code === 0) {
        resolve();
        return;
      }
      reject(
        new Error(
          `${moduleKind} parser regression exited with code ${code ?? 'null'}${
            signal ? ` (${signal})` : ''
          }${stderr ? `\n${stderr}` : ''}`,
        ),
      );
    });
  });
}

const requestedModuleKind = process.argv[2];
if (requestedModuleKind) {
  await exerciseParsers(requestedModuleKind);
} else {
  await assertVendoredPackageProvenance();
  await runIsolated('esm');
  await runIsolated('cjs');
  console.log('image-size security regressions passed');
}

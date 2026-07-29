import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { test } from 'vitest'

import {
  ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES,
  clampDataUrlReadMaxMb,
  DATA_URL_READ_DEFAULT_MAX_MB,
  dataUrlReadMaxBytesFromMb,
  DEFAULT_FETCH_TIMEOUT_MS,
  encryptDesktopSecret,
  filenameFromContentDisposition,
  PLUGIN_DOWNLOAD_MAX_BYTES,
  readFileDataUrlForIpc,
  resolveDirectoryForIpc,
  resolveDownloadCollision,
  resolveReadableFileForIpc,
  resolveRequestedPathForIpc,
  resolveTimeoutMs,
  safeDownloadFilename,
  sensitiveFileBlockReason
} from './hardening'

async function rejectsWithCode(promise, code: string) {
  await assert.rejects(promise, (error: any) => {
    assert.equal(error?.code, code)

    return true
  })
}

test('clampDataUrlReadMaxMb defaults and bounds the attach size preference', () => {
  assert.equal(clampDataUrlReadMaxMb(undefined), DATA_URL_READ_DEFAULT_MAX_MB)
  assert.equal(clampDataUrlReadMaxMb(0), 1)
  assert.equal(clampDataUrlReadMaxMb(256), 256)
  assert.equal(clampDataUrlReadMaxMb(99999), 4096)
  assert.equal(dataUrlReadMaxBytesFromMb(16), 16 * 1024 * 1024)
})

test('attachment upload cap is bounded above the preview default', () => {
  assert.equal(ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES, 256 * 1024 * 1024)
  assert.ok(ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES > dataUrlReadMaxBytesFromMb(DATA_URL_READ_DEFAULT_MAX_MB))
})

test('attachment data URL helper reads bytes above the preview default without changing that limit', async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-large-attachment-'))
  const source = path.join(tempDir, 'large.bin')
  const previewLimit = dataUrlReadMaxBytesFromMb(DATA_URL_READ_DEFAULT_MAX_MB)
  const content = Buffer.alloc(previewLimit + 1024, 0x5a)

  try {
    fs.writeFileSync(source, content)

    await assert.rejects(
      resolveReadableFileForIpc(source, {
        maxBytes: previewLimit,
        purpose: 'File preview'
      }),
      /file is too large/
    )

    const dataUrl = await readFileDataUrlForIpc(source, {
      maxBytes: ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES,
      mimeType: 'application/octet-stream',
      purpose: 'Attachment upload'
    })

    assert.match(dataUrl, /^data:application\/octet-stream;base64,/)
    assert.deepEqual(Buffer.from(dataUrl.slice(dataUrl.indexOf(',') + 1), 'base64'), content)
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('resolveTimeoutMs falls back to defaults and accepts overrides', () => {
  assert.equal(resolveTimeoutMs(undefined), DEFAULT_FETCH_TIMEOUT_MS)
  assert.equal(resolveTimeoutMs(0), DEFAULT_FETCH_TIMEOUT_MS)
  assert.equal(resolveTimeoutMs(-25), DEFAULT_FETCH_TIMEOUT_MS)
  assert.equal(resolveTimeoutMs('2750'), 2750)
})

test('encryptDesktopSecret requires available secure storage', () => {
  assert.equal(
    encryptDesktopSecret('', { isEncryptionAvailable: () => true, encryptString: () => Buffer.alloc(0) }),
    null
  )

  assert.throws(
    () => encryptDesktopSecret('token', { isEncryptionAvailable: () => false, encryptString: () => Buffer.alloc(0) }),
    /Secure token storage is unavailable/
  )
})

test('encryptDesktopSecret stores safeStorage base64 payload', () => {
  const secret = encryptDesktopSecret('token-123', {
    isEncryptionAvailable: () => true,
    encryptString: value => Buffer.from(`enc:${value}`, 'utf8')
  })

  assert.deepEqual(secret, {
    encoding: 'safeStorage',
    value: Buffer.from('enc:token-123', 'utf8').toString('base64')
  })
})

test('sensitiveFileBlockReason blocks obvious secret file patterns', () => {
  assert.match(String(sensitiveFileBlockReason('/tmp/.env')), /\.env/)
  assert.equal(sensitiveFileBlockReason('/tmp/.env.example'), null)
  assert.match(String(sensitiveFileBlockReason('/Users/me/.ssh/id_ed25519')), /SSH/)
  assert.match(String(sensitiveFileBlockReason('/tmp/server-cert.pem')), /\.pem/)
})

test('path helpers reject blank non-string NUL and Windows device syntax', async () => {
  await rejectsWithCode(resolveReadableFileForIpc('', { purpose: 'File preview' }), 'invalid-path')
  await rejectsWithCode(resolveReadableFileForIpc('   ', { purpose: 'File preview' }), 'invalid-path')
  await rejectsWithCode(resolveReadableFileForIpc(null, { purpose: 'File preview' }), 'invalid-path')
  await rejectsWithCode(resolveReadableFileForIpc(`safe${String.fromCharCode(0)}name.txt`), 'invalid-path')

  const devicePaths = [
    '\\\\?\\C:\\secret.txt',
    '\\\\.\\C:\\secret.txt',
    '\\\\?\\UNC\\server\\share\\secret.txt',
    'GLOBALROOT/Device/HarddiskVolumeShadowCopy1/secret.txt'
  ]

  for (const devicePath of devicePaths) {
    assert.throws(
      () => resolveRequestedPathForIpc(devicePath, { purpose: 'File preview' }),
      (error: any) => {
        assert.equal(error?.code, 'device-path')

        return true
      }
    )
    await rejectsWithCode(resolveReadableFileForIpc(devicePath, { purpose: 'File preview' }), 'device-path')
  }

  assert.throws(
    () => resolveRequestedPathForIpc('file:///%E0%A4%A', { purpose: 'File preview' }),
    (error: any) => {
      assert.equal(error?.code, 'invalid-path')

      return true
    }
  )
  await rejectsWithCode(resolveReadableFileForIpc('file:///%E0%A4%A', { purpose: 'File preview' }), 'invalid-path')
})

test('resolveRequestedPathForIpc resolves relative paths from the trimmed base directory', () => {
  const baseDir = path.join(os.tmpdir(), 'hermes-desktop-base')

  assert.equal(
    resolveRequestedPathForIpc('notes.txt', {
      baseDir: `  ${baseDir}  `,
      purpose: 'File preview'
    }),
    path.resolve(baseDir, 'notes.txt')
  )
})

test('resolveRequestedPathForIpc expands ~ to the home directory', () => {
  assert.equal(resolveRequestedPathForIpc('~', { purpose: 'Directory read' }), path.resolve(os.homedir()))
  assert.equal(
    resolveRequestedPathForIpc('~/www/project', { purpose: 'Directory read' }),
    path.resolve(os.homedir(), 'www/project')
  )
  // `~user` shorthand is NOT expanded — only the caller's own home.
  assert.equal(
    resolveRequestedPathForIpc('~other/secret', { baseDir: os.tmpdir(), purpose: 'Directory read' }),
    path.resolve(os.tmpdir(), '~other/secret')
  )
})

test('resolveReadableFileForIpc validates existence type size and sensitivity', async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-hardening-'))

  try {
    const textPath = path.join(tempDir, 'notes.txt')
    fs.writeFileSync(textPath, 'hello world', 'utf8')

    const fromRelative = await resolveReadableFileForIpc('notes.txt', {
      baseDir: tempDir,
      maxBytes: 256,
      purpose: 'File preview'
    })

    assert.equal(fromRelative.resolvedPath, textPath)
    assert.equal(fromRelative.stat.size, 11)

    const fromFileUrl = await resolveReadableFileForIpc(pathToFileURL(textPath).toString(), {
      purpose: 'File preview'
    })

    assert.equal(fromFileUrl.resolvedPath, textPath)

    const spacedPath = path.join(tempDir, 'notes with spaces.txt')
    fs.writeFileSync(spacedPath, 'space ok', 'utf8')

    const fromSpacedFileUrl = await resolveReadableFileForIpc(pathToFileURL(spacedPath).toString(), {
      purpose: 'File preview'
    })

    assert.equal(fromSpacedFileUrl.resolvedPath, spacedPath)

    await assert.rejects(
      resolveReadableFileForIpc('missing.txt', {
        baseDir: tempDir,
        purpose: 'Text preview'
      }),
      /file does not exist/
    )

    const nestedDir = path.join(tempDir, 'directory')
    fs.mkdirSync(nestedDir)
    await assert.rejects(
      resolveReadableFileForIpc(nestedDir, {
        purpose: 'Text preview'
      }),
      /path points to a directory/
    )

    const largePath = path.join(tempDir, 'large.txt')
    fs.writeFileSync(largePath, 'x'.repeat(40), 'utf8')
    await assert.rejects(
      resolveReadableFileForIpc(largePath, {
        maxBytes: 8,
        purpose: 'File preview'
      }),
      /file is too large/
    )

    const envPath = path.join(tempDir, '.env')
    fs.writeFileSync(envPath, 'SECRET_TOKEN=123', 'utf8')
    await assert.rejects(
      resolveReadableFileForIpc(envPath, {
        purpose: 'File preview'
      }),
      /blocked for sensitive file/
    )

    const envTemplatePath = path.join(tempDir, '.env.example')
    fs.writeFileSync(envTemplatePath, 'EXAMPLE_TOKEN=value', 'utf8')

    const envTemplate = await resolveReadableFileForIpc(envTemplatePath, {
      purpose: 'File preview'
    })

    assert.equal(envTemplate.resolvedPath, envTemplatePath)
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('resolveReadableFileForIpc blocks common sensitive files', async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-sensitive-'))

  try {
    const sshDir = path.join(tempDir, '.ssh')
    fs.mkdirSync(sshDir)

    const blockedFiles = [
      path.join(tempDir, '.env'),
      path.join(tempDir, '.npmrc'),
      path.join(sshDir, 'id_ed25519'),
      path.join(tempDir, 'cert.pem'),
      path.join(tempDir, 'cert.p12'),
      path.join(tempDir, 'cert.pfx')
    ]

    for (const filePath of blockedFiles) {
      fs.writeFileSync(filePath, 'secret', 'utf8')
      await rejectsWithCode(resolveReadableFileForIpc(filePath, { purpose: 'File preview' }), 'sensitive-file')
    }

    const allowed = path.join(tempDir, '.env.example')
    fs.writeFileSync(allowed, 'EXAMPLE_TOKEN=value', 'utf8')
    assert.equal((await resolveReadableFileForIpc(allowed, { purpose: 'File preview' })).resolvedPath, allowed)
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('resolveReadableFileForIpc blocks symlinks whose realpath is sensitive', async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-realpath-'))

  try {
    const envPath = path.join(tempDir, '.env')
    const linkPath = path.join(tempDir, 'safe-name.txt')
    fs.writeFileSync(envPath, 'SECRET_TOKEN=123', 'utf8')

    try {
      fs.symlinkSync(envPath, linkPath, 'file')
    } catch (error) {
      if (error?.code === 'EPERM' || error?.code === 'EACCES') {
        // symlink creation is not permitted on this platform — skip
        return
      }

      throw error
    }

    await rejectsWithCode(resolveReadableFileForIpc(linkPath, { purpose: 'File preview' }), 'sensitive-file')
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('resolveDirectoryForIpc accepts directories and rejects invalid directory targets', async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-dir-'))

  try {
    const directory = path.join(tempDir, 'project')
    const filePath = path.join(tempDir, 'file.txt')
    fs.mkdirSync(directory)
    fs.writeFileSync(filePath, 'not a directory', 'utf8')

    const resolved = await resolveDirectoryForIpc(directory)
    assert.equal(resolved.resolvedPath, directory)
    assert.equal(resolved.stat.isDirectory(), true)

    await rejectsWithCode(resolveDirectoryForIpc(filePath), 'ENOTDIR')
    await rejectsWithCode(resolveDirectoryForIpc(path.join(tempDir, 'missing')), 'ENOENT')
    await rejectsWithCode(resolveDirectoryForIpc('\\\\?\\C:\\secret'), 'device-path')
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('resolveDirectoryForIpc accepts directory symlinks or junctions', async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-dir-link-'))

  try {
    const directory = path.join(tempDir, 'actual-project')
    const linkPath = path.join(tempDir, 'linked-project')
    fs.mkdirSync(directory)

    try {
      fs.symlinkSync(directory, linkPath, process.platform === 'win32' ? 'junction' : 'dir')
    } catch (error) {
      if (error?.code === 'EPERM' || error?.code === 'EACCES') {
        // directory symlink creation is not permitted on this platform — skip
        return
      }

      throw error
    }

    const resolved = await resolveDirectoryForIpc(linkPath)
    assert.equal(resolved.resolvedPath, linkPath)
    assert.equal(resolved.stat.isDirectory(), true)
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('safeDownloadFilename strips every path escape a server or DB row could inject', () => {
  // Separators become underscores rather than being dropped, so the name stays
  // recognisable and can never address a parent directory. The leading-dot
  // strip then runs on the result, so a leading '..' loses its dots too.
  assert.equal(safeDownloadFilename('../../etc/passwd'), '_.._etc_passwd')
  assert.equal(safeDownloadFilename('..\\..\\Windows\\System32\\cfg'), '_.._Windows_System32_cfg')
  assert.equal(safeDownloadFilename('/absolute/path.txt'), '_absolute_path.txt')

  // Bare traversal tokens and dotfile-forcing prefixes carry no usable name.
  assert.equal(safeDownloadFilename('..'), 'download')
  assert.equal(safeDownloadFilename('.'), 'download')
  assert.equal(safeDownloadFilename('...'), 'download')
  assert.equal(safeDownloadFilename('.hidden'), 'hidden')

  // Empty / missing / NUL-poisoned input falls back instead of throwing.
  assert.equal(safeDownloadFilename(''), 'download')
  assert.equal(safeDownloadFilename(null), 'download')
  assert.equal(safeDownloadFilename('a\0b.txt'), 'ab.txt')
  assert.equal(safeDownloadFilename('', 'fallback.bin'), 'fallback.bin')

  // An ordinary name survives untouched.
  assert.equal(safeDownloadFilename('report 2026.pdf'), 'report 2026.pdf')
})

test('filenameFromContentDisposition prefers RFC 5987 and sanitizes both forms', () => {
  assert.equal(filenameFromContentDisposition('attachment; filename="notes.txt"'), 'notes.txt')
  assert.equal(filenameFromContentDisposition('attachment; filename=notes.txt'), 'notes.txt')

  // filename* wins when both are present, and percent-decoding is applied.
  assert.equal(
    filenameFromContentDisposition("attachment; filename=\"fallback.txt\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"),
    'résumé.pdf'
  )

  // A traversal smuggled through either form is still neutralized.
  assert.equal(filenameFromContentDisposition('attachment; filename="../../evil.sh"'), '_.._evil.sh')
  assert.equal(filenameFromContentDisposition("attachment; filename*=UTF-8''%2e%2e%2f%2e%2e%2fevil.sh"), '_.._evil.sh')

  // Malformed percent-encoding falls back to the plain form rather than throwing.
  assert.equal(filenameFromContentDisposition("attachment; filename=\"ok.txt\"; filename*=UTF-8''%E0%A4%A"), 'ok.txt')

  // No header, or no filename in it, yields empty so the caller can fall back.
  assert.equal(filenameFromContentDisposition('attachment'), '')
  assert.equal(filenameFromContentDisposition(''), '')
  assert.equal(filenameFromContentDisposition(null), '')
})

test('resolveDownloadCollision suffixes before the extension and never clobbers', () => {
  const dir = path.join(os.tmpdir(), 'dl')
  const taken = new Set([path.join(dir, 'a.txt'), path.join(dir, 'a (1).txt')])
  const exists = (candidate: string) => taken.has(candidate)

  // A free name is returned untouched.
  assert.equal(resolveDownloadCollision(path.join(dir, 'free.txt'), exists), path.join(dir, 'free.txt'))

  // Occupied names walk forward past every taken index.
  assert.equal(resolveDownloadCollision(path.join(dir, 'a.txt'), exists), path.join(dir, 'a (2).txt'))

  // The suffix goes before the extension so the file still opens correctly,
  // including for multi-dot names.
  const archive = path.join(dir, 'bundle.tar.gz')
  assert.equal(resolveDownloadCollision(archive, c => c === archive), path.join(dir, 'bundle.tar (1).gz'))

  // A leading-dot file is all name, no extension.
  const dotfile = path.join(dir, '.gitignore')
  assert.equal(resolveDownloadCollision(dotfile, c => c === dotfile), path.join(dir, '.gitignore (1)'))

  // An exhausted range raises instead of spinning forever.
  assert.throws(() => resolveDownloadCollision(path.join(dir, 'x.txt'), () => true, 3), /after 3 attempts/)
})

test('the plugin download cap matches the backend attachment limit', () => {
  // A blob the backend refused to accept can't come back down, so the ceiling
  // tracks KANBAN_ATTACHMENT_MAX_BYTES rather than drifting on its own.
  assert.equal(PLUGIN_DOWNLOAD_MAX_BYTES, 25 * 1024 * 1024)
  assert.ok(PLUGIN_DOWNLOAD_MAX_BYTES < ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES)
})

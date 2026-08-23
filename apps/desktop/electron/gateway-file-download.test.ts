import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import {
  filenameFromContentDisposition,
  gatewayFilePath,
  isNotFoundError,
  parseDataUrlToBuffer,
  pumpStreamToFile,
  saveDialogFilters
} from './gateway-file-download'

// A Readable-like response driven manually in tests.
class FakeResponse extends EventEmitter {
  paused = false
  resumed = false
  destroyed = false

  pause() {
    this.paused = true
  }

  resume() {
    this.resumed = true
  }

  destroy() {
    this.destroyed = true
  }
}

// A write stream that records writes and lets tests control backpressure.
class FakeWriteStream extends EventEmitter {
  chunks: Buffer[] = []
  ended = false
  destroyed = false
  private writeReturns: boolean[]

  constructor(writeReturns: boolean[] = []) {
    super()
    this.writeReturns = writeReturns
  }

  write(chunk: Buffer): boolean {
    this.chunks.push(chunk)

    return this.writeReturns.length ? this.writeReturns.shift()! : true
  }

  end(cb: () => void) {
    this.ended = true
    cb()
  }

  destroy() {
    this.destroyed = true
  }
}

test('pumpStreamToFile streams chunks to the destination without buffering the whole body', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const unlinked: string[] = []

  const promise = pumpStreamToFile(res as never, '/tmp/out.bin', {
    createWriteStream: () => ws as never,
    unlink: async p => {
      unlinked.push(p)
    }
  })

  res.emit('data', Buffer.from('abc'))
  res.emit('data', Buffer.from('def'))
  res.emit('end')

  await promise

  assert.equal(Buffer.concat(ws.chunks).toString('utf8'), 'abcdef')
  assert.equal(ws.ended, true)
  assert.deepEqual(unlinked, []) // success -> no cleanup
})

test('pumpStreamToFile applies backpressure: pauses on a full buffer and resumes on drain', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream([false]) // first write signals "buffer full"

  const promise = pumpStreamToFile(res as never, '/tmp/out.bin', {
    createWriteStream: () => ws as never,
    unlink: async () => {}
  })

  res.emit('data', Buffer.from('big-chunk'))
  assert.equal(res.paused, true, 'source should be paused when write() returns false')
  assert.equal(res.resumed, false)

  ws.emit('drain')
  assert.equal(res.resumed, true, 'source should resume after the write stream drains')

  res.emit('end')
  await promise
})

test('pumpStreamToFile unlinks the partial file and rejects on a write error', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const unlinked: string[] = []

  const promise = pumpStreamToFile(res as never, '/tmp/partial.bin', {
    createWriteStream: () => ws as never,
    unlink: async p => {
      unlinked.push(p)
    }
  })

  res.emit('data', Buffer.from('abc'))
  ws.emit('error', new Error('ENOSPC: disk full'))

  await assert.rejects(promise, /disk full/)
  assert.deepEqual(unlinked, ['/tmp/partial.bin'])
  assert.equal(res.destroyed, true, 'source should be torn down on write failure')
})

test('pumpStreamToFile unlinks the partial file and rejects on a response error', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const unlinked: string[] = []

  const promise = pumpStreamToFile(res as never, '/tmp/partial.bin', {
    createWriteStream: () => ws as never,
    unlink: async p => {
      unlinked.push(p)
    }
  })

  res.emit('data', Buffer.from('abc'))
  res.emit('error', new Error('socket hang up'))

  await assert.rejects(promise, /socket hang up/)
  assert.deepEqual(unlinked, ['/tmp/partial.bin'])
})

test('parseDataUrlToBuffer decodes base64 payloads', () => {
  const buffer = parseDataUrlToBuffer('data:text/markdown;base64,IyByZXBvcnQ=')

  assert.equal(buffer.toString('utf8'), '# report')
})

test('parseDataUrlToBuffer decodes percent-encoded (non-base64) payloads', () => {
  const buffer = parseDataUrlToBuffer('data:text/plain,hello%20world')

  assert.equal(buffer.toString('utf8'), 'hello world')
})

test('parseDataUrlToBuffer throws on a malformed data URL', () => {
  assert.throws(() => parseDataUrlToBuffer('not-a-data-url'), /Malformed data URL/)
})

test('filenameFromContentDisposition prefers filename* and reduces to a basename', () => {
  assert.equal(
    filenameFromContentDisposition("attachment; filename*=UTF-8''report%20with%20spaces.pdf"),
    'report with spaces.pdf'
  )
  assert.equal(filenameFromContentDisposition('attachment; filename="report.md"'), 'report.md')
  // A traversal attempt in the header cannot escape the chosen directory.
  assert.equal(filenameFromContentDisposition('attachment; filename="../../etc/passwd"'), 'passwd')
  assert.equal(filenameFromContentDisposition(''), '')
  assert.equal(filenameFromContentDisposition(undefined), '')
})

test('gatewayFilePath normalizes bare paths and file:// URLs', () => {
  assert.equal(gatewayFilePath('/Users/me/report.md'), '/Users/me/report.md')
  assert.equal(gatewayFilePath('file:///Users/me/a%20b.md'), '/Users/me/a b.md')
  assert.equal(gatewayFilePath(''), '')
  assert.equal(gatewayFilePath(null), '')
})

test('isNotFoundError matches only HTTP 404', () => {
  const notFound: any = new Error('404: missing')

  notFound.statusCode = 404
  assert.equal(isNotFoundError(notFound), true)

  const forbidden: any = new Error('403: nope')

  forbidden.statusCode = 403
  assert.equal(isNotFoundError(forbidden), false)
  assert.equal(isNotFoundError(new Error('plain')), false)
  assert.equal(isNotFoundError(null), false)
})

// #92480: a .pptx saved through the gateway dialog arrived as a typeless
// "File". The name reaching the dialog was correct; the dialog had no file
// type to keep it with, so Windows had no default extension to append.
test('saveDialogFilters offers the download own type before All Files', () => {
  assert.deepEqual(saveDialogFilters('Presentation_2026-08-14.pptx'), [
    { name: 'PPTX File', extensions: ['pptx'] },
    { name: 'All Files', extensions: ['*'] }
  ])
  assert.deepEqual(saveDialogFilters('report.pdf'), [
    { name: 'PDF File', extensions: ['pdf'] },
    { name: 'All Files', extensions: ['*'] }
  ])
})

test('saveDialogFilters keeps All Files last so any name stays saveable', () => {
  // The escape hatch must survive every branch: a filter list without it turns
  // a save dialog into a rename requirement.
  for (const name of ['a.pptx', 'a.tar.gz', 'noext', '.gitignore', '', null]) {
    const filters = saveDialogFilters(name)

    assert.deepEqual(filters[filters.length - 1], { name: 'All Files', extensions: ['*'] })
  }
})

test('saveDialogFilters normalizes case and reads only the last extension', () => {
  assert.deepEqual(saveDialogFilters('SHOUTING.PDF')[0], { name: 'PDF File', extensions: ['pdf'] })
  // Not 'tar.gz': the shell appends one extension, and gz is the one the name
  // actually ends with.
  assert.deepEqual(saveDialogFilters('archive.tar.gz')[0], { name: 'GZ File', extensions: ['gz'] })
})

test('saveDialogFilters falls back to All Files when there is no usable extension', () => {
  // A dotfile has no extension in path.extname terms, and inventing one from
  // the basename would offer to save .gitignore as "GITIGNORE File".
  assert.deepEqual(saveDialogFilters('.gitignore'), [{ name: 'All Files', extensions: ['*'] }])
  assert.deepEqual(saveDialogFilters('README'), [{ name: 'All Files', extensions: ['*'] }])
  assert.deepEqual(saveDialogFilters(''), [{ name: 'All Files', extensions: ['*'] }])
  assert.deepEqual(saveDialogFilters(undefined), [{ name: 'All Files', extensions: ['*'] }])
})

test('saveDialogFilters refuses an extension it cannot vouch for', () => {
  // The name can come from a server-supplied Content-Disposition header, so the
  // extension is whitelisted rather than merely extracted. Rejection is not a
  // failure: it saves under All Files, which is today's behavior.
  assert.deepEqual(saveDialogFilters('x.' + 'a'.repeat(17)), [{ name: 'All Files', extensions: ['*'] }])
  assert.deepEqual(saveDialogFilters('x.p p t'), [{ name: 'All Files', extensions: ['*'] }])
  assert.deepEqual(saveDialogFilters('x.pp*t'), [{ name: 'All Files', extensions: ['*'] }])
  assert.deepEqual(saveDialogFilters('x.p;t'), [{ name: 'All Files', extensions: ['*'] }])
})

test('saveDialogFilters reads the basename, not a directory component', () => {
  // filenameFromContentDisposition already reduces to a basename; this makes
  // the helper safe on its own rather than only in that company.
  assert.deepEqual(saveDialogFilters('/tmp/a.pdf/report.pptx')[0], {
    name: 'PPTX File',
    extensions: ['pptx']
  })
})

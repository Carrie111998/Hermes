import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { pathForRegistryBackendRequest } from './connection-config'
import {
  filenameFromContentDisposition,
  gatewayFilePath,
  gatewayFileRequestPaths,
  isNotFoundError,
  parseDataUrlToBuffer,
  pumpStreamToFile,
  resolveGatewayFileBackend
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

test('pumpStreamToFile stages chunks and promotes only after the whole body succeeds', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const created: string[] = []
  const unlinked: string[] = []
  const renamed: Array<[string, string]> = []

  const promise = pumpStreamToFile(res as never, '/tmp/out.bin', {
    createWriteStream: p => {
      created.push(p)

      return ws as never
    },
    unlink: async p => {
      unlinked.push(p)
    },
    rename: async (from, to) => {
      renamed.push([from, to])
    },
    temporaryPath: () => '/tmp/out.bin.download'
  })

  res.emit('data', Buffer.from('abc'))
  res.emit('data', Buffer.from('def'))
  res.emit('end')

  await promise

  assert.deepEqual(created, ['/tmp/out.bin.download'])
  assert.equal(Buffer.concat(ws.chunks).toString('utf8'), 'abcdef')
  assert.equal(ws.ended, true)
  assert.deepEqual(renamed, [['/tmp/out.bin.download', '/tmp/out.bin']])
  assert.deepEqual(unlinked, [])
})

test('pumpStreamToFile applies backpressure: pauses on a full buffer and resumes on drain', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream([false]) // first write signals "buffer full"

  const promise = pumpStreamToFile(res as never, '/tmp/out.bin', {
    createWriteStream: () => ws as never,
    unlink: async () => {},
    rename: async () => {},
    temporaryPath: () => '/tmp/out.bin.download'
  })

  res.emit('data', Buffer.from('big-chunk'))
  assert.equal(res.paused, true, 'source should be paused when write() returns false')
  assert.equal(res.resumed, false)

  ws.emit('drain')
  assert.equal(res.resumed, true, 'source should resume after the write stream drains')

  res.emit('end')
  await promise
})

test('pumpStreamToFile cleans only its staging file on a write error', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const created: string[] = []
  const unlinked: string[] = []
  const renamed: Array<[string, string]> = []

  const promise = pumpStreamToFile(res as never, '/tmp/existing.bin', {
    createWriteStream: p => {
      created.push(p)

      return ws as never
    },
    unlink: async p => {
      unlinked.push(p)
    },
    rename: async (from, to) => {
      renamed.push([from, to])
    },
    temporaryPath: () => '/tmp/existing.bin.download'
  })

  res.emit('data', Buffer.from('abc'))
  ws.emit('error', new Error('ENOSPC: disk full'))

  await assert.rejects(promise, /disk full/)
  assert.deepEqual(created, ['/tmp/existing.bin.download'])
  assert.deepEqual(unlinked, ['/tmp/existing.bin.download'])
  assert.deepEqual(renamed, [])
  assert.equal(res.destroyed, true, 'source should be torn down on write failure')
})

test('pumpStreamToFile preserves the selected destination when the response fails', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const unlinked: string[] = []
  const renamed: Array<[string, string]> = []

  const promise = pumpStreamToFile(res as never, '/tmp/existing.bin', {
    createWriteStream: () => ws as never,
    unlink: async p => {
      unlinked.push(p)
    },
    rename: async (from, to) => {
      renamed.push([from, to])
    },
    temporaryPath: () => '/tmp/existing.bin.download'
  })

  res.emit('data', Buffer.from('abc'))
  res.emit('error', new Error('socket hang up'))

  await assert.rejects(promise, /socket hang up/)
  assert.deepEqual(unlinked, ['/tmp/existing.bin.download'])
  assert.deepEqual(renamed, [])
})

test('pumpStreamToFile removes staging and rejects if final promotion fails', async () => {
  const res = new FakeResponse()
  const ws = new FakeWriteStream()
  const unlinked: string[] = []

  const promise = pumpStreamToFile(res as never, '/tmp/existing.bin', {
    createWriteStream: () => ws as never,
    unlink: async p => {
      unlinked.push(p)
    },
    rename: async () => {
      throw new Error('rename failed')
    },
    temporaryPath: () => '/tmp/existing.bin.download'
  })

  res.emit('data', Buffer.from('complete'))
  res.emit('end')

  await assert.rejects(promise, /rename failed/)
  assert.deepEqual(unlinked, ['/tmp/existing.bin.download'])
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
  assert.equal(gatewayFilePath('',), '')
  assert.equal(gatewayFilePath(null), '')
})

test('gatewayFileRequestPaths keeps streaming and fallback requests on the same registered backend', () => {
  const paths = gatewayFileRequestPaths('/srv/output/image one.png', requestPath =>
    pathForRegistryBackendRequest(requestPath, 'research', { sharedRemote: true })
  )

  assert.deepEqual(paths, {
    dataUrl: '/api/fs/read-data-url?path=%2Fsrv%2Foutput%2Fimage+one.png&profile=research',
    download: '/api/fs/download?path=%2Fsrv%2Foutput%2Fimage+one.png&profile=research'
  })
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

test('resolveGatewayFileBackend pins registered files to their owning connection', async () => {
  const calls: string[] = []

  const route = await resolveGatewayFileBackend(
    { connectionId: '  work-ssh  ', profile: ' default ' },
    {
      ensureLegacy: async profile => {
        calls.push(`legacy:${profile}`)

        return { baseUrl: 'http://local.invalid' }
      },
      ensureRegistry: async (connectionId, profile) => {
        calls.push(`registry:${connectionId}:${profile}`)

        return { baseUrl: 'http://ssh.invalid' }
      }
    }
  )

  assert.deepEqual(calls, ['registry:work-ssh:default'])
  assert.deepEqual(route, {
    connection: { baseUrl: 'http://ssh.invalid' },
    connectionId: 'work-ssh',
    profile: 'default'
  })
})

test('resolveGatewayFileBackend preserves the legacy route when no connection owns the file', async () => {
  const calls: string[] = []

  const route = await resolveGatewayFileBackend(
    { profile: 'coder' },
    {
      ensureLegacy: async profile => {
        calls.push(`legacy:${profile}`)

        return { baseUrl: 'http://local.invalid' }
      },
      ensureRegistry: async connectionId => {
        calls.push(`registry:${connectionId}`)

        return { baseUrl: 'http://remote.invalid' }
      }
    }
  )

  assert.deepEqual(calls, ['legacy:coder'])
  assert.equal(route.connectionId, null)
  assert.equal(route.profile, 'coder')
  assert.deepEqual(route.connection, { baseUrl: 'http://local.invalid' })
})

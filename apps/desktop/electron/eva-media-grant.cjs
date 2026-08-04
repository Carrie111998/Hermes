const crypto = require('node:crypto')

const MEDIA_GRANT_TTL_MS = 60 * 60 * 1000
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

function createEvaMediaGrantCodec(options = {}) {
  const secret = Buffer.from(options.secret ?? crypto.randomBytes(32))
  const now = options.now ?? (() => Date.now())
  const ttlMs = options.ttlMs ?? MEDIA_GRANT_TTL_MS

  if (secret.length < 32) {
    throw new TypeError('Managed media grants require at least 32 bytes of secret material.')
  }

  const sign = body => crypto.createHmac('sha256', secret).update(body).digest('base64url')

  function mint(input = {}) {
    const filePath = String(input.path || '').trim()
    const profile = input.profile == null || String(input.profile).trim() === '' ? null : String(input.profile).trim()
    if (!filePath || Array.from(filePath).some(character => character.codePointAt(0) <= 0x1f)) {
      throw new TypeError('Managed media path is invalid.')
    }
    if (profile && !PROFILE_RE.test(profile)) {
      throw new TypeError('Managed media profile is invalid.')
    }

    const body = Buffer.from(
      JSON.stringify({
        expiresAt: now() + ttlMs,
        path: filePath,
        profile
      }),
      'utf8'
    ).toString('base64url')

    return `${body}.${sign(body)}`
  }

  function verify(token) {
    const [body, signature, extra] = String(token || '').split('.')
    if (!body || !signature || extra !== undefined) return null

    const expected = sign(body)
    const actualBytes = Buffer.from(signature)
    const expectedBytes = Buffer.from(expected)
    if (actualBytes.length !== expectedBytes.length || !crypto.timingSafeEqual(actualBytes, expectedBytes)) {
      return null
    }

    try {
      const parsed = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'))
      const filePath = typeof parsed.path === 'string' ? parsed.path : ''
      const profile = parsed.profile == null ? null : String(parsed.profile)
      if (
        !filePath ||
        !Number.isFinite(parsed.expiresAt) ||
        parsed.expiresAt <= now() ||
        (profile && !PROFILE_RE.test(profile))
      ) {
        return null
      }
      return { expiresAt: parsed.expiresAt, path: filePath, profile }
    } catch {
      return null
    }
  }

  return { mint, verify }
}

module.exports = { MEDIA_GRANT_TTL_MS, createEvaMediaGrantCodec }

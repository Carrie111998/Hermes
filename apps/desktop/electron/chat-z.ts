import fs from 'node:fs'
import path from 'node:path'

// Chat-Z is a local same-user transport, not a remote authentication boundary.
// UUIDs correlate a bounded request with its deep link; they are not bearer
// credentials. Spool directories/files are restricted to the current OS user,
// but another process already running as that user is intentionally inside the
// trust boundary and can ask the user's running Desktop agent to submit work.
export const CHAT_Z_VERSION = 1
export const CHAT_Z_MAX_REQUEST_BYTES = 1_100_000
export const CHAT_Z_MAX_PROMPT_CHARS = 1_000_000

const REQUEST_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export interface ChatZRequest {
  version: 1
  requestId: string
  profile: string
  text: string
  title?: string
  sessionId?: string
  newSession?: true
  newTitle?: string
  cwd?: string
  createdAt: number
  expiresAt: number
}

export interface ChatZReceipt {
  requestId: string
  status: 'accepted' | 'error'
  code?: string
  message?: string
  profile?: string
  storedSessionId?: string
  title?: string
  created?: boolean
  cwd?: string
}

export class ChatZRequestError extends Error {
  constructor(
    readonly code: string,
    message: string
  ) {
    super(message)
    this.name = 'ChatZRequestError'
  }
}

export class ChatZRequestState {
  private readonly pendingById = new Map<string, ChatZRequest>()
  private readonly inflightIds = new Set<string>()

  has(requestId: string): boolean {
    return this.pendingById.has(requestId) || this.inflightIds.has(requestId)
  }

  queue(request: ChatZRequest): void {
    this.pendingById.set(request.requestId, request)
  }

  begin(request: ChatZRequest): boolean {
    if (this.inflightIds.has(request.requestId)) {
      return false
    }

    this.inflightIds.add(request.requestId)
    this.pendingById.delete(request.requestId)

    return true
  }

  pendingRequests(): ChatZRequest[] {
    return [...this.pendingById.values()]
  }

  isInflight(requestId: string): boolean {
    return this.inflightIds.has(requestId)
  }

  complete(requestId: string): void {
    this.inflightIds.delete(requestId)
  }

  rendererLost({ dropPending = false }: { dropPending?: boolean } = {}): string[] {
    const failed = new Set(this.inflightIds)
    this.inflightIds.clear()

    if (dropPending) {
      for (const requestId of this.pendingById.keys()) {
        failed.add(requestId)
      }

      this.pendingById.clear()
    }

    return [...failed]
  }
}

export function isChatZRequestId(value: unknown): value is string {
  return typeof value === 'string' && REQUEST_ID_RE.test(value)
}

export function chatZPaths(userData: string, requestId: string): { receipt: string; request: string } {
  if (!isChatZRequestId(requestId)) {
    throw new ChatZRequestError('invalid-request-id', 'Invalid chat-z request ID')
  }

  const root = path.join(userData, 'chat-z')

  return {
    request: path.join(root, 'requests', `${requestId}.json`),
    receipt: path.join(root, 'receipts', `${requestId}.json`)
  }
}

function requiredString(value: unknown, field: string, max: number): string {
  const result = typeof value === 'string' ? value.trim() : ''

  if (!result) {
    throw new ChatZRequestError('invalid-request', `${field} is required`)
  }

  if (result.length > max) {
    throw new ChatZRequestError('request-too-large', `${field} is too long`)
  }

  return result
}

export function parseChatZRequest(raw: unknown, expectedRequestId: string, now = Date.now()): ChatZRequest {
  if (!raw || typeof raw !== 'object') {
    throw new ChatZRequestError('invalid-request', 'Request must be a JSON object')
  }

  const input = raw as Record<string, unknown>

  if (input.version !== CHAT_Z_VERSION) {
    throw new ChatZRequestError('unsupported-version', `Unsupported chat-z request version: ${String(input.version)}`)
  }

  if (input.requestId !== expectedRequestId || !isChatZRequestId(input.requestId)) {
    throw new ChatZRequestError('request-id-mismatch', 'Request ID does not match the deep link')
  }

  const title = typeof input.title === 'string' ? input.title.trim() : ''
  const sessionId = typeof input.sessionId === 'string' ? input.sessionId.trim() : ''
  const newSession = input.newSession === true
  const newTitle = typeof input.newTitle === 'string' ? input.newTitle.trim() : ''
  const cwd = typeof input.cwd === 'string' ? input.cwd.trim() : ''

  if ([Boolean(title), Boolean(sessionId), newSession].filter(Boolean).length !== 1) {
    throw new ChatZRequestError('invalid-target', 'Choose exactly one target: title, sessionId, or newSession')
  }

  if (title.length > 500 || sessionId.length > 500) {
    throw new ChatZRequestError('invalid-target', 'Target is too long')
  }

  if (newSession !== Boolean(cwd)) {
    throw new ChatZRequestError('invalid-workspace', 'newSession requires cwd, and cwd is only valid for newSession')
  }

  if (cwd.length > 4_096) {
    throw new ChatZRequestError('invalid-workspace', 'cwd is too long')
  }

  if (newTitle && !newSession) {
    throw new ChatZRequestError('invalid-title', 'newTitle is only valid for a new session')
  }

  if (newTitle.length > 500) {
    throw new ChatZRequestError('invalid-title', 'newTitle is too long')
  }

  const createdAt = Number(input.createdAt)
  const expiresAt = Number(input.expiresAt)

  if (!Number.isFinite(createdAt) || !Number.isFinite(expiresAt) || expiresAt <= createdAt) {
    throw new ChatZRequestError('invalid-request', 'Request timestamps are invalid')
  }

  if (expiresAt < now) {
    throw new ChatZRequestError('request-expired', 'The chat-z request expired before Desktop could accept it')
  }

  return {
    version: CHAT_Z_VERSION,
    requestId: input.requestId,
    profile: requiredString(input.profile, 'profile', 128),
    text: requiredString(input.text, 'text', CHAT_Z_MAX_PROMPT_CHARS),
    createdAt,
    expiresAt,
    ...(newSession ? { newSession: true, cwd, ...(newTitle ? { newTitle } : {}) } : title ? { title } : { sessionId })
  }
}

export function readChatZRequest(userData: string, requestId: string, now = Date.now()): ChatZRequest {
  const requestPath = chatZPaths(userData, requestId).request
  let stat: fs.Stats

  try {
    stat = fs.statSync(requestPath)
  } catch (error) {
    throw new ChatZRequestError('request-not-found', `Request file is unavailable: ${(error as Error).message}`)
  }

  if (!stat.isFile() || stat.size > CHAT_Z_MAX_REQUEST_BYTES) {
    throw new ChatZRequestError('request-too-large', 'Request file is invalid or too large')
  }

  try {
    return parseChatZRequest(JSON.parse(fs.readFileSync(requestPath, 'utf8')), requestId, now)
  } catch (error) {
    if (error instanceof ChatZRequestError) {
      throw error
    }

    throw new ChatZRequestError('invalid-json', `Request JSON is invalid: ${(error as Error).message}`)
  }
}

export function writeChatZReceipt(userData: string, receipt: ChatZReceipt): void {
  const receiptPath = chatZPaths(userData, receipt.requestId).receipt
  const receiptDirectory = path.dirname(receiptPath)

  fs.mkdirSync(receiptDirectory, { recursive: true, mode: 0o700 })

  if (process.platform !== 'win32') {
    fs.chmodSync(receiptDirectory, 0o700)
  }

  const temporary = path.join(receiptDirectory, `.${path.basename(receiptPath)}.${process.pid}.${Date.now()}.tmp`)

  try {
    fs.writeFileSync(temporary, JSON.stringify(receipt), { encoding: 'utf8', mode: 0o600, flag: 'wx' })
    fs.renameSync(temporary, receiptPath)
  } finally {
    try {
      fs.unlinkSync(temporary)
    } catch {
      /* renamed or never created */
    }
  }
}

export function removeChatZRequest(userData: string, requestId: string): void {
  try {
    fs.unlinkSync(chatZPaths(userData, requestId).request)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      throw error
    }
  }
}

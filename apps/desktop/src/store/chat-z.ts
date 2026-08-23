import { normalizeProfileKey } from './profile'

export interface ChatZSubmitRequest {
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

export interface ChatZSubmitReceipt {
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

interface SessionListRow {
  id: string
  resolved_id?: string
  title?: string
}
interface ChatZSubmitDeps {
  activeProfile: string
  createDesktopSession: (cwd: string) => Promise<{ runtimeSessionId: string; storedSessionId: string } | null>
  getSelectedStoredSessionId: () => null | string
  requestGateway: (method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<unknown>
  submitText: (
    text: string,
    options: { attachments: []; fromQueue?: true; sessionId?: string; storedSessionId?: string }
  ) => Promise<boolean>
}

function errorReceipt(requestId: string, code: string, message: string): ChatZSubmitReceipt {
  return { requestId, status: 'error', code, message }
}

export async function submitChatZRequest(
  request: ChatZSubmitRequest,
  deps: ChatZSubmitDeps
): Promise<ChatZSubmitReceipt> {
  const { activeProfile, createDesktopSession, getSelectedStoredSessionId, requestGateway, submitText } = deps
  const requestId = typeof request?.requestId === 'string' ? request.requestId : ''
  const text = typeof request?.text === 'string' ? request.text.trim() : ''
  const profile = normalizeProfileKey(request?.profile)
  const desktopProfile = normalizeProfileKey(activeProfile)

  if (!requestId || !text) {
    return errorReceipt(requestId, 'invalid-request', 'The chat-z request is missing text or an ID')
  }

  if (Number(request.expiresAt) < Date.now()) {
    return errorReceipt(requestId, 'request-expired', 'The chat-z request expired before the renderer accepted it')
  }

  if (profile !== desktopProfile) {
    return errorReceipt(
      requestId,
      'profile-mismatch',
      `Desktop is on profile "${desktopProfile}", but chat-z targeted "${profile}"`
    )
  }

  const title = typeof request.title === 'string' ? request.title.trim() : ''
  const requestedSessionId = typeof request.sessionId === 'string' ? request.sessionId.trim() : ''
  const newSession = request.newSession === true
  const newTitle = typeof request.newTitle === 'string' ? request.newTitle.trim() : ''
  const cwd = typeof request.cwd === 'string' ? request.cwd.trim() : ''

  if ([Boolean(title), Boolean(requestedSessionId), newSession].filter(Boolean).length !== 1) {
    return errorReceipt(requestId, 'invalid-target', 'Choose exactly one target: title, sessionId, or newSession')
  }

  if (newSession !== Boolean(cwd)) {
    return errorReceipt(requestId, 'invalid-workspace', 'newSession requires cwd, and cwd is only valid for newSession')
  }

  if (newTitle && !newSession) {
    return errorReceipt(requestId, 'invalid-title', 'newTitle is only valid for a new session')
  }

  if (newSession) {
    try {
      const created = await createDesktopSession(cwd)

      if (!created) {
        return errorReceipt(requestId, 'session-create-failed', 'Desktop could not create the requested session')
      }

      if (newTitle) {
        await requestGateway('session.title', { session_id: created.runtimeSessionId, title: newTitle })
      }

      if (
        !(await submitText(text, {
          attachments: [],
          fromQueue: true,
          sessionId: created.runtimeSessionId,
          storedSessionId: created.storedSessionId
        }))
      ) {
        return errorReceipt(requestId, 'submit-rejected', 'Desktop did not accept the prompt')
      }

      return {
        requestId,
        status: 'accepted',
        profile: desktopProfile,
        storedSessionId: created.storedSessionId,
        ...(newTitle ? { title: newTitle } : {}),
        created: true,
        cwd
      }
    } catch (error) {
      return errorReceipt(
        requestId,
        'submit-failed',
        `Desktop new-session submission failed: ${(error as Error).message}`
      )
    }
  }

  let storedSessionId = requestedSessionId

  if (title) {
    let result: { sessions?: SessionListRow[] }

    try {
      result = (await requestGateway('session.list', { title, include_hidden: true, profile: desktopProfile })) as {
        sessions?: SessionListRow[]
      }
    } catch (error) {
      return errorReceipt(
        requestId,
        'lookup-failed',
        `Could not look up the Desktop session: ${(error as Error).message}`
      )
    }

    const match = (result?.sessions ?? []).find(row => row.title === title)
    storedSessionId = (match?.resolved_id || match?.id || '').trim()

    if (!storedSessionId) {
      return errorReceipt(requestId, 'session-not-found', `No existing session has the exact title "${title}"`)
    }
  }

  try {
    if (!(await submitText(text, { attachments: [], fromQueue: true, storedSessionId }))) {
      return errorReceipt(requestId, 'submit-rejected', 'Desktop did not accept the prompt')
    }
  } catch (error) {
    return errorReceipt(requestId, 'submit-failed', `Desktop prompt submission failed: ${(error as Error).message}`)
  }

  return { requestId, status: 'accepted', profile: desktopProfile, storedSessionId, ...(title ? { title } : {}) }
}

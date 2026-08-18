export interface NativeNotificationAction {
  id?: string
  text?: string
}

export interface NativeNotificationAuthority {
  approvalConnectionId?: null | string
  approvalProfile?: string
  approvalRequestId?: string
  sessionId?: string
  kind?: string
  tag?: string
}

export function nativeNotificationDedupeKey(payload: NativeNotificationAuthority): string {
  if (payload.kind === 'approval') {
    return JSON.stringify([
      payload.kind,
      payload.sessionId ?? null,
      payload.approvalConnectionId ?? null,
      payload.approvalProfile ?? null,
      payload.approvalRequestId ?? null
    ])
  }

  return `${payload.kind ?? ''}:${payload.sessionId ?? payload.tag ?? ''}`
}

export interface NativeNotificationActionSender {
  isDestroyed: () => boolean
  send: (channel: string, payload: Record<string, unknown>) => void
}

/** Return a body click to the renderer/source that created its notification. */
export function sendNativeNotificationFocus(
  sender: NativeNotificationActionSender,
  payload: NativeNotificationAuthority
): void {
  if (sender.isDestroyed() || !payload.sessionId) {
    return
  }

  sender.send('hermes:focus-session', {
    connectionId: payload.approvalConnectionId,
    profile: payload.approvalProfile,
    requestId: payload.approvalRequestId,
    sessionId: payload.sessionId
  })
}

/** Return one native action to the renderer that created its notification. */
export function sendNativeNotificationAction(
  sender: NativeNotificationActionSender,
  payload: NativeNotificationAuthority,
  actions: NativeNotificationAction[],
  index: number
): void {
  if (sender.isDestroyed()) {
    return
  }

  const action = actions[index]

  if (!action?.id) {
    return
  }

  sender.send('hermes:notification-action', {
    actionId: action.id,
    connectionId: payload.approvalConnectionId,
    profile: payload.approvalProfile,
    requestId: payload.approvalRequestId,
    sessionId: payload.sessionId
  })
}

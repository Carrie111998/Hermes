import { useEffect, useRef } from 'react'

import { submitChatZRequest } from '@/store/chat-z'
import { isAuxiliaryWindow } from '@/store/windows'

import type { usePromptActions } from '../../session/hooks/use-prompt-actions'
import type { GatewayRequester } from '../types'

interface ChatZBridgeParams {
  activeProfile: string
  createDesktopSession: (cwd: string) => Promise<{ runtimeSessionId: string; storedSessionId: string } | null>
  getSelectedStoredSessionId: () => null | string
  requestGateway: GatewayRequester
  submitText: ReturnType<typeof usePromptActions>['submitText']
}

export function useChatZBridge({
  activeProfile,
  createDesktopSession,
  getSelectedStoredSessionId,
  requestGateway,
  submitText
}: ChatZBridgeParams): void {
  const activeProfileRef = useRef(activeProfile)
  activeProfileRef.current = activeProfile
  const createDesktopSessionRef = useRef(createDesktopSession)
  createDesktopSessionRef.current = createDesktopSession
  const requestGatewayRef = useRef(requestGateway)
  requestGatewayRef.current = requestGateway
  const getSelectedStoredSessionIdRef = useRef(getSelectedStoredSessionId)
  getSelectedStoredSessionIdRef.current = getSelectedStoredSessionId
  const submitTextRef = useRef(submitText)
  submitTextRef.current = submitText
  const submissionTailRef = useRef<Promise<void>>(Promise.resolve())

  // eslint-disable-next-line no-restricted-syntax -- promise tail serializes external submissions, not reactive state.
  useEffect(() => {
    if (isAuxiliaryWindow()) {
      return
    }

    const unsubscribe = window.hermesDesktop?.chatZ?.onSubmit(request => {
      submissionTailRef.current = submissionTailRef.current
        .catch(() => undefined)
        .then(async () => {
          try {
            const receipt = await submitChatZRequest(request, {
              activeProfile: activeProfileRef.current,
              createDesktopSession: createDesktopSessionRef.current,
              getSelectedStoredSessionId: getSelectedStoredSessionIdRef.current,
              requestGateway: requestGatewayRef.current,
              submitText: submitTextRef.current
            })

            window.hermesDesktop.chatZ.complete(receipt)
          } catch (error) {
            window.hermesDesktop.chatZ.complete({
              requestId: request?.requestId ?? '',
              status: 'error',
              code: 'renderer-error',
              message: (error as Error).message
            })
          }
        })
    })

    void window.hermesDesktop?.chatZ?.ready()

    return () => unsubscribe?.()
  }, [])
}

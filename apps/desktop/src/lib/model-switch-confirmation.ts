import { confirm } from '@/store/confirm'

export interface ConfigSetModelResponse {
  confirm_message?: string
  confirm_required?: boolean
  deferred?: boolean
  scope?: string
  value?: string
  warning?: string
}

export interface AppliedModelSwitch {
  result: ConfigSetModelResponse
  status: 'applied'
}

export interface CancelledModelSwitch {
  status: 'cancelled'
}

export type ModelSwitchOutcome = AppliedModelSwitch | CancelledModelSwitch

type GatewayRequest = <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

export async function requestConfirmedModelSwitch(
  requestGateway: GatewayRequest,
  params: { session_id: string; key: 'model'; value: string }
): Promise<ModelSwitchOutcome> {
  const request = (confirmed = false) =>
    requestGateway<ConfigSetModelResponse>('config.set', {
      ...(confirmed ? { confirm_expensive_model: true } : {}),
      ...params
    })

  let result = await request()

  if (result.confirm_required) {
    const accepted = await confirm({
      confirmLabel: 'Switch anyway',
      description: result.confirm_message || result.warning || 'This model requires confirmation.',
      destructive: true,
      title: 'Confirm model switch'
    })

    if (!accepted) {
      return { status: 'cancelled' }
    }

    result = await request(true)
    if (result.confirm_required) {
      throw new Error(result.confirm_message || result.warning || 'Model switch confirmation was not accepted.')
    }
  }

  return { result, status: 'applied' }
}

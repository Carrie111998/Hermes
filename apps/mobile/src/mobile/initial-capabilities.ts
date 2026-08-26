/** A non-secret, one-time marker so first-connection capability requests do not nag on every launch. */
export const INITIAL_MOBILE_CAPABILITIES_KEY = 'hermes.mobile.initial-capabilities.requested.v1'

export function shouldRequestInitialMobileCapabilities(value: null | string): boolean {
  return value !== 'requested'
}

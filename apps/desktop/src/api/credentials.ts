import type { CredentialPoolResponse } from '@/types/hermes'

import { hermesApi, profileScoped } from './client'

export function getCredentialPool(profile?: string): Promise<CredentialPoolResponse> {
  return hermesApi<CredentialPoolResponse>({
    ...profileScoped(profile),
    path: '/api/credentials/pool'
  })
}

export function renameCredentialPoolEntry(
  provider: string,
  credentialId: string,
  label: string,
  profile?: string
): Promise<{ id: string; label: string; ok: boolean; provider: string }> {
  return hermesApi({
    ...profileScoped(profile),
    path: `/api/credentials/pool/${encodeURIComponent(provider)}/${encodeURIComponent(credentialId)}`,
    method: 'PATCH',
    body: { label }
  })
}

export function removeCredentialPoolEntry(
  provider: string,
  credentialId: string,
  profile?: string
): Promise<{ count: number; id: string; ok: boolean; provider: string }> {
  return hermesApi({
    ...profileScoped(profile),
    path: `/api/credentials/pool/${encodeURIComponent(provider)}/id/${encodeURIComponent(credentialId)}`,
    method: 'DELETE'
  })
}

export function setCredentialPoolStrategy(
  provider: string,
  strategy: string,
  profile?: string
): Promise<{ ok: boolean; provider: string; strategy: string }> {
  return hermesApi({
    ...profileScoped(profile),
    path: `/api/credentials/pool/${encodeURIComponent(provider)}/strategy`,
    method: 'PUT',
    body: { strategy }
  })
}

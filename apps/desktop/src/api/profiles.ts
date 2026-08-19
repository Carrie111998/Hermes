import type {
  ProfileCreatePayload,
  ProfileDesktopOverlay,
  ProfileSetupCommand,
  ProfileSoul,
  ProfilesResponse
} from '@/types/hermes'

import { hermesApi, STARTUP_REQUEST_TIMEOUT_MS } from './client'


/** Explicit connection override for the profile-admin endpoints.
 *
 *  Every other helper is AMBIENT: `hermesApi` spreads `connectionScoped()`, so a
 *  request follows whichever gateway is live. That is right for these too — the
 *  Profiles panel of a window pointed at one machine should describe that
 *  machine.
 *
 *  Manage Profiles is the exception: it lists EVERY registered gateway at once,
 *  so each row has to address the box it came from rather than the one that
 *  happens to be live. Passing the id overrides the ambient scope for that one
 *  call; passing nothing keeps the byte-identical ambient request every other
 *  caller makes. */
function profileAdminScope(connectionId?: null | string): { connectionId?: string } {
  const id = (connectionId ?? '').trim()

  return id ? { connectionId: id } : {}
}

export function getProfiles(connectionId?: null | string): Promise<ProfilesResponse> {
  return hermesApi<ProfilesResponse>({
    ...profileAdminScope(connectionId),
    path: '/api/profiles',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function createProfile(
  body: ProfileCreatePayload,
  connectionId?: null | string
): Promise<{ name: string; ok: boolean; path: string }> {
  return hermesApi<{ name: string; ok: boolean; path: string }>({
    ...profileAdminScope(connectionId),
    path: '/api/profiles',
    method: 'POST',
    body
  })
}

export function renameProfile(
  name: string,
  newName: string,
  connectionId?: null | string
): Promise<{ name: string; ok: boolean; path: string }> {
  return hermesApi<{ name: string; ok: boolean; path: string }>({
    ...profileAdminScope(connectionId),
    path: `/api/profiles/${encodeURIComponent(name)}`,
    method: 'PATCH',
    body: { new_name: newName }
  })
}

export function deleteProfile(name: string, connectionId?: null | string): Promise<{ ok: boolean; path: string }> {
  return hermesApi<{ ok: boolean; path: string }>({
    ...profileAdminScope(connectionId),
    path: `/api/profiles/${encodeURIComponent(name)}`,
    method: 'DELETE'
  })
}

export function getProfileSoul(name: string, connectionId?: null | string): Promise<ProfileSoul> {
  return hermesApi<ProfileSoul>({
    ...profileAdminScope(connectionId),
    path: `/api/profiles/${encodeURIComponent(name)}/soul`
  })
}

export function updateProfileSoul(
  name: string,
  content: string,
  connectionId?: null | string
): Promise<{ ok: boolean }> {
  return hermesApi<{ ok: boolean }>({
    ...profileAdminScope(connectionId),
    path: `/api/profiles/${encodeURIComponent(name)}/soul`,
    method: 'PUT',
    body: { content }
  })
}

export function getProfileSetupCommand(name: string): Promise<ProfileSetupCommand> {
  return hermesApi<ProfileSetupCommand>({
    path: `/api/profiles/${encodeURIComponent(name)}/setup-command`
  })
}

/** Export a profile to a shareable .tar.gz on the backend's filesystem.
 *  `extraFiles` stages extra root-level files (desktop.json — the appearance/
 *  interface overlay) into the archive alongside the profile's own artifacts. */
export function exportProfileArchive(
  name: string,
  opts: { extraFiles?: Record<string, string>; output?: string } = {}
): Promise<{ archive: string; ok: boolean }> {
  return hermesApi<{ archive: string; ok: boolean }>({
    path: `/api/profiles/${encodeURIComponent(name)}/export`,
    method: 'POST',
    body: { extra_files: opts.extraFiles ?? {}, output: opts.output ?? '' },
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

/** Import a profile .tar.gz as a new profile. Returns the bundled desktop
 *  appearance overlay too (when the archive carried one) so the caller can
 *  apply theme/layout without another round-trip. */
export function importProfileArchive(
  archive: string,
  name?: string
): Promise<{ desktop: null | ProfileDesktopOverlay; name: string; ok: boolean; path: string }> {
  return hermesApi<{ desktop: null | ProfileDesktopOverlay; name: string; ok: boolean; path: string }>({
    path: '/api/profiles/import',
    method: 'POST',
    body: { archive, name: name || null },
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

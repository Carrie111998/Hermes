/**
 * Boundary helpers between profile switching and the projects cache.
 *
 * Imports only gateway-free project state so unit tests that mock part of
 * `@/store/gateway` can load `profile.ts` without needing a full gateway mock.
 */

export { clearProjectsCacheForProfileSwitch } from '@/store/projects-cache-state'

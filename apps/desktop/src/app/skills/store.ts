import { Codecs, persistentAtom } from '@/lib/persisted'

// Per-view sort direction for the Capabilities lists — persisted so each tab
// remembers most/least-used across navigations and restarts.
export const $skillsSortDesc = persistentAtom('hermes.desktop.capabilities.skillsSortDesc', true, Codecs.bool)
export const $toolsetsSortDesc = persistentAtom('hermes.desktop.capabilities.toolsetsSortDesc', true, Codecs.bool)

// Skills list layout: flat (default) or grouped into category sections.
// Persisted so a 100+ skill install stays grouped across restarts once set.
export const $skillsGroupedView = persistentAtom('hermes.desktop.capabilities.skillsGroupedView', false, Codecs.bool)

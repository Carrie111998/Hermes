export const wisdomCopy = {
  tab: 'Collective',
  browseHub: 'Browse hub',
  title: 'Collective Wisdom',
  loading: 'Loading Collective Wisdom',
  unavailable: 'Collective Wisdom is unavailable.',
  setup: 'Run hermes wisdom setup to verify this profile.',
  search: 'Search shared skills…',
  potential: 'Potential contributions',
  ownerReview: 'Owner review',
  noDrafts: 'No server drafts awaiting review.',
  prepare: 'Prepare',
  localOnly: 'Manual owner selection · local-only reasons',
  reviewExact: 'Review exact bytes',
  serverScanPassed: 'server scan passed',
  noDescription: 'No owner-authored description.',
  managedInstalls: 'managed installs',
  close: 'Close',
  readEvery: 'Read every raw file. Approval is bound to the exact three hashes shown below.',
  prepareTitle: 'Review local package before upload',
  prepareNotice:
    'These fields stay on this profile until you explicitly submit them for owner-only server review. Local qualification counts and reasons are never included.',
  ownerDescription: 'Owner-authored description',
  systemSpecification: 'System Specification (declarative metadata; no dependencies are installed)',
  localOverlay: 'Local overlay',
  cancel: 'Cancel',
  submit: 'Submit for owner-only server review',
  submitting: 'Submitting…',
  publishing: 'Publishing…',
  approve: 'Approve exact content & publish'
} as const

export type WisdomCopy = typeof wisdomCopy

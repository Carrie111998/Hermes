import { atom } from 'nanostores'

/** Presentation-only state for the project artifacts tool pane. A separate
 * request pulse lets the layout front an already-open pane without coupling
 * artifact UI to the pane-tree implementation. */
export const $artifactsPaneOpen = atom(false)
export const $artifactsPaneRevealRequest = atom(0)

export function openArtifactsPane(): void {
  $artifactsPaneOpen.set(true)
  $artifactsPaneRevealRequest.set($artifactsPaneRevealRequest.get() + 1)
}

export function closeArtifactsPane(): void {
  $artifactsPaneOpen.set(false)
}

export function toggleArtifactsPane(): void {
  if ($artifactsPaneOpen.get()) {
    closeArtifactsPane()
  } else {
    openArtifactsPane()
  }
}

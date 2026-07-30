export const REF_RE: RegExp
export const BARE_PATH_RE: RegExp
export function stripLeakedBracketedPasteWrappers(text: string): string
export function collapseRepeatedInputArtifacts(
  text: string,
  minRepeats?: number
): string
export function sanitizeComposerInput(text: string): string
export function barePathRef(path: string): string | null
export function pathifyRefs(text: string): string
export function canonicalComposerSubmitText(text: string): string

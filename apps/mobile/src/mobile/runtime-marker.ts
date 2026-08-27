/**
 * This must run before any reused Desktop module evaluates: pane layout chooses
 * its narrow/drawer mode at module load, while an unfolded Fold can exceed the
 * desktop CSS breakpoint. Kept dependency-free so importing it first is enough.
 */
export function markNativeMobileRenderer(target?: Document): void {
  const documentTarget = target ?? (typeof document === 'undefined' ? undefined : document)

  documentTarget?.documentElement.setAttribute('data-hermes-mobile', '')
}

markNativeMobileRenderer()

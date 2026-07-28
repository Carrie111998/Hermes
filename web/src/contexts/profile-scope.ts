/**
 * Resolve the dashboard's first profile scope.
 *
 * An explicit deep-link scope always wins. The launch profile is only the
 * default for routes such as `/chat?resume=...` that omit `?profile=`.
 */
export function initialProfileScope(
  urlProfile: string | null,
  launchProfile: string | undefined,
): string {
  return urlProfile ?? launchProfile ?? "";
}

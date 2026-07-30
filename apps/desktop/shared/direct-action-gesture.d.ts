export const RICH_INPUT_SLOT: string
export function composerPlainText(node: Node): string
export function eligibleComposerText(editor: HTMLElement | null): string | null
export function directActionGestureText(event: Event): string | null
export function installDirectActionGestureCapture(
  target: Window,
  begin: (canonicalText: string) => void
): () => void

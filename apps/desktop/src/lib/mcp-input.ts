export interface McpInputEventLike {
  currentTarget: { value?: unknown } | null
}

/**
 * Read an MCP credential input synchronously, before a React state updater can
 * run after the browser event has finished and cleared currentTarget.
 */
export function readMcpInputValue(event: McpInputEventLike): string {
  const value = event.currentTarget?.value

  return typeof value === 'string' ? value : ''
}

/**
 * Model switch payload shared by the in-app model picker and the plugin SDK's
 * `host.selectModel` action. Lives in a neutral contract module so the public
 * SDK surface is not coupled to a UI component.
 */
export interface ModelSelection {
  model: string
  provider: string
  /** Runtime id of the surface that opened the menu. When set, the switch
   *  targets that session (a tile) instead of the primary `$activeSessionId`. */
  sessionId?: null | string
}

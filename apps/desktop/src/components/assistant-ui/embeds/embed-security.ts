// Provider pages are untrusted remote content. Scripts and same-origin keep
// the provider runtimes functional, while deliberately omitting allow-popups
// and allow-top-navigation prevents an embedded page from creating a native
// window or escaping the embed. Explicit app links use the Desktop bridge.
export const EXTERNAL_FRAME_SANDBOX = 'allow-same-origin allow-scripts allow-presentation'

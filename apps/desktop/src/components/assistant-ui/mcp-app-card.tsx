import { type ToolCallMessagePartProps } from '@assistant-ui/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { requestMcpAppUserMessage, stageModelContext } from '@/store/mcp-app'

// MCP Apps host-side rendering.
//
// An MCP Apps server (the io.modelcontextprotocol/ui extension) returns an
// interactive HTML card inline in a tool result's `_meta.ui`. The Python MCP
// client stashes it out-of-band and the gateway forwards it on `tool.complete`
// as `result.ui = { server, uri, mimeType, html, csp }`. We render that HTML in
// a sandboxed iframe and bridge the card's JSON-RPC-over-postMessage frames to
// the MCP server session via the `mcp.app.request` gateway method.

interface McpUiCsp {
    scriptSrc?: string
    connectDomains?: string[]
    resourceDomains?: string[]
    allowUnsafeEval?: boolean
}

interface McpUiPayload {
    server: string
    uri: string
    html: string
    mimeType?: string
    csp?: McpUiCsp
}

interface JsonRpcFrame {
    jsonrpc?: string
    id?: number | string | null
    method?: string
    params?: unknown
    result?: unknown
    error?: unknown
}

const MAX_CARD_HEIGHT = 2000
const HOST_PROTOCOL_VERSION = '2026-01-26'
const HOST_INFO = { name: 'Hermes', version: '0.16.0' }

function readMcpUi(result: unknown): McpUiPayload | null {
    if (!result || typeof result !== 'object') {
        return null
    }

    const ui = (result as { ui?: unknown }).ui

    if (!ui || typeof ui !== 'object') {
        return null
    }

    const u = ui as Record<string, unknown>

    if (typeof u.server !== 'string' || typeof u.html !== 'string' || typeof u.uri !== 'string') {
        return null
    }

    return {
        server: u.server,
        uri: u.uri,
        html: u.html,
        mimeType: typeof u.mimeType === 'string' ? u.mimeType : undefined,
        csp: u.csp && typeof u.csp === 'object' ? (u.csp as McpUiCsp) : undefined
    }
}

/** True when a tool result carries an MCP Apps UI card (drives component selection). */
export function hasMcpUi(result: unknown): boolean {
    return readMcpUi(result) !== null
}

/**
 * Build a Content-Security-Policy from the server-declared `_meta.ui.csp`.
 * The server tailors this to its own card (e.g. allowing image/CDN domains),
 * so applying it both hardens the sandboxed iframe and permits what the card
 * legitimately needs.
 */
function buildCsp(csp: McpUiCsp): string {
    const res = (csp.resourceDomains ?? []).join(' ').trim()
    const conn = (csp.connectDomains ?? []).join(' ').trim()
    const script = (csp.scriptSrc || "'unsafe-inline' 'unsafe-eval'").trim()

    return [
        "default-src 'none'",
        `script-src ${script}`,
        `style-src 'unsafe-inline' ${res}`.trim(),
        `img-src data: blob: ${res}`.trim(),
        `font-src data: ${res}`.trim(),
        `media-src ${res || "'none'"}`.trim(),
        `connect-src ${conn || "'none'"}`.trim(),
        "base-uri 'none'",
        'form-action *'
    ].join('; ')
}

function injectCsp(html: string, csp?: McpUiCsp): string {
    if (!csp) {
        return html
    }

    const meta = `<meta http-equiv="Content-Security-Policy" content="${buildCsp(csp).replace(/"/g, '&quot;')}">`

    if (/<head[^>]*>/i.test(html)) {
        return html.replace(/<head[^>]*>/i, match => `${match}${meta}`)
    }

    return `${meta}${html}`
}

function applyNotifySize(params: unknown, setHeight: (h: number) => void): void {
    const p = (params ?? {}) as Record<string, unknown>
    const size = (p.size ?? {}) as Record<string, unknown>
    const raw = typeof p.height === 'number' ? p.height : typeof size.height === 'number' ? size.height : undefined

    if (typeof raw === 'number' && raw > 0) {
        setHeight(Math.min(Math.ceil(raw), MAX_CARD_HEIGHT))
    }
}

/**
 * Wire-shaped tool result handed to the card as `ui/initialize`'s
 * `result.lastToolResult`. Referenced-form cards (utp catalog v5 React) render
 * their initial view from it (products list from `structuredContent`), instead
 * of falling back to an empty search page. Returns undefined when the tool
 * result carries no structured data worth pushing.
 */
function buildLastToolResult(result: unknown): Record<string, unknown> | undefined {
    if (!result || typeof result !== 'object') {
        return undefined
    }

    const r = result as Record<string, unknown>

    if (r.structuredContent === undefined || r.error) {
        return undefined
    }

    const text = typeof r.result === 'string' ? r.result : ''

    return {
        content: text ? [{ type: 'text', text }] : [],
        structuredContent: r.structuredContent,
        isError: false
    }
}

/** Session id (utp cart isolation) from the tool result's structuredContent. */
function readSessionId(result: unknown): string | undefined {
    const structured = (result as { structuredContent?: unknown } | null)?.structuredContent

    if (!structured || typeof structured !== 'object') {
        return undefined
    }

    const sid = (structured as Record<string, unknown>).session_id

    return typeof sid === 'string' && sid ? sid : undefined
}

function toMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error)
}

/**
 * Join the text blocks of a host-protocol content field. The spec types
 * `ui/message` content as a single ContentBlock; live cards (utp) send an
 * array. Accept both.
 */
function extractTextContent(params: unknown): string {
    const raw = ((params ?? {}) as { content?: unknown }).content
    const blocks = Array.isArray(raw) ? raw : raw ? [raw] : []

    return blocks
        .map(block =>
            block && typeof block === 'object' && typeof (block as { text?: unknown }).text === 'string'
                ? (block as { text: string }).text
                : ''
        )
        .filter(Boolean)
        .join('\n')
}

export function McpAppCard({ result, toolCallId }: ToolCallMessagePartProps) {
    const ui = useMemo(() => readMcpUi(result), [result])
    const { requestGateway } = useGatewayRequest()
    const iframeRef = useRef<HTMLIFrameElement>(null)
    const [height, setHeight] = useState(420)

    const srcDoc = useMemo(() => (ui ? injectCsp(ui.html, ui.csp) : ''), [ui])

    // Bridge. The card speaks the MCP Apps host protocol over postMessage:
    //   * `ui/*` methods are HOST-directed and answered locally here
    //     (`ui/initialize` handshake; `ui/notifications/size-changed` resize).
    //   * `tools/call` / `resources/*` are proxied to the MCP server session via
    //     the gateway `mcp.app.request` method.
    // Method names + shapes were confirmed against the live utp card (L3).
    const server = ui?.server ?? ''

    // Keep the latest result accessible inside the stable onMessage closure
    // without putting `result` in the effect deps. Tool call results in
    // @assistant-ui/react are replaced on every streaming update; if `result`
    // were a dep, the listener would be torn down and rebuilt on each chunk,
    // and any postMessage arriving in that synchronous gap is lost.
    const resultRef = useRef(result)
    resultRef.current = result

    useEffect(() => {
        if (!server) {
            return
        }

        const onMessage = (event: MessageEvent) => {
            const iframe = iframeRef.current

            if (!iframe || event.source !== iframe.contentWindow) {
                if (import.meta.env.DEV) {
                    // Debug: identify which component is dropping and why
                    const domIframes = document.querySelectorAll('iframe')
                    let domIdx = -1
                    for (let i = 0; i < domIframes.length; i++) {
                        if (domIframes[i] === iframe) { domIdx = i; break }
                    }
                    let sourceIdx = -1
                    for (let i = 0; i < domIframes.length; i++) {
                        try { if (event.source === domIframes[i].contentWindow) { sourceIdx = i; break } } catch {}
                    }
                    console.warn('[mcp-app] dropped message: source mismatch', {
                        toolCallId,
                        hasIframe: !!iframe,
                        iframeInDom: domIdx >= 0,
                        domIdx,
                        sourceIdx,
                        iframeTitle: iframe?.title?.substring(0, 40) || null,
                    })
                }
                return
            }

            const msg = event.data as JsonRpcFrame

            if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') {
                return
            }

            if (import.meta.env.DEV) {
                console.debug('[mcp-app<-card]', msg)
            }

            const method = typeof msg.method === 'string' ? msg.method : ''
            const hasId = msg.id !== undefined && msg.id !== null

            const reply = (payload: Record<string, unknown>) => {
                const frame = { jsonrpc: '2.0', id: msg.id, ...payload }

                if (import.meta.env.DEV) {
                    console.debug('[mcp-app->card]', frame)
                }

                iframe.contentWindow?.postMessage(frame, '*')
            }

            // Host-directed `ui/*` methods are handled locally, never proxied.
            if (method.startsWith('ui/')) {
                if (method === 'ui/notifications/size-changed') {
                    applyNotifySize(msg.params, setHeight)

                    return
                }

                // `ui/update-model-context`: SILENT per-view state snapshot for
                // the model — overwrite semantics, delivered as a hidden prefix
                // of the next outgoing user message (see src/store/mcp-app.ts).
                if (method === 'ui/update-model-context') {
                    stageModelContext(toolCallId || `${server}:${ui?.uri ?? ''}`, extractTextContent(msg.params))

                    if (hasId) {
                        reply({ result: {} })
                    }

                    return
                }

                // `ui/message`: a conversation message that triggers a follow-up
                // turn. Only ITS text is user-visible; staged context rides along
                // invisibly on the send path.
                if (method === 'ui/message') {
                    requestMcpAppUserMessage(extractTextContent(msg.params), toolCallId)

                    if (hasId) {
                        reply({ result: {} })
                    }

                    return
                }

                if (hasId) {
                    const params = (msg.params ?? {}) as Record<string, unknown>

                    if (method === 'ui/initialize') {
                        const lastToolResult = buildLastToolResult(resultRef.current)
                        const sessionId = readSessionId(resultRef.current)

                        reply({
                            result: {
                                protocolVersion:
                                    typeof params.protocolVersion === 'string' ? params.protocolVersion : HOST_PROTOCOL_VERSION,
                                hostInfo: HOST_INFO,
                                // Declared per spec so views can feature-detect
                                // the card→model channels we implement.
                                hostCapabilities: {
                                    updateModelContext: { text: {} },
                                    message: { text: {} }
                                },
                                ...(lastToolResult ? { lastToolResult } : {}),
                                ...(sessionId ? { sessionId } : {})
                            }
                        })
                    } else {
                        reply({ result: {} })
                    }
                }

                return
            }

            // Non-`ui/*` notifications carry no id and expect no response.
            if (!hasId) {
                return
            }
            // Requests (tools/call, resources/*) proxy to the MCP server session.
            void (async () => {
                try {
                    // Inject session_id into tools/call arguments when the card
                    // omitted it. UTP (and similar servers) require session_id
                    // on every call, but cards built from older SDKs don't
                    // forward the sessionId received in ui/initialize. The host
                    // has it on the original tool result's structuredContent.
                    let outgoingMsg = msg
                    if (method === 'tools/call') {
                        const params = (msg.params ?? {}) as Record<string, unknown>
                        const args = (params.arguments ?? {}) as Record<string, unknown>
                        if (args.session_id === undefined) {
                            const sid = readSessionId(resultRef.current)
                            if (sid) {
                                outgoingMsg = {
                                    ...msg,
                                    params: { ...params, arguments: { ...args, session_id: sid } },
                                }
                            }
                        }
                    }

                    const res = await requestGateway<{ response?: JsonRpcFrame }>('mcp.app.request', {
                        server,
                        toolCallId,
                        message: outgoingMsg
                    })

                    const response = res?.response

                    if (response) {
                        if (import.meta.env.DEV) {
                            console.debug('[mcp-app->card]', response)
                        }

                        iframe.contentWindow?.postMessage(response, '*')
                    }
                } catch (error) {
                    console.warn('[mcp-app] gateway request failed:', toMessage(error))
                    reply({ error: { code: -32000, message: toMessage(error) } })
                }
            })()
        }

        window.addEventListener('message', onMessage)

        return () => window.removeEventListener('message', onMessage)
        // `result` is intentionally excluded — see resultRef comment above.
    }, [server, requestGateway, toolCallId, ui?.uri])

    if (!ui) {
        return null
    }

    return (
        <div className="my-2 overflow-hidden rounded-lg border border-border bg-background">
            <iframe
                className="w-full"
                ref={iframeRef}
                sandbox="allow-scripts allow-forms allow-popups"
                srcDoc={srcDoc}
                style={{ border: 'none', height }}
                title={ui.uri}
                data-tool-call-id={toolCallId}
            />
        </div>
    )
}

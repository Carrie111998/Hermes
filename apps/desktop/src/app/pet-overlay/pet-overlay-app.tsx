import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { PetHeartField, playVibeHearts } from '@/components/chat/vibe-hearts'
import { AvatarPackRenderer } from '@/components/pet/avatar-pack-renderer'
import { PetBubble } from '@/components/pet/pet-bubble'
import { PetSprite } from '@/components/pet/pet-sprite'
import { type PetZoomAnchor, usePetZoomGesture } from '@/components/pet/use-pet-zoom-gesture'
import { Mail } from '@/lib/icons'
import { activityToAvatarState } from '@/store/avatar-pack-store'
import {
  ALL_AVATAR_STATES,
  AVATAR_STATE_LABELS,
  type AvatarPackListResult,
  type AvatarRendererType,
  type AvatarState,
  type ResolvedAvatarPack
} from '@/store/avatar-pack-types'
import { $petActivity, $petInfo, setPetInfo } from '@/store/pet'
import {
  AVATAR_MODE_LABELS,
  AVATAR_OPACITY_LABELS,
  AVATAR_OPACITY_VALUES,
  AVATAR_SIZE_LABELS,
  AVATAR_SIZE_SCALES,
  type AvatarMode,
  type AvatarOpacityPreset,
  type AvatarSizePreset,
  overlayWindowSize
} from '@/store/pet-overlay'
import { $busy, setAwaitingResponse, setBusy } from '@/store/session'

import { useOverlayVoiceLoop } from './overlay-voice-loop'

// Fallbacks mirror pet-sprite's defaults; the gateway normally sends real values.
const DEFAULT_FRAME_W = 192
const DEFAULT_FRAME_H = 208
const DEFAULT_SCALE = 0.33

// Must match the root's paddingBottom — the sprite renders bottom-centered, this
// many px above the window's bottom edge. Used to anchor the resize.
const PET_PADDING_BOTTOM = 24

// A sprite pixel counts as "solid" (interactive) at/above this alpha (0-255).
// Low enough to catch anti-aliased edges, high enough that the faint halo around
// the art still clicks through.
const ALPHA_HIT_THRESHOLD = 16

/**
 * The pop-out overlay's main view: a transparent, draggable mascot with a mini
 * composer, context menu, and settings panel.
 *
 * This runs in a separate, gateway-less BrowserWindow (`?win=overlay`). It is a
 * pure puppet — the main renderer pushes the live pet state over IPC and we
 * mirror it into the same atoms the in-window pet reads, so `PetSprite` /
 * `PetBubble` render identically with zero extra logic.
 *
 * P0 additions (2026-07-28):
 * - Right-click context menu: Chat, Voice replies toggle, Hide, Size submenu,
 *   Opacity submenu, Settings, Quit.
 * - Size presets (Mini → Large) with instant apply + persist.
 * - Opacity presets (Solid / Soft / Ghost) with instant apply + persist.
 * - Settings panel showing gateway status and active session ID.
 * - Composer upgraded with multi-line input + sent messages display.
 */

// Below this much pointer travel, a press counts as a click, not a drag.
const CLICK_SLOP_PX = 3
// A second click within this window is a double-click (open chat) and cancels
// the deferred single-click (open composer).
const DOUBLE_CLICK_MS = 250

interface DragState {
  startX: number
  startY: number
  offX: number
  offY: number
  width: number
  height: number
  moved: boolean
}

// ── Context menu component ───────────────────────────────────────────────────

interface ChatMessage {
  id: number
  text: string
  role: 'user' | 'assistant'
  ts: number
}

let msgIdCounter = 0

export function PetOverlayApp() {
  const info = useStore($petInfo)
  // P1: Subscribe to live activity for avatar pack state derivation.
  const liveActivity = useStore($petActivity)
  const liveBusy = useStore($busy)
  const [composerOpen, setComposerOpen] = useState(false)
  const [draft, setDraft] = useState('')
  // Mirrored from the main renderer: a finish landed while you were away.
  const [unread, setUnread] = useState(false)
  // Chat messages (local to overlay — P0 simple display).
  const [messages, setMessages] = useState<ChatMessage[]>([])
  // Context menu state.
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null)
  // Settings panel open state.
  const [settingsOpen, setSettingsOpen] = useState(false)

  // Avatar display prefs mirrored from main renderer.
  const [avatarSize, setAvatarSizeLocal] = useState<AvatarSizePreset>('small')
  const [avatarOpacity, setAvatarOpacityLocal] = useState<AvatarOpacityPreset>('solid')
  const [voiceReplies, setVoiceRepliesLocal] = useState(false)
  const [avatarHidden, setAvatarHiddenLocal] = useState(false)
  const [avatarMode, setAvatarModeLocal] = useState<AvatarMode>('desktop')
  const [gatewayStatus, setGatewayStatus] = useState('idle')
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  // P1: Avatar pack renderer state (mirrored from main renderer).
  const [avatarRendererType, setAvatarRendererTypeLocal] = useState<AvatarRendererType>('petdex')
  const [selectedAvatarPack, setSelectedAvatarPackLocal] = useState<ResolvedAvatarPack | null>(null)
  const [avatarPreviewState, setAvatarPreviewStateLocal] = useState<AvatarState | null>(null)
  const [avatarPackList, setAvatarPackListLocal] = useState<AvatarPackListResult | null>(null)

  // P1 Voice: Track the latest assistant reply for TTS auto-speak.
  // The main renderer pushes assistant messages via the 'submit'→gateway path,
  // but we can't see the reply directly. Instead, we mirror `busy` transitions:
  // when busy goes false after being true, the reply just landed.
  const prevBusyRef = useRef(false)
  const [replyId, setReplyId] = useState(0)
  const [lastReply, setLastReply] = useState<string | null>(null)
  // P1.5: Nonce for duplicate TTS guard — tracks which assistant message
  // we've already seen in the overlay so a re-push of the same message id
  // (e.g. from a non-busy atom subscription) doesn't re-read the text.
  const lastSpokenAssistantMsgIdRef = useRef<string | null>(null)

  // Submit handler: sends text to main renderer via IPC.
  const handleSubmit = useCallback((text: string) => {
    setMessages(prev => [...prev, { id: ++msgIdCounter, text, role: 'user', ts: Date.now() }])
    window.hermesDesktop?.petOverlay?.control({ text, type: 'submit' })
  }, [])

  const handleVoiceRepliesChange = useCallback((enabled: boolean) => {
    setVoiceRepliesLocal(enabled)
  }, [])

  // P1 Voice: The voice conversation loop.
  const voiceLoop = useOverlayVoiceLoop({
    onSubmit: handleSubmit,
    onVoiceRepliesChange: handleVoiceRepliesChange,
    busy: liveBusy,
    lastReply,
    replyId,
    initialVoiceReplies: voiceReplies
  })

  // Detect busy→idle transition as a signal that a reply landed.
  // In a real implementation, the main renderer would push the actual reply
  // text via the state payload. For P1, we bump replyId when busy settles.
  useEffect(() => {
    if (prevBusyRef.current && !liveBusy) {
      setReplyId(id => id + 1)
    }

    prevBusyRef.current = liveBusy
  }, [liveBusy])

  const dragRef = useRef<DragState | null>(null)
  // Last Alt+wheel anchor, consumed by the resize effect to zoom toward the
  // cursor; null means a non-wheel scale change (slider) → anchor bottom-center.
  const zoomAnchorRef = useRef<PetZoomAnchor | null>(null)
  const petRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const chatInputRef = useRef<HTMLTextAreaElement | null>(null)
  // Last mirrored reaction id — a bump means the main window fired a reaction.
  const lastReactionRef = useRef<number | null>(null)
  const ignoreRef = useRef(true)
  const composerOpenRef = useRef(false)
  const settingsOpenRef = useRef(false)
  const contextMenuRef = useRef(false)
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const setIgnore = (ignore: boolean) => {
    if (ignoreRef.current !== ignore) {
      ignoreRef.current = ignore
      window.hermesDesktop?.petOverlay?.setIgnoreMouse(ignore)
    }
  }

  // Derived opacity value (0-1).
  const opacityValue = AVATAR_OPACITY_VALUES[avatarOpacity]

  // The effective scale: size preset overrides the pet's gateway scale so the
  // avatar size menu is the single source of truth for display size.
  const effectiveScale = useMemo(() => {
    return AVATAR_SIZE_SCALES[avatarSize] ?? info.scale ?? DEFAULT_SCALE
  }, [avatarSize, info.scale])

  // Mirror pushed state into the shared atoms so PetSprite/PetBubble just work.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    const off = window.hermesDesktop?.petOverlay?.onState(payload => {
      setPetInfo(payload.info)
      $petActivity.set(payload.activity ?? {})
      setBusy(Boolean(payload.busy))
      setAwaitingResponse(Boolean(payload.awaiting))
      setUnread(Boolean(payload.unread))

      // Sync avatar display prefs from main renderer.
      if (payload.avatarSize) {setAvatarSizeLocal(payload.avatarSize)}

      if (payload.avatarOpacity) {setAvatarOpacityLocal(payload.avatarOpacity)}

      if (typeof payload.voiceReplies === 'boolean') {setVoiceRepliesLocal(payload.voiceReplies)}

      if (typeof payload.hidden === 'boolean') {setAvatarHiddenLocal(payload.hidden)}

      if (payload.avatarMode) {setAvatarModeLocal(payload.avatarMode)}

      if (payload.gatewayStatus) {setGatewayStatus(payload.gatewayStatus)}

      if (payload.activeSessionId !== undefined) {setActiveSessionId(payload.activeSessionId)}

      // P1: Sync avatar pack renderer state from main renderer.
      if (payload.avatarRendererType) {setAvatarRendererTypeLocal(payload.avatarRendererType)}

      if (payload.selectedAvatarPack !== undefined) {setSelectedAvatarPackLocal(payload.selectedAvatarPack)}

      if (payload.avatarPreviewState !== undefined) {setAvatarPreviewStateLocal(payload.avatarPreviewState)}

      if (payload.avatarPackList !== undefined) {setAvatarPackListLocal(payload.avatarPackList)}

      // P1.5: Push the last assistant text + msg-id into the overlay so the
      // voice loop can TTS it. The msgId nonce prevents re-reading the same
      // reply when the payload arrives from a non-busy atom subscription
      // (e.g. $activity, $petInfo) after the initial push.
      if (payload.lastAssistantText !== undefined) {
        setLastReply(payload.lastAssistantText)
      }

      if (payload.lastAssistantMsgId !== undefined) {
        if (
          payload.lastAssistantMsgId &&
          payload.lastAssistantMsgId !== lastSpokenAssistantMsgIdRef.current
        ) {
          lastSpokenAssistantMsgIdRef.current = payload.lastAssistantMsgId
          setReplyId(id => id + 1)
        }
      }

      // Play a reaction on a new id (ignore the first sync, which just primes it).
      const reaction = payload.reaction ?? null

      if (lastReactionRef.current === null) {
        lastReactionRef.current = reaction?.id ?? 0
      } else if (reaction && reaction.id > lastReactionRef.current) {
        lastReactionRef.current = reaction.id

        if (reaction.kind === 'vibe') {
          playVibeHearts()
        }
      }
    })

    // Tell the main renderer we're mounted so it pushes the current frame (the
    // subscribe-time pushes during open() can land before this view exists).
    window.hermesDesktop?.petOverlay?.control({ type: 'ready' })

    return off
  }, [])

  // Click-through: make only the *solid* sprite pixels (plus the bubble / mail
  // button / open composer) interactive — clicks on the transparent rectangle
  // around the art pass through to whatever's behind. With ignore+forward, the
  // renderer still receives mousemove so we can re-arm the moment the cursor
  // returns to a solid pixel.
  useEffect(() => {
    setIgnore(true)

    // True when the point sits on a solid sprite pixel or on the pet's other
    // interactive chrome (bubble, mail button, context menu, settings panel).
    // Over the canvas we sample the rendered alpha; elsewhere inside the pet
    // we trust DOM hit-testing. Anything else is transparent backdrop.
    const isInteractiveAt = (x: number, y: number): boolean => {
      const pet = petRef.current
      const target = document.elementFromPoint(x, y)

      if (!pet || !target) {
        return false
      }

      // Context menu and settings panel are always interactive.
      if (contextMenuRef.current || settingsOpenRef.current) {
        return true
      }

      if (!pet.contains(target)) {
        return false
      }

      if (!(target instanceof HTMLCanvasElement)) {
        return true
      }

      const rect = target.getBoundingClientRect()

      if (rect.width === 0 || rect.height === 0) {
        return true
      }

      const ctx = target.getContext('2d')

      if (!ctx) {
        return true
      }

      const px = Math.floor((x - rect.left) * (target.width / rect.width))
      const py = Math.floor((y - rect.top) * (target.height / rect.height))

      try {
        return ctx.getImageData(px, py, 1, 1).data[3] >= ALPHA_HIT_THRESHOLD
      } catch {
        // Tainted/zero-size read — fail open so the pet stays grabbable.
        return true
      }
    }

    const onMove = (ev: MouseEvent) => {
      if (dragRef.current || composerOpenRef.current || contextMenuRef.current || settingsOpenRef.current) {
        setIgnore(false)

        return
      }

      setIgnore(!isInteractiveAt(ev.clientX, ev.clientY))
    }

    window.addEventListener('mousemove', onMove)

    return () => {
      window.removeEventListener('mousemove', onMove)
      clearTimeout(clickTimerRef.current)
    }
  }, [])

  // The whole window must stay interactive while the composer or settings are
  // open (so inputs keep focus). The overlay is a non-activating panel — flip
  // it focusable while inputs need the keyboard, then back when they close.
  useEffect(() => {
    composerOpenRef.current = composerOpen
    const needsFocus = composerOpen || settingsOpen || Boolean(contextMenu)
    window.hermesDesktop?.petOverlay?.setFocusable(needsFocus)

    if (needsFocus) {
      setIgnore(false)
      // The OS window has to become key first, so focus the input on next frame.
      requestAnimationFrame(() => {
        if (composerOpen) {inputRef.current?.focus()}
        else if (settingsOpen) {chatInputRef.current?.focus()}
      })
    }
  }, [composerOpen, settingsOpen, contextMenu])

  useEffect(() => {
    settingsOpenRef.current = settingsOpen
  }, [settingsOpen])

  useEffect(() => {
    contextMenuRef.current = Boolean(contextMenu)
  }, [contextMenu])

  const onPetPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) {
      return
    }

    // Close context menu on any click outside it.
    if (contextMenu) {
      setContextMenu(null)
    }

    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    dragRef.current = {
      height: window.outerHeight,
      moved: false,
      offX: e.screenX - window.screenX,
      offY: e.screenY - window.screenY,
      startX: e.screenX,
      startY: e.screenY,
      width: window.outerWidth
    }
  }

  const onPetPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current

    if (!drag) {
      return
    }

    if (Math.hypot(e.screenX - drag.startX, e.screenY - drag.startY) > CLICK_SLOP_PX) {
      drag.moved = true
    }

    window.hermesDesktop?.petOverlay?.setBounds({
      height: drag.height,
      width: drag.width,
      x: e.screenX - drag.offX,
      y: e.screenY - drag.offY
    })
  }

  const onPetPointerUp = (e: React.PointerEvent) => {
    const drag = dragRef.current
    dragRef.current = null
    ;(e.target as Element).releasePointerCapture?.(e.pointerId)

    if (!drag) {
      return
    }

    if (drag.moved) {
      // A drag cancels any deferred single-click.
      clearTimeout(clickTimerRef.current)
      clickTimerRef.current = undefined

      // Remember the spot on the desktop so the pet reopens here next time.
      window.hermesDesktop?.petOverlay?.control({
        bounds: { height: drag.height, width: drag.width, x: e.screenX - drag.offX, y: e.screenY - drag.offY },
        type: 'bounds'
      })

      return
    }

    // Shift-click always pops the pet back in.
    if (e.shiftKey) {
      window.hermesDesktop?.petOverlay?.control({ type: 'pop-in' })

      return
    }

    // Double-click opens the chat; defer the single-click composer toggle.
    if (clickTimerRef.current) {
      clearTimeout(clickTimerRef.current)
      clickTimerRef.current = undefined
      // Double-click → open chat panel.
      window.hermesDesktop?.petOverlay?.control({ type: 'open-chat' })
      setComposerOpen(open => !open)

      return
    }

    clickTimerRef.current = setTimeout(() => {
      clickTimerRef.current = undefined
      setComposerOpen(open => !open)
    }, DOUBLE_CLICK_MS)
  }

  // Right-click → context menu.
  const onPetContextMenu = (e: React.MouseEvent) => {
    e.preventDefault()
    setContextMenu({ x: e.clientX, y: e.clientY })
  }

  const send = () => {
    const text = draft.trim()

    if (text) {
      // Add message to local display.
      setMessages(prev => [...prev, { id: ++msgIdCounter, text, role: 'user', ts: Date.now() }])
      // Send to main renderer → gateway.
      window.hermesDesktop?.petOverlay?.control({ text, type: 'submit' })
    }

    setDraft('')
  }

  const sendChatMessage = () => {
    const text = draft.trim()

    if (text) {
      setMessages(prev => [...prev, { id: ++msgIdCounter, text, role: 'user', ts: Date.now() }])
      window.hermesDesktop?.petOverlay?.control({ text, type: 'submit' })
    }

    setDraft('')

    // Keep focus in the chat input.
    requestAnimationFrame(() => chatInputRef.current?.focus())
  }

  const openApp = () => {
    setUnread(false)
    window.hermesDesktop?.petOverlay?.control({ type: 'open-app' })
  }

  // ── Context menu actions ──────────────────────────────────────────────────

  const sendControl = (type: 'hide' | 'quit' | 'open-chat' | 'open-settings') => {
    window.hermesDesktop?.petOverlay?.control({ type })
    setContextMenu(null)

    if (type === 'hide') {setAvatarHiddenLocal(true)}

    if (type === 'open-chat') {setComposerOpen(true)}

    if (type === 'open-settings') {setSettingsOpen(true)}
  }

  const handleSizeChange = (preset: AvatarSizePreset) => {
    // P0.3 FIX: Do NOT set $petInfo.scale or send a 'scale' control message.
    // The old path set the pet's gateway scale to match the preset, which then
    // echoed back via the main renderer's pushNow — but payload.avatarSize was
    // still the OLD value, so the overlay reverted to the old size on the next
    // state push. Instead, send a 'set-size' control message so the main
    // renderer updates $avatarSize; its subscription fires pushNow with the
    // new size, and effectiveScale (derived from avatarSize) drives both the
    // sprite and the overlay window resize — atomically and persistently.
    setAvatarSizeLocal(preset)
    window.hermesDesktop?.petOverlay?.control({ size: preset, type: 'set-size' })
    setContextMenu(null)
  }

  const handleOpacityChange = (preset: AvatarOpacityPreset) => {
    setAvatarOpacityLocal(preset)
    setContextMenu(null)
  }

  const handleVoiceToggle = () => {
    voiceLoop.toggleVoiceReplies()
    setContextMenu(null)
  }

  // P1 Voice: Listen / Stop listening from context menu.
  const handleListenToggle = () => {
    if (voiceLoop.state.status === 'listening') {
      void voiceLoop.stopListening()
    } else if (voiceLoop.state.status === 'speaking') {
      voiceLoop.stopSpeaking()
    } else if (voiceLoop.state.status === 'idle' || voiceLoop.state.status === 'thinking') {
      void voiceLoop.startListening()
    }

    setContextMenu(null)
  }

  // P0.1: Dock → closes overlay, returns pet to in-window mode.
  const handleDock = () => {
    window.hermesDesktop?.petOverlay?.control({ type: 'dock' })
    setContextMenu(null)
  }

  // Alt+wheel over the popped-out pet resizes it.
  const onScale = useCallback((next: number, anchor: PetZoomAnchor) => {
    zoomAnchorRef.current = anchor
    setPetInfo({ ...$petInfo.get(), scale: next })
    window.hermesDesktop?.petOverlay?.control({ scale: next, type: 'scale' })
  }, [])

  usePetZoomGesture(petRef, onScale, Boolean(info.enabled && (info.spritesheetBase64 || (avatarRendererType === 'avatar-pack' && selectedAvatarPack))))

  // Grow/shrink the OS overlay window to fit the pet at its current scale.
  // Uses effectiveScale so size-preset changes also resize the window.
  useEffect(() => {
    // In avatar-pack mode, spritesheetBase64 is not required. The window
    // resize should fire as long as we have content to show.
    const hasSprite = info.enabled && info.spritesheetBase64
    const hasPack = avatarRendererType === 'avatar-pack' && Boolean(selectedAvatarPack)

    if (!hasSprite && !hasPack) {
      return
    }

    const { width, height } = overlayWindowSize(
      info.frameW ?? DEFAULT_FRAME_W,
      info.frameH ?? DEFAULT_FRAME_H,
      effectiveScale
    )

    const curW = window.outerWidth
    const curH = window.outerHeight

    if (width === curW && height === curH) {
      zoomAnchorRef.current = null

      return
    }

    const anchor = zoomAnchorRef.current
    zoomAnchorRef.current = null

    const ratio = anchor?.ratio ?? 1
    const ax = anchor?.clientX ?? curW / 2
    const ay = anchor?.clientY ?? curH - PET_PADDING_BOTTOM

    const bounds = {
      height,
      width,
      x: Math.round(window.screenX + ax - (ax - curW / 2) * ratio - width / 2),
      y: Math.round(window.screenY + ay - (ay - (curH - PET_PADDING_BOTTOM)) * ratio - (height - PET_PADDING_BOTTOM))
    }

    window.hermesDesktop?.petOverlay?.setBounds(bounds)
    window.hermesDesktop?.petOverlay?.control({ bounds, type: 'bounds' })
  }, [info.enabled, info.spritesheetBase64, effectiveScale, info.frameW, info.frameH, avatarRendererType, selectedAvatarPack])

  // Hidden state: return null but keep the window alive for re-show.
  if (avatarHidden) {
    return null
  }

  // Render nothing only when there's truly nothing to show. In avatar-pack
  // mode, spritesheetBase64 is irrelevant — the pack's own assets drive the
  // render. The old guard (requireBoth) killed the overlay in avatar-pack mode.
  const hasContent = Boolean(info.enabled && (info.spritesheetBase64 || (avatarRendererType === 'avatar-pack' && selectedAvatarPack)))

  if (!hasContent) {
    return null
  }

  // ── Context menu rendering ────────────────────────────────────────────────

  const renderContextMenu = () => {
    if (!contextMenu) {return null}

    const sizePresets = Object.keys(AVATAR_SIZE_SCALES) as AvatarSizePreset[]
    const opacityPresets = Object.keys(AVATAR_OPACITY_VALUES) as AvatarOpacityPreset[]
    // Clamp menu position so it doesn't overflow the window.
    const menuW = 220
    const menuH = 420
    const mx = Math.min(contextMenu.x, window.innerWidth - menuW - 8)
    const my = Math.min(contextMenu.y, window.innerHeight - menuH - 8)

    return (
      <div
        onClick={e => e.stopPropagation()}
        onPointerDown={e => e.stopPropagation()}
        onPointerUp={e => e.stopPropagation()}
        style={{
          background: 'var(--ui-bg-elevated)',
          borderColor: 'var(--ui-stroke-secondary)',
          borderRadius: 6,
          boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          color: 'var(--foreground)',
          fontSize: 12,
          left: mx,
          minWidth: menuW,
          padding: '4px 0',
          position: 'fixed',
          top: my,
          zIndex: 1000
        }}
      >
        {/* Chat… */}
        <MenuItem label="Chat…" onClick={() => sendControl('open-chat')} />

        {/* P1 Voice: Listen / Stop */}
        {(() => {
          const vs = voiceLoop.state.status

          if (vs === 'listening') {
            return (
              <>
                <MenuItem
                  label="● Listening… (click to stop)"
                  onClick={handleListenToggle}
                />
                <MenuItem
                  label="Stop speaking"
                  onClick={() => {
                    voiceLoop.stopSpeaking()
                    setContextMenu(null)
                  }}
                />
              </>
            )
          }

          if (vs === 'speaking') {
            return (
              <MenuItem
                label="■ Speaking… (click to stop)"
                onClick={handleListenToggle}
              />
            )
          }

          if (vs === 'transcribing') {
            return <MenuItem label="⟳ Transcribing…" onClick={() => setContextMenu(null)} />
          }

          if (vs === 'thinking') {
            return <MenuItem label="⟳ Thinking…" onClick={() => setContextMenu(null)} />
          }

          // idle
          return (
            <MenuItem
              label="🎤 Listen"
              onClick={handleListenToggle}
            />
          )
        })()}

        {/* Voice replies toggle */}
        <MenuItem
          label={voiceLoop.state.voiceReplies ? 'Voice replies: ON' : 'Voice replies: OFF'}
          onClick={handleVoiceToggle}
        />

        <MenuDivider />

        {/* Size submenu */}
        <MenuLabel>Size</MenuLabel>
        {sizePresets.map(preset => (
          <MenuItem
            key={preset}
            label={`${AVATAR_SIZE_LABELS[preset]}${avatarSize === preset ? '  ✓' : ''}`}
            onClick={() => handleSizeChange(preset)}
          />
        ))}

        <MenuDivider />

        {/* Opacity submenu */}
        <MenuLabel>Opacity</MenuLabel>
        {opacityPresets.map(preset => (
          <MenuItem
            key={preset}
            label={`${AVATAR_OPACITY_LABELS[preset]}${avatarOpacity === preset ? '  ✓' : ''}`}
            onClick={() => handleOpacityChange(preset)}
          />
        ))}

        <MenuDivider />

        {/* P0.1: Dock toggle — current mode shown */}
        <MenuItem
          label={AVATAR_MODE_LABELS[avatarMode] === 'Desktop overlay' ? '✓ Desktop overlay' : 'Desktop overlay'}
          onClick={() => {
            // Already on desktop — no-op
            setContextMenu(null)
          }}
        />
        <MenuItem
          label={avatarMode === 'docked' ? '✓ Docked in Hermes' : 'Docked in Hermes'}
          onClick={handleDock}
        />

        <MenuDivider />

        <MenuItem label="Hide avatar" onClick={() => sendControl('hide')} />
        <MenuItem label="Settings…" onClick={() => sendControl('open-settings')} />
        <MenuDivider />
        <MenuItem label="Quit" onClick={() => sendControl('quit')} />
      </div>
    )
  }

  // ── Settings panel rendering ──────────────────────────────────────────────

  const renderSettings = () => {
    if (!settingsOpen) {return null}

    const sizePresets = Object.keys(AVATAR_SIZE_SCALES) as AvatarSizePreset[]
    const opacityPresets = Object.keys(AVATAR_OPACITY_VALUES) as AvatarOpacityPreset[]

    return (
      <div
        onClick={e => e.stopPropagation()}
        onPointerDown={e => e.stopPropagation()}
        onPointerUp={e => e.stopPropagation()}
        style={{
          background: 'var(--ui-bg-elevated)',
          borderColor: 'var(--ui-stroke-secondary)',
          borderRadius: 8,
          boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          color: 'var(--foreground)',
          display: 'flex',
          flexDirection: 'column',
          fontSize: 12,
          left: '50%',
          maxHeight: '80vh',
          overflow: 'auto',
          padding: '12px 16px',
          position: 'fixed',
          top: '50%',
          transform: 'translate(-50%, -50%)',
          width: 280,
          zIndex: 1000
        }}
      >
        <div style={{ alignItems: 'center', display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Settings</span>
          <button
            onClick={() => setSettingsOpen(false)}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--ui-text-secondary)',
              cursor: 'pointer',
              fontSize: 14
            }}
            type="button"
          >
            ✕
          </button>
        </div>

        {/* Size */}
        <SettingsRow label="Size">
          <select
            onChange={e => handleSizeChange(e.target.value as AvatarSizePreset)}
            style={selectStyle}
            value={avatarSize}
          >
            {sizePresets.map(p => (
              <option key={p} value={p}>{AVATAR_SIZE_LABELS[p]}</option>
            ))}
          </select>
        </SettingsRow>

        {/* Opacity */}
        <SettingsRow label="Opacity">
          <select
            onChange={e => handleOpacityChange(e.target.value as AvatarOpacityPreset)}
            style={selectStyle}
            value={avatarOpacity}
          >
            {opacityPresets.map(p => (
              <option key={p} value={p}>{AVATAR_OPACITY_LABELS[p]}</option>
            ))}
          </select>
        </SettingsRow>

        {/* Voice replies */}
        <SettingsRow label="Voice replies">
          <span style={{ color: voiceLoop.state.voiceReplies ? 'var(--ui-accent)' : 'var(--ui-text-secondary)' }}>
            {voiceLoop.state.voiceReplies ? 'ON' : 'OFF'}
          </span>
        </SettingsRow>

        <MenuDivider />

        {/* P1 Voice section */}
        <div style={{ color: 'var(--ui-text-quaternary)', fontSize: 10, marginBottom: 4, textTransform: 'uppercase' }}>
          Voice
        </div>
        <SettingsRow label="Microphone">
          <span style={{
            color: voiceLoop.state.status === 'listening' ? 'var(--ui-accent)' : 'var(--ui-text-secondary)',
            fontSize: 11
          }}>
            {voiceLoop.state.sttAvailable === false
              ? 'Not configured'
              : voiceLoop.state.status === 'listening'
                ? `● Recording ${Math.floor(voiceLoop.state.elapsedSeconds)}s`
                : voiceLoop.state.status === 'transcribing'
                  ? '⟳ Transcribing…'
                  : voiceLoop.state.sttAvailable === null
                    ? 'Ready'
                    : 'Available'}
          </span>
        </SettingsRow>
        <SettingsRow label="TTS">
          <span style={{
            color: voiceLoop.state.status === 'speaking' ? 'var(--ui-accent)' : 'var(--ui-text-secondary)',
            fontSize: 11
          }}>
            {voiceLoop.state.ttsAvailable === false
              ? 'Not configured'
              : voiceLoop.state.status === 'speaking'
                ? '■ Speaking…'
                : voiceLoop.state.ttsAvailable === null
                  ? 'Ready'
                  : 'Available'}
          </span>
        </SettingsRow>
        {voiceLoop.state.lastTranscript && (
          <SettingsRow label="Last input">
            <span style={{
              color: 'var(--ui-text-tertiary)',
              fontSize: 10,
              maxWidth: 140,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap'
            }}>
              "{voiceLoop.state.lastTranscript.slice(0, 40)}"
            </span>
          </SettingsRow>
        )}
        {voiceLoop.state.error && (
          <div style={{
            color: '#f87171',
            fontSize: 10,
            padding: '4px 0',
            lineHeight: 1.3
          }}>
            {voiceLoop.state.error}
          </div>
        )}
        <div style={{ display: 'flex', gap: 4, justifyContent: 'center', padding: '4px 0' }}>
          {voiceLoop.state.status === 'listening' ? (
            <button
              onClick={() => void voiceLoop.stopListening()}
              style={{ ...selectStyle, cursor: 'pointer', padding: '3px 8px' }}
              type="button"
            >
              ■ Stop & Send
            </button>
          ) : voiceLoop.state.status === 'speaking' ? (
            <button
              onClick={() => voiceLoop.stopSpeaking()}
              style={{ ...selectStyle, cursor: 'pointer', padding: '3px 8px' }}
              type="button"
            >
              ■ Stop Speaking
            </button>
          ) : (
            <button
              onClick={() => void voiceLoop.startListening()}
              style={{ ...selectStyle, cursor: 'pointer', padding: '3px 8px' }}
              type="button"
            >
              🎤 Listen
            </button>
          )}
        </div>

        <MenuDivider />

        {/* P0.1: Avatar mode */}
        <SettingsRow label="Avatar mode">
          <span style={{ color: 'var(--ui-text-secondary)' }}>
            {AVATAR_MODE_LABELS[avatarMode]}
          </span>
        </SettingsRow>

        <MenuDivider />

        {/* Gateway status */}
        <SettingsRow label="Gateway">
          <span style={{ color: gatewayStatus === 'open' ? 'var(--ui-accent)' : 'var(--ui-text-secondary)' }}>
            {gatewayStatus}
          </span>
        </SettingsRow>

        {/* Session ID */}
        <SettingsRow label="Session">
          <span style={{ color: 'var(--ui-text-secondary)', fontFamily: 'monospace', fontSize: 11 }}>
            {activeSessionId ? activeSessionId.slice(0, 12) + '…' : 'none'}
          </span>
        </SettingsRow>

        <MenuDivider />

        {/* P1: Avatar Pack renderer controls */}
        <div style={{ color: 'var(--ui-text-quaternary)', fontSize: 10, marginBottom: 4, textTransform: 'uppercase' }}>
          Character
        </div>
        <SettingsRow label="Renderer">
          <select
            onChange={e => {
              const t = e.target.value as AvatarRendererType
              setAvatarRendererTypeLocal(t)
              window.hermesDesktop?.petOverlay?.control({ rendererType: t, type: 'set-renderer-type' })
            }}
            style={selectStyle}
            value={avatarRendererType}
          >
            <option value="petdex">Petdex sprite</option>
            <option value="avatar-pack">Avatar Pack</option>
          </select>
        </SettingsRow>

        {avatarRendererType === 'avatar-pack' && (
          <>
            <SettingsRow label="Pack">
              <select
                onChange={e => {
                  const packId = e.target.value || null
                  window.hermesDesktop?.petOverlay?.control({ packId, type: 'set-pack' })
                }}
                style={selectStyle}
                value={selectedAvatarPack?.id ?? ''}
              >
                <option value="">— Select —</option>
                {(avatarPackList?.packs ?? []).map(p => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.stateCount} states)
                  </option>
                ))}
              </select>
            </SettingsRow>

            <SettingsRow label="Folder">
              <button
                onClick={() => {
                  window.hermesDesktop?.petOverlay?.control({ type: 'open-packs-folder' })
                }}
                style={{ ...selectStyle, cursor: 'pointer' }}
                type="button"
              >
                Open…
              </button>
            </SettingsRow>

            <SettingsRow label="Reload">
              <button
                onClick={() => {
                  window.hermesDesktop?.petOverlay?.control({ type: 'reload-packs' })
                }}
                style={{ ...selectStyle, cursor: 'pointer' }}
                type="button"
              >
                Reload
              </button>
            </SettingsRow>

            {/* State preview buttons */}
            <div style={{ alignItems: 'center', display: 'flex', gap: 4, justifyContent: 'space-between', padding: '4px 0' }}>
              <span style={{ color: 'var(--ui-text-secondary)' }}>Preview</span>
              <div style={{ display: 'flex', gap: 3 }}>
                {ALL_AVATAR_STATES.map(s => (
                  <button
                    key={s}
                    onClick={() => {
                      const next = avatarPreviewState === s ? null : s
                      setAvatarPreviewStateLocal(next)
                      window.hermesDesktop?.petOverlay?.control({ state: next, type: 'set-preview-state' })
                    }}
                    style={{
                      ...selectStyle,
                      background: avatarPreviewState === s ? 'var(--ui-accent)' : 'transparent',
                      color: avatarPreviewState === s ? '#fff' : 'var(--foreground)',
                      cursor: 'pointer',
                      padding: '2px 6px'
                    }}
                    type="button"
                  >
                    {AVATAR_STATE_LABELS[s]}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        <MenuDivider />

        <div style={{ color: 'var(--ui-text-quaternary)', fontSize: 10, marginTop: 4 }}>
          Hermes Desktop Avatar · P1 Voice
        </div>
      </div>
    )
  }

  return (
    <div
      onClick={() => {
        if (contextMenu) {setContextMenu(null)}
      }}
      onPointerDown={e => {
        // Click on the transparent backdrop dismisses composer/settings.
        if (composerOpen && e.target === e.currentTarget) {
          setComposerOpen(false)
        }

        if (settingsOpen && e.target === e.currentTarget) {
          setSettingsOpen(false)
        }
      }}
      style={{
        alignItems: 'center',
        background: 'transparent',
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
        justifyContent: 'flex-end',
        opacity: opacityValue,
        paddingBottom: PET_PADDING_BOTTOM,
        userSelect: 'none',
        width: '100vw'
      }}
    >
      {/* Chat panel (multi-message) */}
      {composerOpen && (
        <div
          style={{
            background: 'var(--ui-bg-elevated)',
            borderColor: 'var(--ui-stroke-secondary)',
            borderRadius: 8,
            boxShadow: '0 6px 24px rgba(0,0,0,0.36)',
            color: 'var(--foreground)',
            display: 'flex',
            flexDirection: 'column',
            marginBottom: 8,
            maxHeight: 300,
            width: 280
          }}
        >
          {/* Messages */}
          {messages.length > 0 && (
            <div
              style={{
                flex: 1,
                overflowY: 'auto',
                padding: '8px 10px'
              }}
            >
              {messages.map(msg => (
                <div
                  key={msg.id}
                  style={{
                    marginBottom: 6,
                    textAlign: msg.role === 'user' ? 'right' : 'left'
                  }}
                >
                  <span
                    style={{
                      background: msg.role === 'user' ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
                      borderRadius: 8,
                      color: msg.role === 'user' ? '#fff' : 'var(--foreground)',
                      display: 'inline-block',
                      fontSize: 12,
                      maxWidth: '85%',
                      padding: '4px 8px',
                      wordBreak: 'break-word'
                    }}
                  >
                    {msg.text}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Input */}
          <div style={{ display: 'flex', padding: 6 }}>
            <textarea
              onChange={e => setDraft(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  sendChatMessage()
                } else if (e.key === 'Escape') {
                  setComposerOpen(false)
                }
              }}
              placeholder="Message…"
              ref={chatInputRef}
              rows={1}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--foreground)',
                flex: 1,
                fontSize: 12,
                outline: 'none',
                resize: 'none'
              }}
              value={draft}
            />
          </div>
        </div>
      )}

      {/* P1 Voice: Recording / status indicator */}
      {voiceLoop.state.status !== 'idle' && (
        <div
          style={{
            alignItems: 'center',
            background: voiceLoop.state.status === 'listening'
              ? 'rgba(239, 68, 68, 0.15)'
              : 'var(--ui-bg-elevated)',
            borderColor: voiceLoop.state.status === 'listening'
              ? 'rgba(239, 68, 68, 0.4)'
              : 'var(--ui-stroke-secondary)',
            borderRadius: 8,
            border: '1px solid',
            boxShadow: '0 4px 14px rgba(0,0,0,0.22)',
            color: voiceLoop.state.status === 'listening' ? '#f87171' : 'var(--foreground)',
            display: 'flex',
            fontSize: 11,
            gap: 6,
            marginBottom: 6,
            padding: '4px 10px',
            whiteSpace: 'nowrap'
          }}
        >
          {voiceLoop.state.status === 'listening' && (
            <>
              <span
                style={{
                  animation: 'pulse 1.5s ease-in-out infinite',
                  background: '#ef4444',
                  borderRadius: '50%',
                  display: 'inline-block',
                  height: 8,
                  width: 8
                }}
              />
              <span>Listening… {Math.floor(voiceLoop.state.elapsedSeconds)}s</span>
              {/* Level bars */}
              <div style={{ display: 'flex', gap: 1, height: 10 }}>
                {[0.5, 0.78, 1, 0.78, 0.5].map((w, i) => (
                  <span
                    key={i}
                    style={{
                      background: '#f87171',
                      borderRadius: 1,
                      display: 'inline-block',
                      height: `${Math.max(15, Math.min(100, voiceLoop.state.level * w * 100))}%`,
                      width: 2,
                      transition: 'height 0.08s'
                    }}
                  />
                ))}
              </div>
            </>
          )}
          {voiceLoop.state.status === 'transcribing' && <span>⟳ Transcribing…</span>}
          {voiceLoop.state.status === 'thinking' && <span>⟳ Thinking…</span>}
          {voiceLoop.state.status === 'speaking' && <span>■ Speaking…</span>}
        </div>
      )}

      <div
        onContextMenu={onPetContextMenu}
        onPointerDown={onPetPointerDown}
        onPointerMove={onPetPointerMove}
        onPointerUp={onPetPointerUp}
        ref={petRef}
        style={{
          alignItems: 'center',
          cursor: 'grab',
          display: 'flex',
          flexDirection: 'column',
          position: 'relative',
          touchAction: 'none'
        }}
      >
        <div style={{ marginBottom: 4 }}>
          <PetBubble />
        </div>
        <div style={{ lineHeight: 0, position: 'relative' }}>
          {/* P1: Render AvatarPackRenderer or PetSprite depending on the active renderer type. */}
          {avatarRendererType === 'avatar-pack' && selectedAvatarPack ? (
            <AvatarPackRenderer
              opacity={opacityValue}
              pack={selectedAvatarPack}
              scale={effectiveScale}
              state={
                avatarPreviewState ?? activityToAvatarState({
                  busy: liveBusy,
                  awaitingInput: Boolean(liveActivity.awaitingInput),
                  toolRunning: Boolean(liveActivity.toolRunning),
                  reasoning: Boolean(liveActivity.reasoning),
                  error: Boolean(liveActivity.error),
                  justCompleted: Boolean(liveActivity.justCompleted),
                  celebrate: Boolean(liveActivity.celebrate)
                })
              }
            />
          ) : (
            <PetSprite info={{ ...info, scale: effectiveScale }} />
          )}

          {/* Hearts on the popped-out pet. */}
          <PetHeartField
            petH={(info.frameH ?? DEFAULT_FRAME_H) * effectiveScale}
            petW={(info.frameW ?? DEFAULT_FRAME_W) * effectiveScale}
          />

          {/* Mail icon: only when a finish landed while you were away. */}
          {unread && (
            <button
              aria-label="Open in Hermes"
              onClick={openApp}
              onPointerDown={e => e.stopPropagation()}
              onPointerUp={e => e.stopPropagation()}
              style={{
                alignItems: 'center',
                background: 'var(--ui-bg-elevated)',
                border: '1px solid var(--ui-stroke-secondary)',
                borderRadius: 999,
                boxShadow: '0 4px 14px rgba(0,0,0,0.22)',
                color: 'var(--foreground)',
                cursor: 'pointer',
                display: 'inline-flex',
                height: 24,
                justifyContent: 'center',
                padding: 0,
                position: 'absolute',
                right: 0,
                top: 0,
                width: 24
              }}
              title="Open in Hermes"
              type="button"
            >
              <Mail style={{ height: 13, width: 13 }} />
            </button>
          )}
        </div>
      </div>

      {/* Context menu overlay */}
      {renderContextMenu()}

      {/* Settings panel overlay */}
      {renderSettings()}
    </div>
  )
}

// ── Small presentational helpers ─────────────────────────────────────────────

function MenuItem({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <div
      onClick={onClick}
      onMouseEnter={e => {
        ;(e.currentTarget as HTMLDivElement).style.background = 'var(--ui-stroke-secondary)'
      }}
      onMouseLeave={e => {
        ;(e.currentTarget as HTMLDivElement).style.background = 'transparent'
      }}
      onPointerDown={e => e.stopPropagation()}
      onPointerUp={e => e.stopPropagation()}
      style={{
        cursor: 'pointer',
        padding: '5px 16px',
        whiteSpace: 'nowrap'
      }}
    >
      {label}
    </div>
  )
}

function MenuDivider() {
  return (
    <div
      style={{
        background: 'var(--ui-stroke-secondary)',
        height: 1,
        margin: '4px 0'
      }}
    />
  )
}

function MenuLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        color: 'var(--ui-text-quaternary)',
        fontSize: 10,
        padding: '2px 16px',
        textTransform: 'uppercase'
      }}
    >
      {children}
    </div>
  )
}

function SettingsRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        alignItems: 'center',
        display: 'flex',
        justifyContent: 'space-between',
        padding: '4px 0'
      }}
    >
      <span style={{ color: 'var(--ui-text-secondary)' }}>{label}</span>
      {children}
    </div>
  )
}

const selectStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: 4,
  color: 'var(--foreground)',
  fontSize: 11,
  padding: '2px 4px'
}

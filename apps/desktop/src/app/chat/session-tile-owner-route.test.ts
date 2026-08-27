import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const source = readFileSync(resolve(process.cwd(), 'src/app/chat/session-tile.tsx'), 'utf8')
const chatViewSource = readFileSync(resolve(process.cwd(), 'src/app/chat/index.tsx'), 'utf8')
const composerSource = readFileSync(resolve(process.cwd(), 'src/app/chat/composer/index.tsx'), 'utf8')

const assistantMessageSource = readFileSync(
  resolve(process.cwd(), 'src/components/assistant-ui/thread/assistant-message.tsx'),
  'utf8'
)

describe('SessionTilePane owner-scoped listing', () => {
  it('resolves a newly active tile on its persisted owner route', () => {
    expect(source).toContain('void resolveStoredSession(storedSessionId, ownerRoute)')
    expect(source).not.toMatch(/void resolveStoredSession\(storedSessionId\)\s*\n/)
  })

  it('pins tile transcription to that exact route for direct and relay STT', () => {
    expect(source).toContain('transcribeAudioClientDirect(audio, ownerRoute)')
    expect(source).toContain('transcribeAudio(await blobToDataUrl(audio), audio.type, ownerRoute)')
  })

  it('carries the exact owner through ChatView and ChatBar into the voice engine', () => {
    expect(chatViewSource).toContain('voiceOwnerRoute={sessionOwnerRoute}')
    expect(composerSource).toContain('ownerRoute: voiceOwnerRoute')
  })

  it('binds transcript read-aloud controls to the tile owner', () => {
    expect(source).toContain("kind: 'tile',\n    ownerRoute,")
    expect(assistantMessageSource).toContain('ownerRoute: view.ownerRoute')
  })

  it('passes that exact route through to ChatView', () => {
    expect(source).toContain('sessionOwnerRoute={ownerRoute}')
  })
})

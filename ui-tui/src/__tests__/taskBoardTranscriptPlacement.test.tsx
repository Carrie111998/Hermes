import { PassThrough } from 'stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { afterEach, beforeEach, expect, it } from 'vitest'

import { GatewayProvider } from '../app/gatewayContext.js'
import type { AppLayoutProps } from '../app/interfaces.js'
import { patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import type { GatewayClient } from '../gatewayClient.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { stripAnsi } from '../lib/text.js'
import { openWidget } from '../sdk/host.js'
import { defineWidgetApp } from '../sdk/registry.js'
import { DEFAULT_THEME } from '../theme.js'

const gatewayStub = {
  gw: {
    request: () => new Promise<never>(() => {}),
    send: () => {}
  } as unknown as GatewayClient,
  rpc: (() => new Promise<never>(() => {})) as never
}

const layoutProps: AppLayoutProps = {
  actions: {
    activateLiveSession: () => {},
    answerApproval: () => {},
    answerClarify: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    closeLiveSession: () => Promise.resolve(null),
    newLiveSession: () => {},
    newPromptSession: () => {},
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {}
  },
  composer: {
    cols: 120,
    compIdx: 0,
    completions: [],
    empty: false,
    handleTextPaste: () => null,
    input: 'COMPOSER_ANCHOR',
    inputBuf: [],
    pagerPageSize: 10,
    queueEditIdx: null,
    queuedDisplay: [],
    submit: () => {},
    updateInput: () => {},
    voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
  },
  mouseTracking: 'off',
  progress: { showProgressArea: false },
  status: {
    cwdLabel: '~/repo',
    goodVibesTick: 0,
    lastTurnEndedAt: null,
    sessionStartedAt: 1_800_000_000_000,
    showStickyPrompt: false,
    statusColor: DEFAULT_THEME.color.ok,
    stickyPrompt: '',
    turnStartedAt: null,
    voiceLabel: ''
  },
  transcript: {
    historyItems: [],
    scrollRef: { current: null },
    virtualHistory: {
      bottomSpacer: 0,
      end: 0,
      measureRef: () => () => {},
      offsets: [],
      start: 0,
      topSpacer: 0
    },
    virtualRows: []
  }
}

beforeEach(() => {
  resetOverlayState()
  resetUiState()
})

afterEach(() => {
  resetOverlayState()
  resetUiState()
})

it('reserves transcript-bottom widgets above the composer', async () => {
  const app = defineWidgetApp({
    help: 'transcript placement regression',
    id: 'transcript-placement-test',
    mode: 'ambient',
    zone: 'transcript-bottom',
    init: () => ({}),
    reduce: state => state,
    render: () => <Text>TRANSCRIPT_BOARD_ANCHOR</Text>
  })

  openWidget(app, {})
  patchUiState({ sessionTitle: 'test', sid: 'sid-1', status: 'ready' })
  patchOverlayState({})

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 120, isTTY: false, rows: 20 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    <GatewayProvider value={gatewayStub}>
      <AppLayout {...layoutProps} />
    </GatewayProvider>,
    {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    }
  )

  try {
    await new Promise(resolve => setTimeout(resolve, 20))
    const screen = stripAnsi(output)
    const boardIndex = screen.indexOf('TRANSCRIPT_BOARD_ANCHOR')
    const composerIndex = screen.indexOf('COMPOSER_ANCHOR')

    expect(boardIndex).toBeGreaterThanOrEqual(0)
    expect(composerIndex).toBeGreaterThan(boardIndex)
  } finally {
    instance.unmount()
    instance.cleanup()
  }
})

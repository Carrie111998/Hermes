import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import { turnController } from '../app/turnController.js'
import { rememberToolIcon, resetTurnState } from '../app/turnStore.js'
import { ToolTrail } from '../components/thinking.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'
import type { ActiveTool, SubagentProgress } from '../types.js'

const flushEffects = async () => {
  for (let i = 0; i < 10; i++) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

const mount = async (props: Record<string, unknown>) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: false, rows: 30 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(<ToolTrail t={DEFAULT_THEME} {...(props as any)} />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  await flushEffects()

  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
}

const activeTool = (over: Partial<ActiveTool> = {}): ActiveTool => ({
  context: 'ls -la',
  id: 'tool-1',
  name: 'terminal',
  startedAt: Date.now(),
  ...over
})

describe('tool rows render the canonical tool icon, never a generic bullet', () => {
  beforeEach(() => {
    resetTurnState()
    turnController.fullReset()
  })

  it('draws the gateway-sent icon on the ACTIVE row', async () => {
    const out = await mount({
      busy: true,
      sections: { tools: 'expanded' },
      tools: [activeTool({ icon: '💻' })]
    })

    expect(out).toContain('💻')
    expect(out).toContain('Terminal')
    expect(out).not.toContain('●')
  })

  it('draws the same icon on the COMPLETED row after the tool finishes', async () => {
    // What tool.start filed under the trail label is what the completed row —
    // by then only a formatted string — resolves back out.
    rememberToolIcon('Terminal', '💻')

    const out = await mount({
      sections: { tools: 'expanded' },
      trail: ['Terminal("ls -la") (0.4s) ✓']
    })

    expect(out).toContain('💻')
    expect(out).toContain('Terminal')
    expect(out).not.toContain('●')
  })

  it('keeps distinct icons for distinct tools in the same trail', async () => {
    rememberToolIcon('Terminal', '💻')
    rememberToolIcon('Read File', '📖')

    const out = await mount({
      sections: { tools: 'expanded' },
      trail: ['Terminal("ls") ✓', 'Read File("app.ts") ✓']
    })

    expect(out).toContain('💻')
    expect(out).toContain('📖')
    expect(out).not.toContain('●')
  })

  it('falls back to ⚡ when no icon ever arrived for that tool', async () => {
    const out = await mount({
      sections: { tools: 'expanded' },
      trail: ['Mystery Tool("x") ✓']
    })

    expect(out).toContain('⚡')
    expect(out).not.toContain('●')
  })

  it('falls back to ⚡ on an ACTIVE row from a gateway that sent no icon', async () => {
    const out = await mount({
      busy: true,
      sections: { tools: 'expanded' },
      tools: [activeTool({ icon: undefined })]
    })

    expect(out).toContain('⚡')
    expect(out).not.toContain('●')
  })

  it('draws per-row icons inside the subagent tool list', async () => {
    const subagent: SubagentProgress = {
      depth: 0,
      goal: 'inspect the repo',
      id: 'sub-1',
      index: 0,
      notes: [],
      parentId: null,
      status: 'running',
      taskCount: 1,
      thinking: [],
      toolCount: 2,
      toolIcons: ['💻', '📖'],
      tools: ['Terminal("ls")', 'Read File("app.ts")']
    }

    const out = await mount({
      busy: true,
      sections: { subagents: 'expanded', tools: 'expanded' },
      subagents: [subagent]
    })

    expect(out).toContain('💻')
    expect(out).toContain('📖')
    expect(out).not.toContain('●')
  })

  it('falls back to ⚡ for a subagent frame that predates the icon contract', async () => {
    const subagent: SubagentProgress = {
      depth: 0,
      goal: 'inspect the repo',
      id: 'sub-1',
      index: 0,
      notes: [],
      parentId: null,
      status: 'running',
      taskCount: 1,
      thinking: [],
      toolCount: 1,
      tools: ['Terminal("ls")']
    }

    const out = await mount({
      busy: true,
      sections: { subagents: 'expanded', tools: 'expanded' },
      subagents: [subagent]
    })

    expect(out).toContain('⚡')
    expect(out).not.toContain('●')
  })
})

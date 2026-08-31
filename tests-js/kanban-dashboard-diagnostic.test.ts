/**
 * Tests for Kanban Dashboard DiagnosticCard component.
 *
 * Verifies:
 * 1. Visible picker parity: When suggested_assignee (e.g. 'coder') is not in the board assignees list
 *    (e.g. ['reviewer']), the rendered select dropdown still includes 'coder' in its options,
 *    selectEl.value is 'coder', and clicking Reassign dispatches POST with { profile: 'coder' }.
 * 2. Stable-key rerender synchronization: When DiagnosticCard updates with a new suggested_assignee prop
 *    under a stable key, selectEl.value synchronizes with the new target and dispatches the updated profile.
 * 3. Manual picker selection: When user changes select to another option, value and POST payload update.
 * 4. Disabled state when unassigned: When value is empty, Reassign button is disabled.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

// @ts-ignore jsdom declaration
import { JSDOM } from 'jsdom'
import React from 'react'
import ReactDOMClient from 'react-dom/client'
import { describe, it } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..')
const BUNDLE_PATH = path.join(REPO_ROOT, 'plugins', 'kanban', 'dashboard', 'dist', 'index.js')

interface DiagnosticAction {
  kind: string
  label?: string
  payload?: {
    suggested_assignee?: string
    current_assignee?: string
    reclaim_first?: boolean
    command?: string
    url?: string
  }
  suggested?: boolean
}

interface DiagnosticItem {
  kind: string
  severity?: string
  title: string
  detail?: string
  actions?: DiagnosticAction[]
  data?: Record<string, unknown>
}

interface TaskItem {
  id: string
  title: string
  assignee?: string
  status?: string
}

interface PostCall {
  url: string
  opts: {
    method: string
    headers: Record<string, string>
    body: string
  }
}

function setupDiagnosticCardHarness() {
  const dom = new JSDOM('<!DOCTYPE html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost',
  })

  // Set up React Act and DOM globals
  const win = dom.window

  const g = globalThis as any
  g.window = win
  g.document = win.document

  try {
    Object.defineProperty(globalThis, 'navigator', {
      value: win.navigator,
      configurable: true,
      writable: true,
    })
  } catch (_) {
    g.navigator = win.navigator
  }

  g.HTMLElement = win.HTMLElement
  g.Node = win.Node
  g.IS_REACT_ACT_ENVIRONMENT = true


  let registeredPage: any = null
  const postCalls: PostCall[] = []

  const SDK = {
    React,
    components: {

      Button: (props: any) => React.createElement('button', props),

      Input: (props: any) => React.createElement('input', props),

      Label: (props: any) => React.createElement('label', props),

      Select: (props: any) => React.createElement('select', props),

      SelectOption: (props: any) => React.createElement('option', props),

      Card: (props: any) => React.createElement('div', props),

      CardContent: (props: any) => React.createElement('div', props),

      Badge: (props: any) => React.createElement('span', props),
    },
    hooks: {
      useState: React.useState,
      useEffect: React.useEffect,
      useCallback: React.useCallback,
      useMemo: React.useMemo,
      useRef: React.useRef,
    },

    utils: {

      cn: (...args: any[]) => args.filter(Boolean).join(' '),
      timeAgo: () => 'just now',
    },
    useI18n: () => ({ t: {}, locale: 'en' }),

    fetchJSON: async (url: string, opts: any) => {
      postCalls.push({ url, opts })

      return { ok: true, task: {} }
    },
  }

  win.__HERMES_PLUGIN_SDK__ = SDK
  win.__HERMES_PLUGINS__ = {

    register: (_name: string, component: any) => {
      registeredPage = component
    },
  }

  const bundleCode = fs.readFileSync(BUNDLE_PATH, 'utf8')
  // Evaluate bundle in window context
  const evalFn = new Function('window', 'document', 'navigator', 'globalThis', bundleCode)
  evalFn(win, win.document, win.navigator, g)

  const DiagnosticCard = registeredPage?.DiagnosticCard
  assert.ok(DiagnosticCard, 'DiagnosticCard component must be exported on registeredPage')

  const container = win.document.getElementById('root') as HTMLElement
  assert.ok(container, 'Root container must exist')
  const root = ReactDOMClient.createRoot(container)

  return {
    win,
    container,
    root,
    DiagnosticCard,
    postCalls,
  }
}

describe('DiagnosticCard visible picker parity and rerender synchronization', () => {
  it('guarantees suggested assignee is visible in picker options when missing from assignees prop', async () => {
    const harness = setupDiagnosticCardHarness()
    const { container, root, DiagnosticCard, postCalls } = harness

    const diag: DiagnosticItem = {
      kind: 'role_assignee_mismatch',
      severity: 'warning',
      title: 'Title role prefix mismatches assignee',
      detail: 'Task title specifies Coder: but assignee is reviewer',
      actions: [
        {
          kind: 'reassign',
          label: 'Reassign to @coder',
          payload: { suggested_assignee: 'coder', current_assignee: 'reviewer' },
          suggested: true,
        },
      ],
    }

    const task: TaskItem = { id: 't_test_1', title: 'Coder: fix bug', assignee: 'reviewer' }

    // assignees only contains 'reviewer' (producer has no non-archived tasks assigned to 'coder')
    await React.act(async () => {
      root.render(
        React.createElement(DiagnosticCard, {
          key: 'diag-card-stable-key',
          diag,
          task,
          boardSlug: 'default',
          assignees: ['reviewer'],
          onRefresh: () => {},
        }),
      )
    })

    const selectEl = container.querySelector('select') as HTMLSelectElement
    assert.ok(selectEl, 'Select element must be rendered')

    // All options in select
    const optionValues = Array.from(selectEl.options).map((o: HTMLOptionElement) => o.value)
    assert.ok(
      optionValues.includes('coder'),
      `Picker options must include suggested assignee 'coder', got: ${JSON.stringify(optionValues)}`,
    )
    assert.equal(
      selectEl.value,
      'coder',
      `Controlled select value must be 'coder', got: '${selectEl.value}'`,
    )

    // Click Reassign button
    const buttons = Array.from(container.querySelectorAll('button')) as HTMLButtonElement[]
    const reassignBtn = buttons.find((b) => b.textContent?.includes('Reassign'))
    assert.ok(reassignBtn, 'Reassign button must exist')

    await React.act(async () => {
      reassignBtn.click()
      await new Promise((r) => setTimeout(r, 20))
    })

    assert.equal(postCalls.length, 1, 'Clicking reassign must make exactly 1 POST call')
    const body = JSON.parse(postCalls[0].opts.body)
    assert.equal(body.profile, 'coder', `POST payload profile must be 'coder', got: '${body.profile}'`)
  })

  it('synchronizes picker value and options across stable-key rerenders', async () => {
    const harness = setupDiagnosticCardHarness()
    const { container, root, DiagnosticCard, postCalls } = harness

    const diagStep1: DiagnosticItem = {
      kind: 'role_assignee_mismatch',
      severity: 'warning',
      title: 'Title role prefix mismatches assignee',
      detail: 'Mismatch detail',
      actions: [
        {
          kind: 'reassign',
          label: 'Reassign to @coder',
          payload: { suggested_assignee: 'coder', current_assignee: 'reviewer' },
          suggested: true,
        },
      ],
    }

    const taskStep1: TaskItem = { id: 't_rerender_1', title: 'Coder: fix bug', assignee: 'reviewer' }

    const diagStep2: DiagnosticItem = {
      kind: 'role_assignee_mismatch',
      severity: 'warning',
      title: 'Title role prefix mismatches assignee',
      detail: 'Mismatch detail',
      actions: [
        {
          kind: 'reassign',
          label: 'Reassign to @reviewer',
          payload: { suggested_assignee: 'reviewer', current_assignee: 'coder' },
          suggested: true,
        },
      ],
    }

    const taskStep2: TaskItem = { id: 't_rerender_1', title: 'Reviewer: review PR', assignee: 'coder' }

    // Step 1 render
    await React.act(async () => {
      root.render(
        React.createElement(DiagnosticCard, {
          key: 'diag-card-stable-key',
          diag: diagStep1,
          task: taskStep1,
          boardSlug: 'default',
          assignees: ['coder', 'reviewer', 'devops'],
          onRefresh: () => {},
        }),
      )
    })

    const selectEl1 = container.querySelector('select') as HTMLSelectElement
    assert.equal(selectEl1.value, 'coder', `Initial step must select 'coder', got: '${selectEl1.value}'`)

    // Step 2 rerender under the same stable key
    await React.act(async () => {
      root.render(
        React.createElement(DiagnosticCard, {
          key: 'diag-card-stable-key',
          diag: diagStep2,
          task: taskStep2,
          boardSlug: 'default',
          assignees: ['coder', 'reviewer', 'devops'],
          onRefresh: () => {},
        }),
      )
    })

    const selectEl2 = container.querySelector('select') as HTMLSelectElement
    assert.equal(
      selectEl2.value,
      'reviewer',
      `Rerendered step under stable key must update select value to 'reviewer', got: '${selectEl2.value}'`,
    )

    // Click Reassign
    const buttons = Array.from(container.querySelectorAll('button')) as HTMLButtonElement[]
    const reassignBtn = buttons.find((b) => b.textContent?.includes('Reassign'))
    assert.ok(reassignBtn, 'Reassign button must exist')

    await React.act(async () => {
      reassignBtn.click()
      await new Promise((r) => setTimeout(r, 20))
    })

    assert.equal(postCalls.length, 1, 'Clicking reassign must make exactly 1 POST call')
    const body = JSON.parse(postCalls[0].opts.body)
    assert.equal(body.profile, 'reviewer', `POST payload profile must be 'reviewer', got: '${body.profile}'`)
  })

  it('allows manual picker override and posts manually selected profile', async () => {
    const harness = setupDiagnosticCardHarness()
    const { container, root, DiagnosticCard, postCalls } = harness

    const diag: DiagnosticItem = {
      kind: 'role_assignee_mismatch',
      severity: 'warning',
      title: 'Title role prefix mismatches assignee',
      detail: 'Mismatch detail',
      actions: [
        {
          kind: 'reassign',
          label: 'Reassign to @coder',
          payload: { suggested_assignee: 'coder', current_assignee: 'reviewer' },
          suggested: true,
        },
      ],
    }

    const task: TaskItem = { id: 't_manual_1', title: 'Coder: fix bug', assignee: 'reviewer' }

    await React.act(async () => {
      root.render(
        React.createElement(DiagnosticCard, {
          key: 'diag-card-stable-key',
          diag,
          task,
          boardSlug: 'default',
          assignees: ['coder', 'reviewer', 'devops'],
          onRefresh: () => {},
        }),
      )
    })

    const selectEl = container.querySelector('select') as HTMLSelectElement
    assert.equal(selectEl.value, 'coder')

    // Simulate user selecting 'devops'
    await React.act(async () => {
      selectEl.value = 'devops'
      selectEl.dispatchEvent(new harness.win.Event('change', { bubbles: true }))
    })

    assert.equal(selectEl.value, 'devops', `After manual change, value must be 'devops'`)

    const buttons = Array.from(container.querySelectorAll('button')) as HTMLButtonElement[]
    const reassignBtn = buttons.find((b) => b.textContent?.includes('Reassign'))
    assert.ok(reassignBtn, 'Reassign button must exist')

    await React.act(async () => {
      reassignBtn.click()
      await new Promise((r) => setTimeout(r, 20))
    })

    assert.equal(postCalls.length, 1)
    const body = JSON.parse(postCalls[0].opts.body)
    assert.equal(body.profile, 'devops', `POST payload profile must be 'devops'`)
  })

  it('disables reassign action button when unassigned or no profile is selected', async () => {
    const harness = setupDiagnosticCardHarness()
    const { container, root, DiagnosticCard } = harness

    const diag: DiagnosticItem = {
      kind: 'role_assignee_mismatch',
      severity: 'warning',
      title: 'Title role prefix mismatches assignee',
      detail: 'Mismatch detail',
      actions: [
        {
          kind: 'reassign',
          label: 'Reassign',
          payload: {},
        },
      ],
    }

    const task: TaskItem = { id: 't_unassigned_1', title: 'Task without assignee' }

    await React.act(async () => {
      root.render(
        React.createElement(DiagnosticCard, {
          key: 'diag-card-stable-key',
          diag,
          task,
          boardSlug: 'default',
          assignees: ['coder', 'reviewer'],
          onRefresh: () => {},
        }),
      )
    })

    const selectEl = container.querySelector('select') as HTMLSelectElement
    assert.equal(selectEl.value, '', "Initial select value must be empty '' when unassigned")

    const buttons = Array.from(container.querySelectorAll('button')) as HTMLButtonElement[]
    const reassignBtn = buttons.find((b) => b.textContent?.includes('Reassign'))
    assert.ok(reassignBtn, 'Reassign button must exist')
    assert.equal(reassignBtn.disabled, true, 'Reassign button must be disabled when unassigned')

    // Selecting an option enables the button
    await React.act(async () => {
      selectEl.value = 'coder'
      selectEl.dispatchEvent(new harness.win.Event('change', { bubbles: true }))
    })
    assert.equal(reassignBtn.disabled, false, 'Reassign button must be enabled when a profile is selected')

    // Changing back to unassigned disables the button again
    await React.act(async () => {
      selectEl.value = ''
      selectEl.dispatchEvent(new harness.win.Event('change', { bubbles: true }))
    })
    assert.equal(reassignBtn.disabled, true, 'Reassign button must be disabled when select is changed back to unassigned')
    assert.equal(selectEl.value, '', "Select value must return to '' when changed back to unassigned")
  })
})

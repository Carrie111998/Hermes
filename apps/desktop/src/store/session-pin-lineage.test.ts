/**
 * GateGuard 事实呈报:
 * 1. 调用方（importers）：仅 vitest 运行本文件；无生产模块 import
 * 2. 受影响 API：无。覆盖既有 pinSession/unpinSession 行为
 * 3. 数据 schema：不读写生产 DB；仅测内存 $pinnedSessionIds / $sessions
 * 4. 逐字指令：用户「目前置顶的标签有的不能取消置顶，这个问题需要用第一性原理找出根因并修复」
 */
import { beforeEach, describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { $pinnedSessionIds, pinSession, unpinSession } from './layout'
import { $sessions } from './session'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

beforeEach(() => {
  $sessions.set([])
  $pinnedSessionIds.set([])
})

describe('pinSession / unpinSession lineage collapse', () => {
  it('pinSession stores the durable root and strips tip aliases', () => {
    $sessions.set([row('tip', { _lineage_root_id: 'root' })])
    $pinnedSessionIds.set(['tip', 'unrelated'])

    pinSession('tip')

    expect($pinnedSessionIds.get()).toEqual(['unrelated', 'root'])
  })

  it('unpinSession(root) drops a leftover tip alias for the same conversation', () => {
    $sessions.set([row('tip', { _lineage_root_id: 'root' })])
    $pinnedSessionIds.set(['tip', 'root', 'other'])

    unpinSession('root')

    expect($pinnedSessionIds.get()).toEqual(['other'])
  })

  it('unpinSession(tip) drops the durable root alias too', () => {
    $sessions.set([row('tip', { _lineage_root_id: 'root' })])
    $pinnedSessionIds.set(['root'])

    unpinSession('tip')

    expect($pinnedSessionIds.get()).toEqual([])
  })
})

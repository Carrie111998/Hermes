import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesPushRequest, HermesReviewFile } from '@/global'
import {
  $reviewCommitMsgBusy,
  $reviewFiles,
  $reviewPushRequest,
  $reviewShipBusy,
  $reviewShipInfo
} from '@/store/review'
import { $currentCwd } from '@/store/session'

import { ReviewShipBar } from './ship-bar'

const pushRequest: HermesPushRequest = {
  changeSetDigest: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
  commitSha: 'abcdef0123456789',
  createdAt: '2026-08-06T00:00:00.000Z',
  destinationBranch: 'feature/workspace',
  expiresAt: '2099-08-06T00:10:00.000Z',
  remote: 'origin',
  remoteUrl: 'https://github.com/example/project.git',
  remoteUrlDigest: 'url-digest',
  requestId: 'request-1'
}

function installBridge() {
  const createPushRequest = vi.fn(async () => pushRequest)

  const pushApproved = vi.fn(async () => ({ commitSha: pushRequest.commitSha, ok: true }))

  ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
    git: {
      review: {
        createPushRequest,
        pushApproved,
        shipInfo: vi.fn(async () => ({ ghReady: false, pr: null, pushAvailable: false }))
      }
    }
  }

  return { createPushRequest, pushApproved }
}

beforeEach(() => {
  $currentCwd.set('/repo')
  $reviewCommitMsgBusy.set(false)
  $reviewFiles.set([])
  $reviewPushRequest.set(null)
  $reviewShipBusy.set(false)
  $reviewShipInfo.set({ ghReady: false, pr: null, pushAvailable: true })
})

afterEach(() => {
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('ReviewShipBar push approval', () => {
  it('shows the host snapshot before sending an explicit approval', async () => {
    const bridge = installBridge()

    render(<ReviewShipBar />)
    fireEvent.click(screen.getByRole('button', { name: 'Review and approve push' }))

    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(screen.getByText(`Commit: ${pushRequest.commitSha}`)).toBeTruthy()
    expect(screen.getByText('Target: https://github.com/example/project.git → feature/workspace')).toBeTruthy()
    expect(screen.getByText(/Destination fingerprint: url-digest/)).toBeTruthy()
    expect(bridge.pushApproved).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Approve exact push' }))

    await waitFor(() => expect(bridge.pushApproved).toHaveBeenCalledTimes(1))
    expect(bridge.pushApproved).toHaveBeenCalledWith(
      '/repo',
      expect.objectContaining({ ...pushRequest, approved: true, approvedBy: 'local-desktop-user' })
    )
  })

  it('never offers a combined commit and push action', () => {
    installBridge()
    $reviewFiles.set([
      {
        added: 1,
        path: 'src/change.ts',
        removed: 0,
        staged: false,
        status: 'modified'
      } as HermesReviewFile
    ])

    render(<ReviewShipBar />)

    expect(screen.getByRole('button', { name: 'Commit' })).toBeTruthy()
    expect(screen.queryByText(/Commit\s*&\s*Push/i)).toBeNull()
  })
})

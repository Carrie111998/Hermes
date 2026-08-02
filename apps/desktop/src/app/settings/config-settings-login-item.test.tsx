import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

const loginItemGet = vi.fn()
const loginItemSet = vi.fn()

beforeEach(() => {
  loginItemGet.mockReset()
  loginItemSet.mockReset()
  loginItemGet.mockResolvedValue({ openAtLogin: false })
  loginItemSet.mockResolvedValue({ openAtLogin: true })

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { loginItem: { get: loginItemGet, set: loginItemSet } }
  })
})

describe('LoginItemSetting', () => {
  it('loads the current login-item state on mount', async () => {
    const { LoginItemSetting } = await import('./config-settings')

    render(<LoginItemSetting />)

    const toggle = await screen.findByRole('switch', { name: 'Launch Hermes Desktop at login' })
    expect(loginItemGet).toHaveBeenCalledOnce()
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })

  it('updates the login item when toggled on', async () => {
    const { LoginItemSetting } = await import('./config-settings')

    render(<LoginItemSetting />)

    const toggle = await screen.findByRole('switch', { name: 'Launch Hermes Desktop at login' })
    fireEvent.click(toggle)

    await waitFor(() => expect(loginItemSet).toHaveBeenCalledWith({ openAtLogin: true }))
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  })

  it('treats a missing login-item API as disabled (off)', async () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {}
    })
    const { LoginItemSetting } = await import('./config-settings')

    render(<LoginItemSetting />)

    const toggle = await screen.findByRole('switch', { name: 'Launch Hermes Desktop at login' })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })
})

import { configure } from '@testing-library/react'

// React 19 + Testing Library 16: opt into the act environment so render(),
// fireEvent(), and findBy* queries automatically flush state updates without
// spurious "not wrapped in act(...)" warnings.
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true

// findBy*/waitFor default to a 1000ms deadline — too tight for async-heavy
// panels (radix menus, refetch chains) when the full suite runs under xdist
// CPU contention in CI. Success still resolves the instant the node appears;
// the wider deadline only absorbs a starved runner, killing timing flakes.
configure({ asyncUtilTimeout: 5000 })

// Ensure window exists in jsdom environment
if (typeof global.window === 'undefined') {
  Object.defineProperty(global, 'window', {
    value: {},
    writable: true,
    configurable: true,
  })
}

// Ensure window.localStorage exists
if (typeof window !== 'undefined' && !window.localStorage) {
  Object.defineProperty(window, 'localStorage', {
    value: {
      getItem: vi.fn(),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
    },
    writable: true,
    configurable: true,
  })
}

// Mock window.dispatchEvent
window.dispatchEvent = vi.fn((event) => true)

// Mock window.requestAnimationFrame and cancelAnimationFrame
window.requestAnimationFrame = vi.fn((cb) => setTimeout(cb, 16))
window.cancelAnimationFrame = vi.fn((id) => clearTimeout(id))
window.requestIdleCallback = vi.fn((cb) => setTimeout(cb, 0))
window.cancelIdleCallback = vi.fn((id) => clearTimeout(id))

// Mock window.hermesDesktop
window.hermesDesktop = {
  onWindowStateChanged: vi.fn((callback) => {
    return () => {}
  })
}

// Mock document.visibilityState
Object.defineProperty(document, 'visibilityState', {
  value: 'visible',
  writable: true,
  configurable: true,
})

// Mock document.hasFocus
Object.defineProperty(document, 'hasFocus', {
  value: vi.fn(() => true),
  writable: true,
  configurable: true,
})

// Mock window.requestAnimationFrame and cancelAnimationFrame
global.requestAnimationFrame = vi.fn((cb) => setTimeout(cb, 16))
global.cancelAnimationFrame = vi.fn((id) => clearTimeout(id))

// Mock window.requestIdleCallback
global.requestIdleCallback = vi.fn((cb) => setTimeout(cb, 0))
global.cancelIdleCallback = vi.fn((id) => clearTimeout(id))

// Mock window.hermesDesktop for tests that need it
Object.defineProperty(window, 'hermesDesktop', {
  value: {
    onWindowStateChanged: vi.fn((callback) => {
      return () => {}
    })
  },
  writable: true,
  configurable: true,
})

// Mock window.localStorage
const mockLocalStorage = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
}

Object.defineProperty(window, 'localStorage', {
  value: {
    getItem: vi.fn(),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  },
  writable: true,
  configurable: true,
})

// Mock window.dispatchEvent
window.dispatchEvent = vi.fn((event) => true)

// Mock document.visibilityState
Object.defineProperty(document, 'visibilityState', {
  value: 'visible',
  writable: true,
  configurable: true,
})

// Mock document.hasFocus
Object.defineProperty(document, 'hasFocus', {
  value: vi.fn(() => true),
  writable: true,
  configurable: true,
})

// Mock window.requestAnimationFrame and cancelAnimationFrame
global.requestAnimationFrame = vi.fn((cb) => setTimeout(cb, 16))
global.cancelAnimationFrame = vi.fn((id) => clearTimeout(id))

// Mock window.requestIdleCallback
global.requestIdleCallback = vi.fn((cb) => setTimeout(cb, 0))
global.cancelIdleCallback = vi.fn((id) => clearTimeout(id))

// Mock window.hermesDesktop
Object.defineProperty(window, 'hermesDesktop', {
  value: {
    onWindowStateChanged: vi.fn((callback) => {
      return () => {}
    })
  },
  writable: true,
  configurable: true,
})

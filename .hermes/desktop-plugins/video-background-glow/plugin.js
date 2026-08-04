/**
 * Video Background + Glowing Text + Quantum Plasma Galaxy Plugin for Hermes Desktop
 * Apply video backgrounds, glowing text effects, and quantum plasma galaxy visualization to Hermes desktop app
 * 
 * Save as: ~/.hermes/desktop-plugins/video-background-glow/plugin.js
 * Then run "Reload desktop plugins" from ⌘K in the desktop app.
 */

import { cn, haptic, host, Tip, usePluginI18n, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'video-background-glow'

// Global state for the visualization
let visualizationMode = 'quantum-galaxy' // 'video' or 'quantum-galaxy'
let quantumGalaxyInstance = null
let videoBackgroundContainer = null
let proceduralAnimationFrame = null
let styleElement = null

// React components for UI
function QuantumGalaxyPane() {
  const t = usePluginI18n(ID)
  const [mode, setMode] = useValue(visualizationMode, 'quantum-galaxy')
  const [enabled, setEnabled] = useValue(true, true)

  const switchMode = async (newMode) => {
    visualizationMode = newMode
    setMode(newMode)
    localStorage.setItem('video-background-glow-mode', newMode)
    await applyMode(newMode)
    host.notify({ kind: 'info', message: t(newMode === 'quantum-galaxy' ? 'switchedToQuantum' : 'switchedToVideo') })
  }

  const toggleEnabled = async () => {
    const newEnabled = !enabled
    setEnabled(newEnabled)
    if (newEnabled) {
      await applyMode(mode)
    } else {
      await cleanup()
    }
    host.notify({ kind: 'info', message: t(newEnabled ? 'enabled' : 'disabled') })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-accent)', children: t('paneTitle') }),
      
      // Enable/Disable toggle
      jsx('label', {
        className: 'flex items-center gap-2 cursor-pointer',
        children: [
          jsx('input', {
            type: 'checkbox',
            checked: enabled,
            onChange: toggleEnabled,
            className: 'w-4 h-4 accent-(--ui-accent)'
          }),
          jsx('span', { className: 'text-(--ui-text-secondary)', children: t('enableLabel') })
        ]
      }),

      // Mode selector
      jsx('div', { className: 'text-(--ui-text-tertiary) text-xs mb-1', children: t('modeLabel') }),
      jsx('div', {
        className: 'flex gap-2',
        children: [
          jsx('button', {
            className: cn(
              'flex-1 py-2 px-3 rounded text-xs font-mono transition-colors',
              mode === 'quantum-galaxy' 
                ? 'bg-(--ui-accent) text-(--ui-bg-primary) shadow-[0_0_8px_var(--ui-accent)]'
                : 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary) hover:bg-(--ui-bg-secondary)'
            ),
            onClick: () => switchMode('quantum-galaxy'),
            children: '🌌 ' + t('quantumGalaxy')
          }),
          jsx('button', {
            className: cn(
              'flex-1 py-2 px-3 rounded text-xs font-mono transition-colors',
              mode === 'video' 
                ? 'bg-(--ui-accent) text-(--ui-bg-primary) shadow-[0_0_8px_var(--ui-accent)]'
                : 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary) hover:bg-(--ui-bg-secondary)'
            ),
            onClick: () => switchMode('video'),
            children: '🎬 ' + t('videoBackground')
          })
        ]
      }),

      // Info
      jsx('div', {
        className: 'text-(--ui-text-quaternary) text-xs border-t pt-2',
        children: t('description')
      })
    ]
  })
}

function StatusChip() {
  const t = usePluginI18n(ID)
  const [mode, setMode] = useValue(visualizationMode, 'quantum-galaxy')
  const [enabled, setEnabled] = useValue(true, true)

  return jsx(Tip, {
    label: t('chipTip'),
    children: jsx('button', {
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      type: 'button',
      onClick: () => {
        haptic('tap')
        const newEnabled = !enabled
        setEnabled(newEnabled)
        if (newEnabled) applyMode(mode)
        else cleanup()
        host.notify({ kind: 'info', message: t(newEnabled ? 'enabled' : 'disabled') })
      },
      children: enabled ? (mode === 'quantum-galaxy' ? '🌌' : '🎬') : '⚫'
    })
  })
}

// Core functionality
async function loadQuantumGalaxyModule() {
  if (window.QuantumGalaxy) return Promise.resolve()
  
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = '/plugins/video-background-glow/js/quantum-galaxy.js'
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('Failed to load quantum-galaxy.js'))
    document.head.appendChild(script)
  })
}

function injectStyles() {
  if (styleElement) styleElement.remove()
  
  const css = `
    :root {
      --video-bg-opacity: 0.35;
      --grid-opacity: 0.03;
      --orb-opacity: 0.4;
      --glow-intensity: 1;
    }

    .hermes-video-bg-container {
      position: fixed;
      inset: 0;
      z-index: -2;
      pointer-events: none;
      overflow: hidden;
    }

    .hermes-video-bg {
      width: 100%;
      height: 100%;
      object-fit: cover;
      opacity: var(--video-bg-opacity);
      filter: saturate(1.2) contrast(1.1);
      display: block;
    }

    .hermes-video-bg::after {
      content: '';
      position: absolute;
      inset: 0;
      background: 
        radial-gradient(ellipse at 20% 20%, rgba(0, 255, 234, 0.08) 0%, transparent 50%),
        radial-gradient(ellipse at 80% 80%, rgba(255, 0, 234, 0.08) 0%, transparent 50%),
        radial-gradient(ellipse at 50% 50%, rgba(0, 0, 0, 0.4) 0%, transparent 70%);
      pointer-events: none;
    }

    .hermes-grid-overlay {
      position: fixed;
      inset: 0;
      z-index: -1;
      pointer-events: none;
      background-image: 
        linear-gradient(rgba(0, 255, 234, var(--grid-opacity)) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0, 255, 234, var(--grid-opacity)) 1px, transparent 1px);
      background-size: 60px 60px;
      animation: gridMove 20s linear infinite;
    }

    @keyframes gridMove {
      0% { transform: translate(0, 0); }
      100% { transform: translate(60px, 60px); }
    }

    .hermes-orb {
      position: fixed;
      border-radius: 50%;
      filter: blur(60px);
      opacity: var(--orb-opacity);
      pointer-events: none;
      animation: orbFloat 15s ease-in-out infinite;
    }

    .hermes-orb-1 { 
      width: 400px; height: 400px; 
      background: var(--accent-cyan, #00ffea); 
      top: -100px; left: -100px; 
      animation-delay: 0s; 
    }
    .hermes-orb-2 { 
      width: 300px; height: 300px; 
      background: var(--accent-magenta, #ff00ea); 
      bottom: -50px; right: -50px; 
      animation-delay: -5s; 
    }
    .hermes-orb-3 { 
      width: 200px; height: 200px; 
      background: var(--accent-gold, #ffd700); 
      top: 50%; left: 50%; 
      transform: translate(-50%, -50%); 
      animation-delay: -10s; 
    }

    @keyframes orbFloat {
      0%, 100% { transform: translate(0, 0) scale(1); }
      25% { transform: translate(30px, -20px) scale(1.1); }
      50% { transform: translate(-20px, 30px) scale(0.95); }
      75% { transform: translate(10px, -30px) scale(1.05); }
    }

    /* Glowing Text for Hermes UI */
    .hermes-glow-text {
      position: relative;
      display: inline-block;
    }

    .hermes-glow-text::before {
      content: attr(data-glow-text);
      position: absolute;
      inset: 0;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      filter: blur(8px);
      opacity: 0.6;
      animation: glowPulse 3s ease-in-out infinite alternate;
    }

    .hermes-glow-text.cyan::after { 
      text-shadow: 0 0 10px #00ffea, 0 0 20px #00ffea44, 0 0 30px #00ffea; 
    }
    .hermes-glow-text.magenta::after { 
      text-shadow: 0 0 10px #ff00ea, 0 0 20px #ff00ea44, 0 0 30px #ff00ea; 
    }
    .hermes-glow-text.gold::after { 
      text-shadow: 0 0 10px #ffd700, 0 0 20px #ffd70044, 0 0 30px #ffd700; 
    }

    @keyframes glowPulse {
      0% { filter: blur(8px); opacity: 0.4; }
      100% { filter: blur(16px); opacity: 0.8; }
    }

    /* Apply glowing gradient to Hermes titles */
    .hermes-title, 
    .hermes-header h1,
    .hermes-chat-title,
    .hermes-node-title {
      font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
      background: linear-gradient(135deg, #f0f0f5 0%, #00ffea 50%, #f0f0f5 100%) !important;
      background-size: 200% 200% !important;
      -webkit-background-clip: text !important;
      -webkit-text-fill-color: transparent !important;
      background-clip: text !important;
      animation: gradientShift 4s ease-in-out infinite !important;
      position: relative !important;
    }

    .hermes-title::before,
    .hermes-header h1::before,
    .hermes-chat-title::before,
    .hermes-node-title::before {
      content: attr(data-glow-text) !important;
      position: absolute !important;
      inset: 0 !important;
      -webkit-text-fill-color: transparent !important;
      background-clip: text !important;
      filter: blur(8px) !important;
      opacity: 0.6 !important;
      animation: glowPulse 3s ease-in-out infinite alternate !important;
    }

    @keyframes gradientShift {
      0%, 100% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
    }
  `;

  styleElement = document.createElement('style')
  styleElement.textContent = css
  document.head.appendChild(styleElement)
}

async function applyMode(mode) {
  await cleanup()
  injectStyles()
  
  if (mode === 'quantum-galaxy') {
    await initQuantumGalaxy()
  } else {
    await initVideoBackground()
  }
}

async function initQuantumGalaxy() {
  await loadQuantumGalaxyModule()
  
  // Create container
  const container = document.createElement('div')
  container.className = 'hermes-quantum-galaxy-container'
  container.style.cssText = 'position:fixed;inset:0;z-index:-2;pointer-events:none;'
  container.setAttribute('aria-hidden', 'true')
  document.body.appendChild(container)
  
  if (window.QuantumGalaxy) {
    quantumGalaxyInstance = new window.QuantumGalaxy({
      container: container,
      autoStart: true
    })
    window.galaxy = quantumGalaxyInstance
  }
}

async function initVideoBackground() {
  // Create video background container
  const container = document.createElement('div')
  container.className = 'hermes-video-bg-container'
  container.setAttribute('aria-hidden', 'true')
  
  // Create procedural background (since no video configured)
  const canvas = document.createElement('canvas')
  canvas.className = 'hermes-video-bg-procedural'
  canvas.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;'
  container.appendChild(canvas)
  
  document.body.appendChild(container)
  videoBackgroundContainer = container
  
  // Add grid overlay
  const grid = document.createElement('div')
  grid.className = 'hermes-grid-overlay'
  grid.setAttribute('aria-hidden', 'true')
  document.body.appendChild(grid)
  
  // Add floating orbs
  ['1', '2', '3'].forEach(num => {
    const orb = document.createElement('div')
    orb.className = `hermes-orb hermes-orb-${num}`
    orb.setAttribute('aria-hidden', 'true')
    document.body.appendChild(orb)
  })
  
  // Start procedural animation
  startProceduralAnimation(canvas)
}

function startProceduralAnimation(canvas) {
  const ctx = canvas.getContext('2d')
  let time = 0
  
  function resize() {
    canvas.width = window.innerWidth * devicePixelRatio
    canvas.height = window.innerHeight * devicePixelRatio
    canvas.style.width = window.innerWidth + 'px'
    canvas.style.height = window.innerHeight + 'px'
  }
  
  function draw() {
    time += 0.008
    const w = canvas.width, h = canvas.height
    
    ctx.fillStyle = '#0a0a0f'
    ctx.fillRect(0, 0, w, h)
    
    for (let i = 0; i < 3; i++) {
      const gradient = ctx.createLinearGradient(0, 0, w, h)
      const phase = time + i * 2
      gradient.addColorStop(0, `hsla(${180 + Math.sin(phase) * 30}, 100%, 50%, 0.03)`)
      gradient.addColorStop(0.5, `hsla(${300 + Math.cos(phase) * 30}, 100%, 50%, 0.02)`)
      gradient.addColorStop(1, `hsla(${60 + Math.sin(phase * 0.7) * 30}, 100%, 50%, 0.03)`)
      ctx.fillStyle = gradient
      ctx.fillRect(0, (Math.sin(phase) * 0.3 + 0.5) * h - 200, w, 400)
    }
    
    ctx.fillStyle = '#00ffea22'
    for (let i = 0; i < 50; i++) {
      const x = (Math.sin(time * 0.5 + i * 0.3) * 0.5 + 0.5) * w
      const y = (Math.cos(time * 0.3 + i * 0.7) * 0.5 + 0.5) * h
      const size = Math.max(1, Math.sin(time + i) * 2 + 2)
      ctx.beginPath()
      ctx.arc(x, y, size, 0, Math.PI * 2)
      ctx.fill()
    }
    
    proceduralAnimationFrame = requestAnimationFrame(draw)
  }
  
  resize()
  window.addEventListener('resize', resize)
  draw()
}

async function cleanup() {
  // Cleanup quantum galaxy
  if (quantumGalaxyInstance) {
    quantumGalaxyInstance.destroy()
    quantumGalaxyInstance = null
  }
  const qContainer = document.querySelector('.hermes-quantum-galaxy-container')
  if (qContainer) qContainer.remove()
  
  // Cleanup video background
  const vContainer = document.querySelector('.hermes-video-bg-container')
  if (vContainer) vContainer.remove()
  const grid = document.querySelector('.hermes-grid-overlay')
  if (grid) grid.remove()
  ['orb-1', 'orb-2', 'orb-3'].forEach(id => {
    const orb = document.querySelector(`.hermes-orb-${id}`)
    if (orb) orb.remove()
  })
  
  // Cleanup procedural animation
  if (proceduralAnimationFrame) {
    cancelAnimationFrame(proceduralAnimationFrame)
    proceduralAnimationFrame = null
  }
  window.removeEventListener('resize', () => {}) // Best effort
  
  // Cleanup styles
  if (styleElement) {
    styleElement.remove()
    styleElement = null
  }
}

// Initialize on load
function initialize() {
  // Load saved mode
  visualizationMode = localStorage.getItem('video-background-glow-mode') || 'quantum-galaxy'
  applyMode(visualizationMode).catch(console.error)
  
  // Apply glowing text styles to existing elements
  applyGlowingTextStyles()
}

function applyGlowingTextStyles() {
  // This runs periodically to catch dynamically added elements
  const observer = new MutationObserver(() => {
    document.querySelectorAll('.hermes-title, .hermes-header h1, .hermes-chat-title, .hermes-node-title').forEach(el => {
      if (!el.dataset.glowProcessed) {
        el.dataset.glowProcessed = 'true'
        el.setAttribute('data-glow-text', el.textContent)
      }
    })
  })
  observer.observe(document.body, { childList: true, subtree: true })
  
  // Initial pass
  setTimeout(() => {
    document.querySelectorAll('.hermes-title, .hermes-header h1, .hermes-chat-title, .hermes-node-title').forEach(el => {
      if (!el.dataset.glowProcessed) {
        el.dataset.glowProcessed = 'true'
        el.setAttribute('data-glow-text', el.textContent)
      }
    })
  }, 500)
}

export default {
  id: ID,
  name: 'Video Background + Glowing Text + Quantum Galaxy',
  defaultEnabled: false, // Opt-in plugin
  
  register(ctx) {
    // Register locale bundles
    ctx.i18n.register({
      en: {
        pluginName: 'Video Background + Glowing Text + Quantum Galaxy',
        pluginDescription: 'Video backgrounds with glowing text, floating orbs, and quantum plasma galaxy for Hermes',
        paneTitle: 'Background Visualization',
        enableLabel: 'Enable Visualization',
        modeLabel: 'Visualization Mode',
        quantumGalaxy: 'Quantum Galaxy',
        videoBackground: 'Video Background',
        switchedToQuantum: 'Switched to Quantum Plasma Galaxy',
        switchedToVideo: 'Switched to Video Background',
        enabled: 'Visualization enabled',
        disabled: 'Visualization disabled',
        description: 'Your Obsidian vault visualized as a living quantum plasma galaxy with plasma, liquid, and vortex physics engines.',
        chipTip: 'Background visualization — click to toggle'
      }
    })

    // Layout pane
    ctx.register({
      id: 'quantum-galaxy-pane',
      area: 'panes',
      title: 'Quantum Galaxy',
      data: { placement: 'right', width: '280px' },
      render: () => jsx(QuantumGalaxyPane, {})
    })

    // Statusbar chip
    ctx.register({
      id: 'bg-viz-chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(StatusChip, {})
    })

    // Initialize on register
    initialize()
  }
}
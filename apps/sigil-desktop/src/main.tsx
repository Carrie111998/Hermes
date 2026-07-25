import '@hermes-desktop/styles.css'
import './sigil.css'

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { SigilOperatorView } from './mission-control'

document.documentElement.classList.add('dark')
document.documentElement.style.colorScheme = 'dark'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <main aria-label="Sigil Mission Control" className="h-full bg-(--ui-bg-primary)">
      <SigilOperatorView />
    </main>
  </StrictMode>
)

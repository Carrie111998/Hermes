import DefaultTheme from 'vitepress/theme'
import type { Theme } from 'vitepress'
import './custom.css'

function ensureLightbox() {
  let lightbox = document.querySelector<HTMLDivElement>('.us-lightbox')
  if (lightbox) return lightbox

  lightbox = document.createElement('div')
  lightbox.className = 'us-lightbox'
  lightbox.setAttribute('aria-hidden', 'true')

  const button = document.createElement('button')
  button.className = 'us-lightbox-close'
  button.type = 'button'
  button.setAttribute('aria-label', '关闭图片预览')
  button.textContent = '×'

  const image = document.createElement('img')
  image.className = 'us-lightbox-image'
  image.alt = ''

  lightbox.append(button, image)
  document.body.append(lightbox)

  const close = () => {
    lightbox?.classList.remove('is-open')
    lightbox?.setAttribute('aria-hidden', 'true')
  }

  button.addEventListener('click', close)
  lightbox.addEventListener('click', (event) => {
    if (event.target === lightbox) close()
  })
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close()
  })

  return lightbox
}

function bindImageLightbox() {
  const lightbox = ensureLightbox()
  const preview = lightbox.querySelector<HTMLImageElement>('.us-lightbox-image')
  if (!preview) return

  document.querySelectorAll<HTMLImageElement>('.vp-doc img').forEach((image) => {
    if (image.dataset.lightboxBound === 'true') return
    image.dataset.lightboxBound = 'true'
    image.addEventListener('click', () => {
      preview.src = image.currentSrc || image.src
      preview.alt = image.alt
      lightbox.classList.add('is-open')
      lightbox.setAttribute('aria-hidden', 'false')
    })
  })
}

function installImageLightbox() {
  if (typeof window === 'undefined') return
  bindImageLightbox()
  const observer = new MutationObserver(bindImageLightbox)
  observer.observe(document.body, { childList: true, subtree: true })
}

export default {
  extends: DefaultTheme,
  enhanceApp(ctx) {
    DefaultTheme.enhanceApp?.(ctx)
    if (typeof window !== 'undefined') {
      window.addEventListener('DOMContentLoaded', installImageLightbox, { once: true })
    }
  }
} satisfies Theme

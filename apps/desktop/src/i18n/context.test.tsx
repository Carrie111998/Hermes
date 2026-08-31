import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesConfigRecord } from '@/hermes'

import { type I18nConfigClient, I18nProvider, useI18n } from './context'
import type { Locale } from './types'

function LanguageProbe({ target = 'zh' }: { target?: Locale }) {
  const { isLoadingConfig, isSavingLocale, locale, saveError, setLocale, t } = useI18n()

  return (
    <div>
      <p data-testid="locale">{locale}</p>
      <p data-testid="label">{t.language.label}</p>
      <p data-testid="save">{t.common.save}</p>
      <p data-testid="loading">{String(isLoadingConfig)}</p>
      <p data-testid="saving">{String(isSavingLocale)}</p>
      <p data-testid="save-error">{saveError?.message ?? ''}</p>
      <button onClick={() => void setLocale(target).catch(() => undefined)} type="button">
        switch
      </button>
    </div>
  )
}

describe('I18nProvider', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('defaults to English without a config client', () => {
    render(
      <I18nProvider configClient={null}>
        <LanguageProbe />
      </I18nProvider>
    )

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })

  it('normalizes an initial locale alias and switches translations', async () => {
    render(
      <I18nProvider configClient={null} initialLocale="zh-CN">
        <LanguageProbe target="en" />
      </I18nProvider>
    )

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    // The locale applies immediately; its message tree is a separate chunk
    // (catalog.ts) and lands a tick later, so assert on the rendered text
    // rather than on the same tick as the locale switch.
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('语言'))

    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(screen.getByTestId('locale').textContent).toBe('en'))
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })

  it('loads the initial locale from display.language config', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'zh-Hans' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh')
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('语言'))
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('keeps English usable when config loading fails', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockRejectedValue(new Error('config unavailable')),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('loads zh-hant from display.language config', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'zh-TW' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('zh-hant')
    await waitFor(() => expect(screen.getByTestId('save').textContent).toBe('儲存'))
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('loads ja from display.language config', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'ja-JP' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('ja')
    await waitFor(() => expect(screen.getByTestId('save').textContent).toBe('保存'))
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('does not overwrite unsupported configured languages', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'de' } }),
      saveConfig: vi.fn()
    }

    render(
      <I18nProvider configClient={configClient} initialLocale="zh">
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
    expect(configClient.saveConfig).not.toHaveBeenCalled()
  })

  it('reads latest config before saving language and preserves unrelated values', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true })

    const latestConfig: HermesConfigRecord = {
      display: { language: 'en', skin: 'slate' },
      terminal: { cwd: '/new' }
    }

    const configClient: I18nConfigClient = {
      getConfig: vi
        .fn()
        .mockResolvedValueOnce({ display: { language: 'en', skin: 'mono' }, terminal: { cwd: '/old' } })
        .mockResolvedValueOnce(latestConfig),
      saveConfig
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    expect(saveConfig).toHaveBeenCalledWith({
      display: { language: 'zh', skin: 'slate' },
      terminal: { cwd: '/new' }
    })
  })

  it('saves newly supported locales to display.language', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true })

    const configClient: I18nConfigClient = {
      getConfig: vi
        .fn()
        .mockResolvedValueOnce({ display: { language: 'en' } })
        .mockResolvedValueOnce({ display: { language: 'en', skin: 'mono' } }),
      saveConfig
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe target="ja" />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    expect(saveConfig).toHaveBeenCalledWith({ display: { language: 'ja', skin: 'mono' } })
    expect(screen.getByTestId('locale').textContent).toBe('ja')
  })

  // Direction and copy must never disagree. Arabic's message tree is a
  // separate chunk (catalog.ts), so if `dir` followed the SELECTED locale
  // while the copy waited on that import, the layout would mirror around
  // English text and then swap again — a visible flip, not the invisible
  // late-text swap an LTR locale gets. Asserting only the settled state
  // (waitFor on the final text) cannot see that window; this checks the
  // invariant holds at every step through it.
  function directionMatchesCopy(): boolean {
    const isRtl = document.documentElement.dir === 'rtl'
    const isArabic = screen.getByTestId('label').textContent !== 'Language'

    return isRtl === isArabic
  }

  it('applies RTL direction for Arabic and restores LTR on switch back', async () => {
    render(
      <I18nProvider configClient={null} initialLocale="ar">
        <LanguageProbe target="en" />
      </I18nProvider>
    )

    // The selection is visible immediately; direction waits for the copy.
    expect(screen.getByTestId('locale').textContent).toBe('ar')
    expect(directionMatchesCopy()).toBe(true)

    await waitFor(() => expect(document.documentElement.dir).toBe('rtl'))
    expect(document.documentElement.lang).toBe('ar')
    expect(directionMatchesCopy()).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(screen.getByTestId('locale').textContent).toBe('en'))
    await waitFor(() => expect(document.documentElement.dir).toBe('ltr'))
    expect(document.documentElement.lang).toBe('en')
    expect(directionMatchesCopy()).toBe(true)
  })

  it('rolls back the visible locale when saving fails', async () => {
    const configClient: I18nConfigClient = {
      getConfig: vi.fn().mockResolvedValue({ display: { language: 'en' } }),
      saveConfig: vi.fn().mockRejectedValue(new Error('save failed'))
    }

    render(
      <I18nProvider configClient={configClient}>
        <LanguageProbe />
      </I18nProvider>
    )

    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('false'))
    fireEvent.click(screen.getByRole('button', { name: 'switch' }))

    await waitFor(() => expect(screen.getByTestId('save-error').textContent).toBe('save failed'))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('label').textContent).toBe('Language')
  })
})

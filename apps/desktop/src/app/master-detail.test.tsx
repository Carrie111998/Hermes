import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'

import { DetailColumn, ListColumn, MasterDetail } from './master-detail'

afterEach(() => {
  cleanup()
  document.documentElement.removeAttribute('data-hermes-mobile')
})

describe('mobile master-detail navigation', () => {
  it('opens a selected inspector and returns to the browse list without stacking both panes', () => {
    document.documentElement.setAttribute('data-hermes-mobile', '')

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <MasterDetail>
          <ListColumn>
            <button data-master-detail-select="" type="button">
              Example skill
            </button>
          </ListColumn>
          <DetailColumn>Example skill details</DetailColumn>
        </MasterDetail>
      </I18nProvider>
    )

    const masterDetail = screen.getByTestId('mobile-master-detail')
    const list = masterDetail.querySelector<HTMLElement>('[data-master-detail-list]')
    const detail = masterDetail.querySelector<HTMLElement>('[data-master-detail-detail]')

    expect(list?.hidden).toBe(false)
    expect(detail?.hidden).toBe(true)

    fireEvent.click(screen.getByText('Example skill'))

    expect(list?.hidden).toBe(true)
    expect(detail?.hidden).toBe(false)
    expect(screen.getByRole('button', { name: 'Back' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))

    expect(list?.hidden).toBe(false)
    expect(detail?.hidden).toBe(true)
  })
})

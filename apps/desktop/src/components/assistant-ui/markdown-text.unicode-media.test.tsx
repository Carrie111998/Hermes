import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { renderMediaTags } from '@/lib/chat-messages'

import { MarkdownTextContent } from './markdown-text'

describe('Unicode MEDIA filenames through the full renderer', () => {
  afterEach(cleanup)

  it('renders a Windows delivery path with a Chinese basename as readable text', async () => {
    const path =
      'C:/Users/user/Downloads/114教學實踐計畫審閱/期末報告/114教學實踐成果交流會_整合修正版.pptx'

    const markdown = renderMediaTags(`MEDIA:${path}`)

    expect(markdown).toContain('File: 114教學實踐成果交流會_整合修正版.pptx')
    render(<MarkdownTextContent isRunning={false} text={markdown} />)

    expect(await screen.findByText('Open 114教學實踐成果交流會_整合修正版.pptx')).toBeTruthy()
  })

  it('preserves the readable link label when the media href basename is encoded more than once', async () => {
    const encodedPath =
      'C:/Users/user/Downloads/114教學實踐計畫審閱/期末報告/114%25E6%2595%2599%25E5%25AD%25B8%25E5%25AF%25A6%25E8%25B8%2590%25E6%2588%2590%25E6%259E%259C%25E4%25BA%25A4%25E6%25B5%2581%25E6%259C%2583_%25E6%2595%25B4%25E5%2590%2588%25E4%25BF%25AE%25E6%25AD%25A3%25E7%2589%2588.pptx'

    const href = `#media:${encodeURIComponent(encodedPath)}`

    render(
      <MarkdownTextContent
        isRunning={false}
        text={`[File: 114教學實踐成果交流會_整合修正版.pptx](${href})`}
      />
    )

    expect(await screen.findByText('Open 114教學實踐成果交流會_整合修正版.pptx')).toBeTruthy()
  })
})

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { DisclosureRow } from './disclosure-row'

describe('DisclosureRow', () => {
  it('keeps trailing status content in flow so it cannot overlap the title', () => {
    const markup = renderToStaticMarkup(
      <DisclosureRow open={false} trailing={<span>68s</span>}>
        <span>Running process</span>
      </DisclosureRow>
    )

    const trailingHost = markup.match(/<span class="([^"]*)"><span>68s<\/span><\/span>/)?.[1]

    expect(trailingHost).toBeDefined()
    expect(trailingHost?.split(' ')).toContain('ml-auto')
    expect(trailingHost?.split(' ')).not.toContain('absolute')
  })
})

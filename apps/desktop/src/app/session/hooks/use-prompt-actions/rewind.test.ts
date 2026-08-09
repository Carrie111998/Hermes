import { describe, expect, it } from 'vitest'

import { truncateSubmitParams } from './rewind'

describe('truncateSubmitParams', () => {
  it('omits truncation fields when no ordinal is set', () => {
    expect(truncateSubmitParams(undefined)).toEqual({})
  })

  it('confirms intent for every ordinal, and the empty edge only for ordinal 0', () => {
    expect(truncateSubmitParams(0)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 0,
      confirm_empty_truncate: true
    })
    expect(truncateSubmitParams(1)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 1
    })
  })

  it('includes truncate_before_message_id when passed', () => {
    expect(truncateSubmitParams(1, 'msg-123')).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 1,
      truncate_before_message_id: 'msg-123'
    })
    expect(truncateSubmitParams(undefined, 'msg-123')).toEqual({
      confirm_truncate: true,
      truncate_before_message_id: 'msg-123'
    })
  })
})

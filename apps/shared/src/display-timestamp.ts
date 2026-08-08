export interface DisplayTimestampOptions {
  enabled: boolean
  format: string
}

const WEEKDAYS_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const
const WEEKDAYS_LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'] as const
const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'] as const

const MONTHS_LONG = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December'
] as const

const pad = (value: number, width = 2) => String(value).padStart(width, '0')

const dayOfYear = (date: Date): number => {
  const start = new Date(date.getFullYear(), 0, 1)
  const current = new Date(date.getFullYear(), date.getMonth(), date.getDate())

  return Math.floor((current.getTime() - start.getTime()) / 86_400_000) + 1
}

const timezoneOffset = (date: Date): string => {
  const total = -date.getTimezoneOffset()
  const sign = total < 0 ? '-' : '+'
  const absolute = Math.abs(total)

  return `${sign}${pad(Math.floor(absolute / 60))}${pad(absolute % 60)}`
}

const timezoneName = (date: Date): string => {
  const part = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' })
    .formatToParts(date)
    .find(candidate => candidate.type === 'timeZoneName')

  return part?.value ?? ''
}

/**
 * Format a display-only timestamp using the shared Python ``strftime``-style
 * contract from ``display.timestamp_format``. The formatter intentionally
 * returns only the label; renderers own brackets, separators, and styling, so
 * timestamp text never enters model context or machine-readable payloads.
 */
export function formatDisplayTimestamp(value: Date | number | undefined, options: DisplayTimestampOptions): string {
  if (!options.enabled || value === undefined) {
    return ''
  }

  // Backend transcript timestamps are Unix seconds. Keep Date values as-is so
  // browser-native message dates remain unambiguous at the call site.
  const date = value instanceof Date ? value : new Date(value * 1000)

  if (Number.isNaN(date.getTime())) {
    return ''
  }

  const hour12 = date.getHours() % 12 || 12

  const replacements: Record<string, string> = {
    '%': '%',
    a: WEEKDAYS_SHORT[date.getDay()],
    A: WEEKDAYS_LONG[date.getDay()],
    b: MONTHS_SHORT[date.getMonth()],
    B: MONTHS_LONG[date.getMonth()],
    d: pad(date.getDate()),
    e: String(date.getDate()).padStart(2, ' '),
    f: pad(date.getMilliseconds() * 1000, 6),
    H: pad(date.getHours()),
    I: pad(hour12),
    j: pad(dayOfYear(date), 3),
    m: pad(date.getMonth() + 1),
    M: pad(date.getMinutes()),
    p: date.getHours() < 12 ? 'AM' : 'PM',
    S: pad(date.getSeconds()),
    w: String(date.getDay()),
    y: pad(date.getFullYear() % 100),
    Y: pad(date.getFullYear(), 4),
    z: timezoneOffset(date),
    Z: timezoneName(date)
  }

  return String(options.format || '%H:%M').replace(/%([%aAbBdefHIjmMpSwyYzZ])/g, (token, directive: string) =>
    Object.prototype.hasOwnProperty.call(replacements, directive) ? replacements[directive] : token
  )
}

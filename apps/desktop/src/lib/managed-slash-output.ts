import { sanitizeManagedBrandText } from '@/i18n/managed-brand'

const ANSI_ESCAPE_RE = new RegExp(String.raw`\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])`, 'g')

const BILLING_SECTION_START_RE =
  /^(?:📈\s*)?(?:\*\*)?(?:account limits|nous credits)(?:\*\*)?$|^plan:\s|^billing(?:\s|:)|^subscription(?:\s|:)/i

const BILLING_LINE_RE =
  /\/(?:subscription|topup|upgrade)\b|\bbilling\b|\bportal\b|\bcredits?\b|\btop[ -]?up\b|\bbalance\b|\bspendable\b/i

const MANAGED_USAGE_EMPTY = 'No session token usage is available yet.'

function comparableLine(line: string): string {
  return line.replace(ANSI_ESCAPE_RE, '').replace(/[*_`]/g, '').trim()
}

function sanitizeManagedUsageOutput(output: string): string {
  const kept: string[] = []
  let droppingAccountBlock = false

  for (const line of output.split('\n')) {
    const comparable = comparableLine(line)

    if (BILLING_SECTION_START_RE.test(comparable)) {
      droppingAccountBlock = true
    }

    if (droppingAccountBlock || BILLING_LINE_RE.test(comparable)) {
      continue
    }

    kept.push(line)
  }

  const sanitized = sanitizeManagedBrandText(kept.join('\n'))
    .replace(/\bhermes\b/gi, 'evaOS Agent')
    .replace(/\bnous\b/gi, 'Electric Sheep')
    .replace(/\n{3,}/g, '\n\n')
    .trim()

  return sanitized || MANAGED_USAGE_EMPTY
}

/**
 * Desktop-only boundary for backend-rendered slash output.
 *
 * Upstream and unmanaged output is byte-for-byte unchanged. Managed builds
 * relabel `/version` and retain only the non-account portion of `/usage`;
 * billing data and calls to action remain owned by Electric Sheep.
 */
export function sanitizeDesktopSlashOutput(command: string, output: string, managed: boolean): string {
  if (!managed) {
    return output
  }

  const normalized = command.trim().replace(/^\/+/, '').toLowerCase()

  if (normalized === 'version') {
    return sanitizeManagedBrandText(output)
  }

  if (normalized === 'usage') {
    return sanitizeManagedUsageOutput(output)
  }

  return output
}

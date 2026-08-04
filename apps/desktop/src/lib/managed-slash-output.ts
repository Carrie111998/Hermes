import { sanitizeManagedBrandText } from '@/i18n/managed-brand'

const ANSI_ESCAPE_RE = new RegExp(String.raw`\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])`, 'g')

const NOUS_BILLING_RE =
  /\bnous credits?\b|^\s*provider:\s*nous\b|portal\.nousresearch\.com|\/(?:subscription|topup|upgrade)\b/i

const MANAGED_USAGE_EMPTY = 'No session token usage is available yet.'

function comparableLine(line: string): string {
  return line.replace(ANSI_ESCAPE_RE, '').replace(/[*_`]/g, '').trim()
}

function isNousBillingSection(section: string): boolean {
  return section.split('\n').some(line => NOUS_BILLING_RE.test(comparableLine(line)))
}

function sanitizeManagedUsageProse(line: string): string {
  if (/^\s*model\s*:/i.test(comparableLine(line))) {
    return line
  }

  return line
    .replace(/Hermes Desktop/g, 'evaOS Agent')
    .replace(/Hermes Agent/g, 'evaOS Agent')
    .replace(/Nous Portal/g, 'Electric Sheep account')
    .replace(/Nous Research/g, 'Electric Sheep')
}

function sanitizeManagedUsageOutput(output: string): string {
  const sanitized = output
    .split(/\n{2,}/)
    .filter(section => !isNousBillingSection(section))
    .map(section => section.split('\n').map(sanitizeManagedUsageProse).join('\n'))
    .join('\n\n')
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

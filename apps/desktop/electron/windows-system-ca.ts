interface NodeTlsCaApi {
  getCACertificates(type?: 'default' | 'system'): string[]
  setDefaultCACertificates(certificates: string[]): void
}

type WindowsSystemCaSkipReason =
  /** Platform was not win32. */
  | 'not-applicable'
  /** Platform is win32 but the runtime returned zero system certificates. */
  | 'empty-store'
  /** Platform is win32 but loading the system store failed. */
  | 'tls-error'

interface WindowsSystemCaResult {
  applied: boolean
  systemCertificateCount: number
  totalCertificateCount: number
  /**
   * Why the helper did not apply the system CAs. Set on every non-applied
   * return path so callers can distinguish a no-op (off-Windows or empty
   * store) from a real failure when surfacing the outcome in logs.
   */
  reason?: WindowsSystemCaSkipReason
  error?: string
}

function installWindowsSystemCaTrust(tlsApi: NodeTlsCaApi, platform = process.platform): WindowsSystemCaResult {
  if (platform !== 'win32') {
    return {
      applied: false,
      systemCertificateCount: 0,
      totalCertificateCount: 0,
      reason: 'not-applicable'
    }
  }

  try {
    const defaultCertificates = tlsApi.getCACertificates('default')
    const systemCertificates = tlsApi.getCACertificates('system')

    if (systemCertificates.length === 0) {
      return {
        applied: false,
        systemCertificateCount: 0,
        totalCertificateCount: defaultCertificates.length,
        reason: 'empty-store'
      }
    }

    const certificates = [...defaultCertificates, ...systemCertificates]
    tlsApi.setDefaultCACertificates(certificates)

    return {
      applied: true,
      systemCertificateCount: systemCertificates.length,
      totalCertificateCount: certificates.length
    }
  } catch (error) {
    return {
      applied: false,
      systemCertificateCount: 0,
      totalCertificateCount: 0,
      reason: 'tls-error',
      error: error instanceof Error ? error.message : String(error)
    }
  }
}

export { installWindowsSystemCaTrust }
export type { NodeTlsCaApi, WindowsSystemCaResult, WindowsSystemCaSkipReason }

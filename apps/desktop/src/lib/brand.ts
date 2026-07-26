/**
 * brand.ts — the active desktop brand for the renderer.
 *
 * Mirror of electron/brand.ts: brand manifests are imported statically
 * and the active one is selected by `import.meta.env.VITE_DESKTOP_BRAND`,
 * which vite.config.ts defines from DESKTOP_BRAND at build time (default
 * ix-agency). Because the define is a compile-time constant, the other
 * brand's workspace bundle is dead-code-eliminated — an IX Agency build
 * contains no QuizVerse/Foundrly workspace and vice versa.
 */
import ixAgencyBrand from '../../brands/ix-agency.json'
import quizverseBrand from '../../brands/quizverse.json'
import foundrlyBrand from '../../brands/foundrly.json'

export interface DesktopBrand {
  id: string
  productName: string
  appId: string
  artifactPrefix: string
  executableName: string
  description: string
  author: string
  copyright: string
  homepage: string
  protocolScheme: string
  icon: string
  iconIco: string
  markSvg: string
  touchIcon: string
  updateFeedUrl: string
  /** CI-managed download landing page; '' → fall back to the raw artifact. */
  downloadPageUrl: string
  s3PublishPath: string
  workspace: 'ix-agency' | 'quizverse' | 'foundrly'
  workspaceLabel: string
  quizverse?: {
    deeptutorRemoteUrl: string
  }
  foundrly?: {
    webUrl: string
    adminPortalUrl: string
  }
}

export const DESKTOP_BRAND_ID: string = import.meta.env.VITE_DESKTOP_BRAND || 'ix-agency'

export const BRAND: DesktopBrand = (
  DESKTOP_BRAND_ID === 'quizverse'
    ? quizverseBrand
    : DESKTOP_BRAND_ID === 'foundrly'
      ? foundrlyBrand
      : ixAgencyBrand
) as DesktopBrand

export const BRAND_NAME = BRAND.productName

export const IS_IX_AGENCY_BRAND = DESKTOP_BRAND_ID === 'ix-agency'
export const IS_QUIZVERSE_BRAND = DESKTOP_BRAND_ID === 'quizverse'
export const IS_FOUNDRLY_BRAND = DESKTOP_BRAND_ID === 'foundrly'

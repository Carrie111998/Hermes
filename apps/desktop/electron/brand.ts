/**
 * brand.ts — the active desktop brand for the Electron main process.
 *
 * One repo ships strictly-separated branded desktop apps (IX Agency, QuizVerse,
 * Foundrly) from the same Hermes core. Manifests are imported statically and
 * the active one is selected by `process.env.HERMES_DESKTOP_BRAND`:
 *
 *  - packaged / `npm run build`: bundle-electron-main.mjs bakes the value in
 *    via an esbuild define, so the branch below is constant-folded and the
 *    bundle can never follow the wrong brand;
 *  - dev (`electron .` against the source tree): the plain env var applies,
 *    default ix-agency.
 *
 * Everything identity-shaped in the main process (app name, AppUserModelID,
 * update feed, protocol scheme, which brand IPC surface registers) must come
 * from BRAND — never a hardcoded product string.
 */
import ixAgencyBrand from '../brands/ix-agency.json' with { type: 'json' }
import quizverseBrand from '../brands/quizverse.json' with { type: 'json' }
import foundrlyBrand from '../brands/foundrly.json' with { type: 'json' }

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
  /** Favicon / tray / onboarding tile (under public/). */
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

// The gate booleans live in brand-gates.ts (an import-free module) so
// esbuild's cross-module constant propagation can inline them — see the DCE
// contract there. Re-exported here so consumers keep one import surface.
export { IS_IX_AGENCY_BRAND, IS_QUIZVERSE_BRAND, IS_FOUNDRLY_BRAND } from './brand-gates'

// Reads the env DIRECTLY (not via a variable or object lookup) so the esbuild
// define constant-folds the ternary and the inactive brand's manifest JSON is
// tree-shaken out of the bundle.
export const BRAND: DesktopBrand = (
  process.env.HERMES_DESKTOP_BRAND === 'quizverse'
    ? quizverseBrand
    : process.env.HERMES_DESKTOP_BRAND === 'foundrly'
      ? foundrlyBrand
      : ixAgencyBrand
) as DesktopBrand

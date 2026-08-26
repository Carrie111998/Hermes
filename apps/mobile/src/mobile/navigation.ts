export type MobileSetupView = 'connect' | 'login' | 'token'

/** The Android Back destination for setup screens; null means exit/minimize. */
export function mobileBackDestination(view: MobileSetupView): MobileSetupView | null {
  return view === 'login' || view === 'token' ? 'connect' : null
}

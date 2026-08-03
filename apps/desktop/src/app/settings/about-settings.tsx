import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { BrandMark } from '@/components/brand-mark'
import type { EvaManagedStatus } from '@/global'
import { $desktopVersion, refreshDesktopVersion } from '@/store/updates'

import { ListRow, SettingsContent } from './primitives'

export function AboutSettings() {
  const version = useStore($desktopVersion)
  const [managedStatus, setManagedStatus] = useState<EvaManagedStatus | null>(null)

  useEffect(() => {
    void refreshDesktopVersion()
    void window.hermesDesktop?.eva
      ?.status()
      .then(setManagedStatus)
      .catch(() => undefined)
  }, [])

  return (
    <SettingsContent>
      <div className="flex flex-col items-center gap-3 pt-6 pb-2 text-center">
        <BrandMark className="size-20 rounded-2xl" />
        <div>
          <h2 className="text-lg font-semibold tracking-tight">evaOS Agent</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {version?.appVersion ? `Version ${version.appVersion}` : 'Version unavailable'}
          </p>
        </div>
      </div>

      <div className="mx-auto mt-4 w-full max-w-2xl overflow-hidden rounded-xl border border-border/70">
        <ListRow
          description="Your account, assigned agent, access policy, and software updates are managed by Electric Sheep."
          title="Managed business beta"
        />
        <ListRow
          description={`${managedStatus?.updateChannel ?? 'managed'} · Signed updates from Electric Sheep`}
          title="Update channel"
        />
        <ListRow
          description="Built on Hermes Agent by Nous Research, used under the MIT License."
          title="Open-source attribution"
        />
        <ListRow
          description="Signed Apple Silicon managed business beta. This is not a public release."
          title="Distribution"
        />
      </div>
    </SettingsContent>
  )
}

import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'

import { formatQuotaChip, getAccountUsage } from '@/api/account-usage'
import { useI18n } from '@/i18n'
import { $activeGatewayProfile } from '@/store/profile'

import type { StatusbarItem } from '../statusbar-controls'

const POLL_MS = 5 * 60 * 1000
const EMPTY_POLL_MS = 30 * 60 * 1000

export function useAccountQuotaStatusbarItem(): StatusbarItem {
  const { statusbar: copy } = useI18n()
  const profile = useStore($activeGatewayProfile)
  const query = useQuery({
    queryKey: ['account-usage', profile],
    queryFn: () => getAccountUsage(profile),
    refetchInterval: current => (formatQuotaChip(current.state.data).label ? POLL_MS : EMPTY_POLL_MS),
    retry: false,
    staleTime: POLL_MS
  })
  const { label, tip } = formatQuotaChip(query.data)
  const refetch = query.refetch

  return useMemo<StatusbarItem>(
    () => ({
      hidden: !label,
      id: 'account-quota',
      label,
      onSelect: () => {
        void refetch()
      },
      title: tip || undefined,
      toggleLabel: copy.toggleQuota,
      variant: 'action'
    }),
    [copy.toggleQuota, label, refetch, tip]
  )
}

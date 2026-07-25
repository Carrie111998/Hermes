import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  decideGovernanceApproval,
  getGovernanceApprovals,
  getGovernanceConnectors,
  getGovernanceRules,
  revokeGovernanceRule,
  type GovernanceApproval
} from '@/hermes'
import { Clock, Link, Lock, RefreshCw } from '@/lib/icons'

import { ListRow, SettingsContent, SettingsSection, SettingsSkeleton } from './primitives'

function humanTime(value: number | null | undefined): string {
  if (!value) return 'No expiry'
  return new Date(value * 1000).toLocaleString()
}

function riskTone(risk: string): 'default' | 'muted' | 'warn' {
  if (risk === 'privileged' || risk === 'destructive') return 'warn'
  if (risk === 'external' || risk === 'exec') return 'default'
  return 'muted'
}

function ApprovalActions({ approval, busy, onDecision }: {
  approval: GovernanceApproval
  busy: boolean
  onDecision: (decision: 'allow-once' | 'allow-always' | 'deny') => void
}) {
  if (!approval.integrity_ok) {
    return <Badge variant="warn">Integrity failed</Badge>
  }
  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      <Button disabled={busy} onClick={() => onDecision('allow-once')} size="xs" variant="outline">Allow once</Button>
      <Button disabled={busy || !approval.target} onClick={() => onDecision('allow-always')} size="xs" variant="outline">Allow for target</Button>
      <Button className="text-destructive" disabled={busy} onClick={() => onDecision('deny')} size="xs" variant="ghost">Deny</Button>
    </div>
  )
}

export function GovernanceSettings() {
  const queryClient = useQueryClient()
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const approvals = useQuery({ queryKey: ['governance', 'approvals'], queryFn: getGovernanceApprovals, refetchInterval: 10_000 })
  const rules = useQuery({ queryKey: ['governance', 'rules'], queryFn: getGovernanceRules })
  const connectors = useQuery({ queryKey: ['governance', 'connectors'], queryFn: getGovernanceConnectors })

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['governance'] })
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'allow-once' | 'allow-always' | 'deny' }) =>
      decideGovernanceApproval(id, decision),
    onMutate: ({ id }) => { setBusyId(id); setError(null) },
    onError: err => setError(err instanceof Error ? err.message : String(err)),
    onSettled: () => { setBusyId(null); refresh() }
  })
  const revoke = useMutation({
    mutationFn: revokeGovernanceRule,
    onMutate: id => { setBusyId(id); setError(null) },
    onError: err => setError(err instanceof Error ? err.message : String(err)),
    onSettled: () => { setBusyId(null); refresh() }
  })

  if (approvals.isPending || rules.isPending || connectors.isPending) {
    return <SettingsSkeleton sections={[{ rows: 2 }, { rows: 2 }, { rows: 3 }]} />
  }

  return (
    <SettingsContent>
      <div className="flex items-center justify-between py-4">
        <div>
          <h2 className="text-base font-semibold">Governance</h2>
          <p className="mt-1 text-xs text-muted-foreground">Durable approvals, bounded authority, and connector health.</p>
        </div>
        <Button aria-label="Refresh governance" onClick={refresh} size="sm" variant="ghost"><RefreshCw className="size-4" /></Button>
      </div>

      {error && <div className="mb-4 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{error}</div>}

      <SettingsSection icon={Clock} meta={String(approvals.data?.count ?? 0)} title="Approval inbox">
        {(approvals.data?.approvals ?? []).length === 0 ? (
          <p className="py-3 text-sm text-muted-foreground">No actions are waiting for approval.</p>
        ) : approvals.data?.approvals.map(approval => (
          <ListRow
            action={<ApprovalActions approval={approval} busy={busyId === approval.id} onDecision={decision => decide.mutate({ id: approval.id, decision })} />}
            description={<><span>{approval.reason}</span><span className="mt-1 block font-mono text-[0.68rem]">{approval.target || 'No target'} · expires {humanTime(approval.expires_at)}</span></>}
            key={approval.id}
            title={<span className="flex items-center gap-2"><span>{approval.tool_name}</span><Badge variant={riskTone(approval.risk_class)}>{approval.risk_class}</Badge></span>}
          />
        ))}
      </SettingsSection>

      <SettingsSection icon={Lock} meta={String(rules.data?.count ?? 0)} title="Standing approvals">
        {(rules.data?.rules ?? []).length === 0 ? (
          <p className="py-3 text-sm text-muted-foreground">No standing approvals. Create one only from an exact pending target.</p>
        ) : rules.data?.rules.map(rule => (
          <ListRow
            action={<Button className="text-destructive" disabled={busyId === rule.id} onClick={() => revoke.mutate(rule.id)} size="xs" variant="ghost">Revoke</Button>}
            description={`${rule.target_pattern} · ${rule.use_count}${rule.max_uses == null ? '' : `/${rule.max_uses}`} uses · ${humanTime(rule.expires_at)}`}
            key={rule.id}
            title={<span className="flex items-center gap-2"><span>{rule.tool_name}</span><Badge variant={riskTone(rule.risk_ceiling)}>{rule.risk_ceiling}</Badge></span>}
          />
        ))}
      </SettingsSection>

      <SettingsSection icon={Link} meta={String(connectors.data?.count ?? 0)} title="Connectors">
        {(connectors.data?.connectors ?? []).map(connector => (
          <ListRow
            action={<Badge variant={connector.health === 'healthy' ? 'default' : 'muted'}>{connector.health}</Badge>}
            description={`${connector.available_tool_count ?? connector.tool_count}/${connector.tool_count} tools available · ${connector.risk_classes.join(', ') || 'unclassified'}`}
            key={connector.id}
            title={connector.id}
          />
        ))}
      </SettingsSection>
    </SettingsContent>
  )
}

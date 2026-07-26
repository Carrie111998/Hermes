import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  ExternalLink,
  Link2,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  ShieldX,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";

import { api } from "@/lib/api";
import type {
  GovernanceApproval,
  GovernanceConnector,
  GovernanceDecision,
  GovernanceRule,
} from "@/lib/api";

const REFRESH_INTERVAL_MS = 10_000;

type BadgeTone =
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";

function humanTime(value: number | null | undefined): string {
  if (!value) return "No expiry";
  return new Date(value * 1000).toLocaleString();
}

function riskTone(risk: string | null | undefined): BadgeTone {
  if (risk === "destructive") return "destructive";
  if (risk === "privileged" || risk === "exec") return "warning";
  if (risk === "external") return "outline";
  return "secondary";
}

function healthTone(health: string): BadgeTone {
  if (health === "healthy") return "success";
  if (health === "degraded") return "destructive";
  if (health === "disabled") return "secondary";
  return "outline";
}

function EmptyState({ children }: { children: string }) {
  return (
    <div className="px-4 py-8 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function LoadingCard({ title }: { title: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="flex min-h-32 items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner /> Loading…
      </CardContent>
    </Card>
  );
}

function ApprovalRow({
  approval,
  busy,
  onDecision,
}: {
  approval: GovernanceApproval;
  busy: boolean;
  onDecision: (decision: GovernanceDecision) => void;
}) {
  return (
    <article className="flex flex-col gap-3 px-4 py-4">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{approval.tool_name}</span>
          <Badge tone={riskTone(approval.risk_class)}>{approval.risk_class}</Badge>
          {!approval.integrity_ok && (
            <Badge tone="destructive">
              <ShieldX className="mr-1 size-3" /> Integrity failed
            </Badge>
          )}
        </div>
        <p className="text-sm text-muted-foreground">
          {approval.reason || "This action requires operator approval."}
        </p>
        <code
          className="block max-w-full break-all border border-border bg-background/50 px-2 py-1 text-xs text-foreground"
          title={approval.target || "No target"}
        >
          {approval.target || "No target"}
        </code>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>Source: {approval.source || "tool"}</span>
          <span>Created: {humanTime(approval.created_at)}</span>
          <span>Expires: {humanTime(approval.expires_at)}</span>
        </div>
      </div>

      <div className="flex shrink-0 flex-wrap gap-2 lg:justify-end">
        {approval.integrity_ok ? (
          <>
            <Button
              size="sm"
              outlined
              disabled={busy}
              onClick={() => onDecision("allow-once")}
            >
              Allow once
            </Button>
            <Button
              size="sm"
              outlined
              disabled={busy || !approval.target}
              title={
                approval.target
                  ? "Create an exact-target rule, bounded to 30 days and 100 uses"
                  : "Targetless requests cannot become standing approvals"
              }
              onClick={() => onDecision("allow-always")}
            >
              Allow for target
            </Button>
            <Button
              size="sm"
              ghost
              className="text-destructive hover:text-destructive"
              disabled={busy}
              onClick={() => onDecision("deny")}
            >
              Deny
            </Button>
          </>
        ) : (
          <span className="text-xs text-destructive">
            Decisions are blocked because the stored envelope failed verification.
          </span>
        )}
      </div>
    </article>
  );
}

function RuleRow({
  rule,
  busy,
  onRevoke,
}: {
  rule: GovernanceRule;
  busy: boolean;
  onRevoke: () => void;
}) {
  const useLabel =
    rule.max_uses == null
      ? `${rule.use_count} uses`
      : `${rule.use_count}/${rule.max_uses} uses`;

  return (
    <article className="flex flex-col gap-3 px-4 py-4 md:flex-row md:items-start md:justify-between">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{rule.tool_name}</span>
          <Badge tone={riskTone(rule.risk_ceiling)}>{rule.risk_ceiling}</Badge>
          <Badge tone="outline">exact target</Badge>
        </div>
        <code
          className="block max-w-full break-all border border-border bg-background/50 px-2 py-1 text-xs text-foreground"
          title={rule.target_pattern}
        >
          {rule.target_pattern}
        </code>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>{useLabel}</span>
          <span>Expires: {humanTime(rule.expires_at)}</span>
          {rule.last_used_at ? (
            <span>Last used: {humanTime(rule.last_used_at)}</span>
          ) : null}
        </div>
      </div>
      <Button
        size="sm"
        ghost
        className="shrink-0 text-destructive hover:text-destructive"
        disabled={busy}
        onClick={onRevoke}
      >
        Revoke
      </Button>
    </article>
  );
}

function ConnectorRow({ connector }: { connector: GovernanceConnector }) {
  const available = connector.available_tool_count ?? connector.tool_count;
  return (
    <article className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{connector.id}</span>
          <Badge tone={healthTone(connector.health)}>{connector.health}</Badge>
          {!connector.enabled && <Badge tone="secondary">disabled</Badge>}
        </div>
        <p className="text-sm text-muted-foreground">
          {available}/{connector.tool_count} tools available
          {connector.risk_classes.length
            ? ` · ${connector.risk_classes.join(", ")}`
            : " · unclassified"}
        </p>
        {connector.tools.length > 0 && (
          <p className="break-words font-mono text-xs text-muted-foreground">
            {connector.tools.join(", ")}
          </p>
        )}
      </div>
      {connector.highest_risk ? (
        <Badge tone={riskTone(connector.highest_risk)}>
          highest: {connector.highest_risk}
        </Badge>
      ) : null}
    </article>
  );
}

export default function GovernancePage() {
  const navigate = useNavigate();
  const [approvals, setApprovals] = useState<GovernanceApproval[]>([]);
  const [rules, setRules] = useState<GovernanceRule[]>([]);
  const [connectors, setConnectors] = useState<GovernanceConnector[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { toast, showToast } = useToast();

  const loadGovernance = useCallback(async () => {
    const results = await Promise.allSettled([
      api.getGovernanceApprovals(),
      api.getGovernanceRules(),
      api.getGovernanceConnectors(),
    ]);
    const failures: string[] = [];

    if (results[0].status === "fulfilled") {
      setApprovals(results[0].value.approvals);
    } else failures.push("approvals");
    if (results[1].status === "fulfilled") {
      setRules(results[1].value.rules);
    } else failures.push("standing approvals");
    if (results[2].status === "fulfilled") {
      setConnectors(results[2].value.connectors);
    } else failures.push("connectors");

    setLoadError(
      failures.length
        ? `Could not refresh ${failures.join(", ")}. Previously loaded data is retained.`
        : null,
    );
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    const initialTimer = window.setTimeout(() => void loadGovernance(), 0);
    const refreshTimer = window.setInterval(
      () => void loadGovernance(),
      REFRESH_INTERVAL_MS,
    );
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(refreshTimer);
    };
  }, [loadGovernance]);

  const decide = async (
    approval: GovernanceApproval,
    decision: GovernanceDecision,
  ) => {
    setBusyId(approval.id);
    try {
      await api.decideGovernanceApproval(approval.id, decision);
      showToast(
        decision === "deny"
          ? "Approval denied"
          : decision === "allow-always"
            ? "Approved and bounded exact-target rule created"
            : "Approved once",
        "success",
      );
      await loadGovernance();
    } catch (error) {
      showToast(`Decision failed: ${String(error)}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  const revoke = async (rule: GovernanceRule) => {
    setBusyId(rule.id);
    try {
      const result = await api.revokeGovernanceRule(rule.id);
      if (!result.revoked) throw new Error("Rule was already inactive or not found");
      showToast("Standing approval revoked", "success");
      await loadGovernance();
    } catch (error) {
      showToast(`Revoke failed: ${String(error)}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  const degradedConnectors = connectors.filter(
    (connector) => connector.health === "degraded",
  ).length;

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <Card className="border-border">
        <CardContent className="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" />
            <div>
              <h2 className="font-medium">Governed authority</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Review durable requests, manage exact-target standing approvals, and
                inspect connector health. Standing approvals created here expire after
                30 days or 100 uses.
              </p>
            </div>
          </div>
          <Button
            size="sm"
            outlined
            className="shrink-0"
            disabled={refreshing}
            prefix={refreshing ? <Spinner /> : <RefreshCw />}
            onClick={() => {
              setRefreshing(true);
              void loadGovernance();
            }}
          >
            Refresh
          </Button>
        </CardContent>
      </Card>

      {loadError && (
        <div className="flex items-start gap-2 border border-warning/50 bg-warning/10 px-4 py-3 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
          <span>{loadError}</span>
        </div>
      )}

      {!loading && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Card>
            <CardContent className="flex items-center justify-between p-4">
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Pending approvals
                </p>
                <p className="mt-1 text-2xl font-semibold">{approvals.length}</p>
              </div>
              <Clock3 className="size-5 text-muted-foreground" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex items-center justify-between p-4">
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Active rules
                </p>
                <p className="mt-1 text-2xl font-semibold">{rules.length}</p>
              </div>
              <LockKeyhole className="size-5 text-muted-foreground" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="flex items-center justify-between p-4">
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Degraded connectors
                </p>
                <p className="mt-1 text-2xl font-semibold">{degradedConnectors}</p>
              </div>
              {degradedConnectors ? (
                <AlertTriangle className="size-5 text-warning" />
              ) : (
                <CheckCircle2 className="size-5 text-success" />
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {loading ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <LoadingCard title="Approval inbox" />
          <LoadingCard title="Standing approvals" />
          <div className="xl:col-span-2">
            <LoadingCard title="Connector health" />
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card className="min-w-0">
            <CardHeader className="flex-row items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Clock3 className="size-4 text-muted-foreground" />
                <CardTitle className="text-base">Approval inbox</CardTitle>
              </div>
              <Badge tone={approvals.length ? "warning" : "secondary"}>
                {approvals.length}
              </Badge>
            </CardHeader>
            <CardContent className="p-0">
              {approvals.length === 0 ? (
                <EmptyState>No actions are waiting for approval.</EmptyState>
              ) : (
                <div className="max-h-[400px] divide-y divide-border overflow-y-auto">
                  {approvals.map((approval) => (
                    <ApprovalRow
                      key={approval.id}
                      approval={approval}
                      busy={busyId === approval.id}
                      onDecision={(decision) => void decide(approval, decision)}
                    />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="min-w-0">
            <CardHeader className="flex-row items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <LockKeyhole className="size-4 text-muted-foreground" />
                <CardTitle className="text-base">Standing approvals</CardTitle>
              </div>
              <Badge tone="secondary">{rules.length}</Badge>
            </CardHeader>
            <CardContent className="p-0">
              {rules.length === 0 ? (
                <EmptyState>
                  No standing approvals. Create one only from an exact pending target.
                </EmptyState>
              ) : (
                <div className="max-h-[400px] divide-y divide-border overflow-y-auto">
                  {rules.map((rule) => (
                    <RuleRow
                      key={rule.id}
                      rule={rule}
                      busy={busyId === rule.id}
                      onRevoke={() => void revoke(rule)}
                    />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="min-w-0 xl:col-span-2">
            <CardHeader className="flex-row items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Link2 className="size-4 text-muted-foreground" />
                <CardTitle className="text-base">Connector health</CardTitle>
                <Badge tone="secondary">{connectors.length}</Badge>
              </div>
              <Button size="sm" ghost onClick={() => navigate("/mcp")}>
                Manage MCP <ExternalLink className="ml-1 size-3" />
              </Button>
            </CardHeader>
            <CardContent className="p-0">
              {connectors.length === 0 ? (
                <EmptyState>No connectors are registered for this profile.</EmptyState>
              ) : (
                <div className="max-h-[400px] divide-y divide-border overflow-y-auto">
                  {connectors.map((connector) => (
                    <ConnectorRow key={connector.id} connector={connector} />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

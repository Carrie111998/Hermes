import { useEffect, useMemo, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { AlertTriangle, Network, ShieldCheck } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Switch } from "@nous-research/ui/ui/components/switch";

import { useI18n } from "@/i18n";
import { api, type ChannelCapability, type ChannelMcpPolicy } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  profile?: string;
  query: string;
  onError: (message: string) => void;
  onSaved: (message: string) => void;
}

const HIGH_IMPACT = new Set([
  "terminal",
  "file",
  "code_execution",
  "computer_use",
  "delegation",
  "cronjob",
]);

export function ChannelCapabilitiesPanel({ profile, query, onError, onSaved }: Props) {
  const { t } = useI18n();
  const [channels, setChannels] = useState<ChannelCapability[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toolsets, setToolsets] = useState<Set<string>>(new Set());
  const [mcpMode, setMcpMode] = useState<ChannelMcpPolicy["mode"]>("all");
  const [mcpServers, setMcpServers] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getChannelCapabilities(profile)
      .then((rows) => {
        if (cancelled) return;
        setChannels(rows);
        setSelected(rows[0]?.platform ?? null);
      })
      .catch(() => !cancelled && onError(t.skills.channelCapabilitiesFailed))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [onError, profile, t.skills.channelCapabilitiesFailed]);

  const channel = useMemo(
    () => channels.find((row) => row.platform === selected) ?? null,
    [channels, selected],
  );

  useEffect(() => {
    if (!channel) return;
    setToolsets(
      new Set(channel.toolsets.filter((row) => row.enabled).map((row) => row.name)),
    );
    setMcpMode(channel.mcp.mode);
    setMcpServers(new Set(channel.mcp.selected));
  }, [channel]);

  const visibleChannels = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return channels;
    return channels.filter(
      (row) =>
        row.label.toLowerCase().includes(needle) ||
        row.platform.toLowerCase().includes(needle) ||
        row.toolsets.some(
          (item) =>
            item.label.toLowerCase().includes(needle) ||
            item.name.toLowerCase().includes(needle) ||
            item.tools.some((tool) => tool.toLowerCase().includes(needle)),
        ),
    );
  }, [channels, query]);

  const updateSet = (
    setter: Dispatch<SetStateAction<Set<string>>>,
    name: string,
    enabled: boolean,
  ) =>
    setter((current) => {
      const next = new Set(current);
      if (enabled) next.add(name);
      else next.delete(name);
      return next;
    });

  const save = async () => {
    if (!channel || saving) return;
    setSaving(true);
    try {
      const result = await api.updateChannelCapabilities(
        channel.platform,
        {
          toolsets: [...toolsets].sort(),
          mcp_mode: mcpMode,
          mcp_servers: mcpMode === "allowlist" ? [...mcpServers].sort() : [],
        },
        profile,
      );
      setChannels((rows) =>
        rows.map((row) =>
          row.platform === result.channel.platform ? result.channel : row,
        ),
      );
      onSaved(t.skills.channelCapabilitiesSaved);
    } catch {
      onError(t.skills.channelCapabilitiesFailed);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Card className="rounded-none">
        <CardContent className="py-12 text-center text-sm text-muted-foreground">
          {t.common.loading}
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid min-h-[560px] gap-3 lg:grid-cols-[240px_minmax(0,1fr)]">
      <Card className="rounded-none">
        <CardHeader className="px-3 py-3">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Network className="h-4 w-4" />
            {t.skills.channels}
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-1 px-2 pb-3">
          {visibleChannels.map((row) => (
            <button
              key={row.platform}
              type="button"
              className={cn(
                "flex w-full items-center gap-2 px-2 py-2 text-left text-sm",
                selected === row.platform
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
              )}
              onClick={() => setSelected(row.platform)}
            >
              <span className="min-w-0 flex-1 truncate">{row.label}</span>
            </button>
          ))}
        </CardContent>
      </Card>

      {channel && (
        <Card className="rounded-none">
          <CardHeader className="px-4 py-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <CardTitle className="flex items-center gap-2 text-base">
                  <ShieldCheck className="h-4 w-4" />
                  {channel.label}
                </CardTitle>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t.skills.channelCapabilitiesDescription}
                </p>
              </div>
              <Badge tone="secondary">
                {channel.explicit
                  ? t.skills.customBoundary
                  : t.skills.inheritedDefaults}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="grid gap-6 px-4 pb-5">
            <section>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-medium">{t.skills.abilitiesEnabled}</h3>
                <span className="text-xs text-muted-foreground">
                  {toolsets.size}/{channel.toolsets.length}
                </span>
              </div>
              <div className="grid gap-x-5 gap-y-1 sm:grid-cols-2">
                {channel.toolsets.map((item) => (
                  <div key={item.name} className="flex items-start gap-3 py-2.5">
                    <Switch
                      checked={toolsets.has(item.name)}
                      onCheckedChange={(checked) =>
                        updateSet(setToolsets, item.name, checked)
                      }
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-sm font-medium">{item.label}</span>
                        {HIGH_IMPACT.has(item.name) && (
                          <Badge tone="warning" className="text-[10px]">
                            <AlertTriangle className="mr-1 h-3 w-3" />
                            {t.skills.highImpact}
                          </Badge>
                        )}
                      </div>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {item.description}
                      </p>
                      {item.tools.length > 0 && (
                        <p className="mt-1 truncate font-mono text-[10px] text-text-tertiary">
                          {item.tools.join(", ")}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </section>

            {channel.implicit_toolsets.length > 0 && (
              <section>
                <h3 className="mb-2 text-sm font-medium">
                  {t.skills.requiredAbilities}
                </h3>
                <p className="mb-2 text-xs text-muted-foreground">
                  {t.skills.requiredAbilitiesDescription}
                </p>
                <div className="flex flex-wrap gap-2">
                  {channel.implicit_toolsets.map((item) => (
                    <Badge key={item.name} tone="secondary">
                      {item.label}
                    </Badge>
                  ))}
                </div>
              </section>
            )}

            <section>
              <h3 className="mb-2 text-sm font-medium">{t.skills.mcpAccess}</h3>
              <div className="flex flex-wrap gap-2">
                {(["all", "none", "allowlist"] as const).map((mode) => (
                  <Button
                    key={mode}
                    size="sm"
                    outlined={mcpMode !== mode}
                    onClick={() => setMcpMode(mode)}
                  >
                    {mode === "all"
                      ? t.skills.mcpAll
                      : mode === "none"
                        ? t.skills.mcpNone
                        : t.skills.mcpSelected}
                  </Button>
                ))}
              </div>
              {mcpMode === "allowlist" && (
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {channel.mcp.available.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      {t.skills.noMcpServers}
                    </p>
                  ) : (
                    channel.mcp.available.map((server) => (
                      <label key={server} className="flex items-center gap-2 text-sm">
                        <Switch
                          checked={mcpServers.has(server)}
                          onCheckedChange={(checked) =>
                            updateSet(setMcpServers, server, checked)
                          }
                        />
                        <span className="font-mono text-xs">{server}</span>
                      </label>
                    ))
                  )}
                </div>
              )}
            </section>

            <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
              <p className="text-xs text-muted-foreground">
                {t.skills.changesNewSessions}
              </p>
              <Button onClick={() => void save()} disabled={saving}>
                {saving ? t.skills.savingCapabilities : t.skills.saveCapabilities}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

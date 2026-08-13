import { useCallback, useEffect, useMemo, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import type { HermesGateway } from '@/hermes'
import { isDesktopToolsetVisible } from '@/lib/desktop-toolsets'
import { ChevronDown, ChevronRight, Globe, Loader2, Lock, Plus, Save, SlidersHorizontal, Trash2, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { activeGateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'

import { EmptyState, Pill, SectionHeading, SettingsContent } from './primitives'

// The `tools.catalog` / preset RPC response shapes are declared on the shared
// gateway client but not re-exported from `@hermes/shared`; derive them from the
// method signatures so this panel stays self-contained (no cross-package edit).
type ToolCatalog = Awaited<ReturnType<HermesGateway['toolsCatalog']>>
type ToolCatalogToolset = ToolCatalog['toolsets'][number]
type ToolCatalogMcpServer = ToolCatalog['mcp_servers'][number]
type ToolPreset = Parameters<HermesGateway['toolsPresetSave']>[0] & { builtin?: boolean }

/**
 * Working copy of a preset while it is being edited. `useDefault` is the
 * tri-state boundary the contract cares about:
 *   - `useDefault: true`  → `enabled_toolsets: null` = profile default ("Full").
 *   - `useDefault: false` → `enabled_toolsets` is the EXPLICIT list, and `[]`
 *     (chat-only) is meaningful — it is never coerced back to null.
 */
interface Draft {
  name: string
  useDefault: boolean
  enabledToolsets: string[]
  disabledTools: string[]
  allowedTools: string[]
  disabledSkills: string[]
}

const RESERVED_NAMES = new Set(['Chat-only', 'Full'])

// Radix Select forbids an empty-string item value (it's the placeholder
// sentinel), so "no default" maps to this token in the control and back to
// null on the wire.
const NO_DEFAULT = '__none__'

function formatTokens(n: number): string {
  if (n < 1000) {
    return `~${Math.round(n)}`
  }

  return `~${(n / 1000).toFixed(1)}k`
}

function TokenBadge({ tokens }: { tokens: number }) {
  return (
    <Badge className="font-mono" size="xs" variant="outline">
      {formatTokens(tokens)}
    </Badge>
  )
}

function toggleMembership(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

function fromPreset(preset: ToolPreset): Draft {
  const useDefault = preset.enabled_toolsets == null

  return {
    name: preset.name,
    useDefault,
    enabledToolsets: preset.enabled_toolsets ?? [],
    disabledTools: preset.disabled_tools ?? [],
    allowedTools: preset.allowed_tools ?? [],
    disabledSkills: preset.disabled_skills ?? []
  }
}

/**
 * Serialize the draft into the preset-save payload. `enabled_toolsets` keeps the
 * `[]` vs `null` distinction; `allowed_tools` maps an empty selection to `null`
 * (no whitelist) so the UI never accidentally emits a "whitelist to nothing"
 * list that would strip every non-core tool.
 */
function toPreset(draft: Draft): ToolPreset {
  return {
    name: draft.name.trim(),
    enabled_toolsets: draft.useDefault ? null : draft.enabledToolsets,
    disabled_tools: draft.disabledTools,
    allowed_tools: draft.allowedTools.length > 0 ? draft.allowedTools : null,
    disabled_skills: draft.disabledSkills
  }
}

function normalize(preset: ToolPreset): string {
  const sorted = (list: string[] | null | undefined) => (list ? [...list].sort() : null)

  return JSON.stringify({
    enabled_toolsets: preset.enabled_toolsets == null ? null : [...preset.enabled_toolsets].sort(),
    disabled_tools: sorted(preset.disabled_tools),
    allowed_tools: sorted(preset.allowed_tools),
    disabled_skills: sorted(preset.disabled_skills)
  })
}

function isToolsetEnabled(draft: Draft, toolset: string): boolean {
  return draft.useDefault || draft.enabledToolsets.includes(toolset)
}

// A tool is on when its toolset is enabled and it is not blacklisted, OR it is
// individually whitelisted while its toolset is off. Mirrors the backend's
// allowed/denied precedence.
function isToolOn(draft: Draft, toolset: string, tool: string): boolean {
  if (isToolsetEnabled(draft, toolset)) {
    return !draft.disabledTools.includes(tool)
  }

  return draft.allowedTools.includes(tool)
}

function computeTotal(catalog: ToolCatalog, draft: Draft): number {
  let total = catalog.core_tokens

  for (const toolset of catalog.toolsets) {
    for (const tool of toolset.tools) {
      if (isToolOn(draft, toolset.name, tool.name)) {
        total += tool.est_tokens
      }
    }
  }

  for (const server of catalog.mcp_servers) {
    for (const tool of server.tools) {
      if (isToolOn(draft, server.toolset, tool.name)) {
        total += tool.est_tokens
      }
    }
  }

  for (const skill of catalog.skills) {
    if (!draft.disabledSkills.includes(skill.name)) {
      total += skill.est_tokens
    }
  }

  return total
}

function materializeAll(catalog: ToolCatalog): string[] {
  return [...catalog.toolsets.map(t => t.name), ...catalog.mcp_servers.map(s => s.toolset)]
}

interface GroupRowProps {
  name: string
  description?: string
  tools: { name: string; est_tokens: number }[]
  toolset: string
  est_tokens: number
  draft: Draft
  /** Toolset-level checkbox is disabled while the preset uses the profile default. */
  toolsetLocked: boolean
  onToggleGroup: () => void
  onToggleTool: (tool: string) => void
}

function GroupRow({
  name,
  description,
  tools,
  toolset,
  est_tokens,
  draft,
  toolsetLocked,
  onToggleGroup,
  onToggleTool
}: GroupRowProps) {
  const [expanded, setExpanded] = useState(false)
  const enabled = isToolsetEnabled(draft, toolset)
  const onCount = tools.filter(tool => isToolOn(draft, toolset, tool.name)).length

  return (
    <div className="overflow-hidden rounded-lg bg-background/55">
      <div className="flex items-center gap-2 px-2.5 py-2">
        <button
          aria-label={expanded ? 'Collapse' : 'Expand'}
          className="text-muted-foreground transition hover:text-foreground"
          onClick={() => setExpanded(v => !v)}
          type="button"
        >
          {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </button>
        <Checkbox
          aria-label={name}
          checked={enabled}
          disabled={toolsetLocked}
          onCheckedChange={() => onToggleGroup()}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{name}</div>
          {description && <div className="truncate text-[0.7rem] text-muted-foreground">{description}</div>}
        </div>
        <span className="shrink-0 text-[0.68rem] tabular-nums text-muted-foreground">
          {onCount}/{tools.length}
        </span>
        <TokenBadge tokens={est_tokens} />
      </div>

      {expanded && (
        <div className="grid gap-1 border-t border-(--ui-stroke-tertiary) py-2 pr-2.5 pl-9">
          {tools.length === 0 ? (
            <span className="text-[0.7rem] text-muted-foreground">No individual tools reported.</span>
          ) : (
            tools.map(tool => (
              <label className="flex cursor-pointer items-center gap-2" key={tool.name}>
                <Checkbox
                  aria-label={tool.name}
                  checked={isToolOn(draft, toolset, tool.name)}
                  onCheckedChange={() => onToggleTool(tool.name)}
                />
                <span className="min-w-0 flex-1 truncate font-mono text-[0.72rem]">{tool.name}</span>
                <TokenBadge tokens={tool.est_tokens} />
              </label>
            ))
          )}
        </div>
      )}
    </div>
  )
}

export function ToolPresetsSettings() {
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null)
  const [presets, setPresets] = useState<ToolPreset[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedName, setSelectedName] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  // Profile-scoped default preset every NEW chat starts with (null = none →
  // platform/coding posture). Persisted via tools.default_preset_get/set.
  const [defaultPreset, setDefaultPreset] = useState<string | null>(null)
  const [savingDefault, setSavingDefault] = useState(false)

  const load = useCallback(async () => {
    const gateway = activeGateway()

    if (!gateway) {
      setLoading(false)

      return
    }

    setLoading(true)

    try {
      const [cat, list, def] = await Promise.all([
        gateway.toolsCatalog(),
        gateway.toolsPresetsList(),
        gateway.toolsDefaultPresetGet()
      ])

      setCatalog({
        ...cat,
        toolsets: cat.toolsets.filter(t => isDesktopToolsetVisible(t.name))
      })
      setPresets(list.presets)
      setDefaultPreset(def.name)
    } catch (err) {
      notifyError(err, 'Could not load tool presets')
    } finally {
      setLoading(false)
    }
  }, [])

  const changeDefaultPreset = useCallback(async (value: string) => {
    const gateway = activeGateway()

    if (!gateway) {
      return
    }

    const name = value === NO_DEFAULT ? null : value
    setSavingDefault(true)

    try {
      const result = await gateway.toolsDefaultPresetSet(name)
      setDefaultPreset(result.name)
      notify({
        kind: 'success',
        title: 'Default updated',
        message: result.name ? `New chats start with "${result.name}"` : 'New chats use the platform default'
      })
    } catch (err) {
      notifyError(err, 'Could not update default preset')
    } finally {
      setSavingDefault(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Keep a selection once presets are known; prefer the first user preset so the
  // editor opens on something editable rather than a read-only built-in.
  useEffect(() => {
    if (presets.length === 0) {
      setSelectedName(null)

      return
    }

    if (!selectedName || !presets.some(p => p.name === selectedName)) {
      const firstUser = presets.find(p => !p.builtin)
      setSelectedName((firstUser ?? presets[0]).name)
    }
  }, [presets, selectedName])

  const selected = useMemo(() => presets.find(p => p.name === selectedName) ?? null, [presets, selectedName])

  // Reset the working draft whenever the selected preset changes identity.
  useEffect(() => {
    setDraft(selected ? fromPreset(selected) : null)
  }, [selected])

  // Built-ins (Chat-only / Full) keep a fixed identity name and reset (rather
  // than delete) — but their tool/skill selection is fully editable like any
  // other preset. Nothing about the selection is locked; it's up to the user.
  const isBuiltin = Boolean(selected?.builtin)

  const total = useMemo(() => {
    if (!catalog || !draft) {
      return 0
    }

    return computeTotal(catalog, draft)
  }, [catalog, draft])

  // Context cost of just the currently-enabled skills (skills index footprint).
  const skillsTotal = useMemo(() => {
    if (!catalog || !draft) {
      return 0
    }

    return catalog.skills.reduce(
      (sum, skill) => (draft.disabledSkills.includes(skill.name) ? sum : sum + skill.est_tokens),
      0
    )
  }, [catalog, draft])

  const isDirty = useMemo(() => {
    if (!draft || !selected) {
      return false
    }

    return draft.name.trim() !== selected.name || normalize(toPreset(draft)) !== normalize(selected)
  }, [draft, selected])

  const update = useCallback((fn: (prev: Draft) => Draft) => {
    setDraft(prev => (prev ? fn(prev) : prev))
  }, [])

  const toggleUseDefault = useCallback(
    (on: boolean) => {
      update(prev => ({
        ...prev,
        useDefault: on,
        // Leaving default → materialize the current full surface so the user
        // prunes from "everything" instead of an empty list.
        enabledToolsets: on ? prev.enabledToolsets : catalog ? materializeAll(catalog) : prev.enabledToolsets
      }))
    },
    [catalog, update]
  )

  const toggleGroup = useCallback(
    (toolset: string) => {
      update(prev => ({ ...prev, enabledToolsets: toggleMembership(prev.enabledToolsets, toolset) }))
    },
    [update]
  )

  const toggleTool = useCallback(
    (toolset: string, tool: string) => {
      update(prev => {
        // Toolset on → the tool lives in the blacklist; toolset off → it lives
        // in the per-tool whitelist.
        if (isToolsetEnabled(prev, toolset)) {
          return { ...prev, disabledTools: toggleMembership(prev.disabledTools, tool) }
        }

        return { ...prev, allowedTools: toggleMembership(prev.allowedTools, tool) }
      })
    },
    [update]
  )

  const toggleSkill = useCallback(
    (skill: string) => {
      update(prev => ({ ...prev, disabledSkills: toggleMembership(prev.disabledSkills, skill) }))
    },
    [update]
  )

  async function createPreset() {
    const gateway = activeGateway()
    const name = newName.trim()

    if (!gateway || !name) {
      return
    }

    if (RESERVED_NAMES.has(name)) {
      notifyError(new Error('reserved name'), `"${name}" is a reserved built-in preset name`)

      return
    }

    setSaving(true)

    try {
      // New presets start chat-only ([] = zero non-core tools) so additions are
      // explicit and the token savings are visible from the outset.
      const result = await gateway.toolsPresetSave({
        name,
        enabled_toolsets: [],
        disabled_tools: [],
        allowed_tools: null,
        disabled_skills: []
      })

      setPresets(result.presets)
      setSelectedName(name)
      setCreating(false)
      setNewName('')
      notify({ kind: 'success', title: 'Preset created', message: `Created "${name}"` })
    } catch (err) {
      notifyError(err, `Could not create "${name}"`)
    } finally {
      setSaving(false)
    }
  }

  async function savePreset() {
    const gateway = activeGateway()

    if (!gateway || !draft || !selected) {
      return
    }

    const payload = toPreset(draft)

    if (!payload.name) {
      notifyError(new Error('empty name'), 'Preset name cannot be empty')

      return
    }

    // Editing a built-in in place is allowed — it persists an override (delete
    // resets it). Only block a reserved name when it differs from the selected
    // preset, i.e. renaming a user preset onto a reserved built-in's name.
    if (RESERVED_NAMES.has(payload.name) && payload.name !== selected.name) {
      notifyError(new Error('reserved name'), `"${payload.name}" is a reserved built-in preset name`)

      return
    }

    setSaving(true)

    try {
      let result = await gateway.toolsPresetSave(payload)
      const renamed = payload.name !== selected.name

      if (renamed) {
        // Rename = save-under-new-name then drop the old name. The save has
        // already succeeded, so a delete failure must NOT surface as a save
        // failure: the new preset exists and only the stale old name lingers.
        // Report that accurately and adopt the new name rather than rolling the
        // (successful) save back and risking losing the edited content.
        try {
          result = await gateway.toolsPresetDelete(selected.name)
        } catch (deleteErr) {
          setPresets(result.presets)
          setSelectedName(payload.name)
          notifyError(
            deleteErr,
            `Saved "${payload.name}", but couldn't remove the old "${selected.name}" — delete it manually.`
          )

          return
        }
      }

      setPresets(result.presets)
      setSelectedName(payload.name)
      notify({ kind: 'success', title: 'Preset saved', message: `Saved "${payload.name}"` })
    } catch (err) {
      notifyError(err, `Could not save "${payload.name}"`)
    } finally {
      setSaving(false)
    }
  }

  async function deletePreset() {
    const gateway = activeGateway()

    if (!gateway || !selected) {
      return
    }

    // Built-ins can't be removed — deleting one clears its override, resetting
    // it to the default definition.
    const resetting = Boolean(selected.builtin)

    const prompt = resetting
      ? `Reset built-in preset "${selected.name}" to its default?`
      : `Delete preset "${selected.name}"?`

    if (!window.confirm(prompt)) {
      return
    }

    setSaving(true)

    try {
      const result = await gateway.toolsPresetDelete(selected.name)
      setPresets(result.presets)
      setSelectedName(resetting ? selected.name : null)
      notify({
        kind: 'success',
        title: resetting ? 'Preset reset' : 'Preset deleted',
        message: resetting ? `Reset "${selected.name}" to default` : `Deleted "${selected.name}"`
      })
    } catch (err) {
      notifyError(err, resetting ? `Could not reset "${selected.name}"` : `Could not delete "${selected.name}"`)
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <SettingsContent>
        <SectionHeading icon={SlidersHorizontal} title="Tool Presets" />
        <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" />
          Loading catalog…
        </div>
      </SettingsContent>
    )
  }

  return (
    <SettingsContent>
      <SectionHeading icon={SlidersHorizontal} meta={`${presets.length}`} title="Tool Presets" />
      <p className="mb-4 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
        Reusable tool / MCP / skill selections a chat can adopt. Trimming the surface shrinks the tool schemas, guidance,
        and skills index sent to the model — the token estimate updates as you toggle items.
      </p>

      {/* Default preset for new chats — the profile-scoped starting posture every
          new chat inherits (a draft-time pick in the composer overrides it). */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg bg-background/55 px-3 py-2.5">
        <div className="min-w-0">
          <div className="text-sm font-medium">Default preset for new chats</div>
          <div className="text-[0.7rem] text-muted-foreground">
            Every new chat starts with this preset. Pick one per chat from the composer to override it.
          </div>
        </div>
        <Select
          disabled={savingDefault}
          onValueChange={value => void changeDefaultPreset(value)}
          value={defaultPreset ?? NO_DEFAULT}
        >
          <SelectTrigger className="w-48 shrink-0">
            <SelectValue placeholder="Platform default" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NO_DEFAULT}>Platform default</SelectItem>
            {presets.map(preset => (
              <SelectItem key={preset.name} value={preset.name}>
                {preset.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-4 @2xl:grid-cols-[15rem_minmax(0,1fr)]">
        {/* Preset list */}
        <div className="grid content-start gap-1">
          {presets.map(preset => {
            const active = preset.name === selectedName

            return (
              <button
                aria-pressed={active}
                className={cn(
                  'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition',
                  active ? 'bg-(--ui-bg-tertiary) text-foreground' : 'hover:bg-accent/40'
                )}
                key={preset.name}
                onClick={() => setSelectedName(preset.name)}
                type="button"
              >
                <span className="min-w-0 flex-1 truncate">{preset.name}</span>
                {preset.builtin && <Lock className="size-3 shrink-0 text-muted-foreground" />}
              </button>
            )
          })}

          {creating ? (
            <div className="mt-1 grid gap-1.5 rounded-md bg-background/55 p-2">
              <Input
                autoFocus
                onChange={e => setNewName(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    void createPreset()
                  }

                  if (e.key === 'Escape') {
                    setCreating(false)
                  }
                }}
                placeholder="Preset name"
                value={newName}
              />
              <div className="flex items-center gap-1.5">
                <Button disabled={saving || !newName.trim()} onClick={() => void createPreset()} size="xs">
                  {saving ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
                  Create
                </Button>
                <Button
                  onClick={() => {
                    setCreating(false)
                    setNewName('')
                  }}
                  size="xs"
                  variant="text"
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <Button className="mt-1 justify-start" onClick={() => setCreating(true)} size="sm" variant="outline">
              <Plus className="size-3.5" />
              New preset
            </Button>
          )}
        </div>

        {/* Editor */}
        {!draft || !catalog ? (
          <EmptyState title="Select a preset to view or edit its tool selection." />
        ) : (
          <div className="grid content-start gap-3">
            {/* Header: name + running total + actions */}
            <div className="grid gap-2 rounded-lg bg-background/60 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                {isBuiltin ? (
                  <span className="flex items-center gap-2 text-sm font-medium">
                    {draft.name}
                    <Pill>Built-in</Pill>
                  </span>
                ) : (
                  <Input
                    aria-label="Preset name"
                    className="max-w-56"
                    onChange={e => update(prev => ({ ...prev, name: e.target.value }))}
                    value={draft.name}
                  />
                )}
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">Est. total</span>
                  <Badge className="font-mono" variant="default">
                    {formatTokens(total)}
                  </Badge>
                </div>
              </div>

              {catalog.core_tokens > 0 && (
                <p className="text-[0.68rem] text-muted-foreground">
                  Core tools {formatTokens(catalog.core_tokens)} are always included.
                </p>
              )}

              {(
                <div className="flex items-center gap-2 pt-1">
                  <Button disabled={saving || !isDirty} onClick={() => void savePreset()} size="sm">
                    {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save />}
                    Save
                  </Button>
                  <Button disabled={saving} onClick={() => void deletePreset()} size="sm" variant="text">
                    <Trash2 className="size-3.5" />
                    {isBuiltin ? 'Reset' : 'Delete'}
                  </Button>
                  {isDirty && <span className="text-[0.68rem] text-muted-foreground">Unsaved changes</span>}
                </div>
              )}
            </div>

            {/* Use-profile-default toggle */}
            <div className="flex items-center justify-between gap-3 rounded-lg bg-background/55 px-3 py-2.5">
              <div className="min-w-0">
                <div className="text-sm font-medium">Use profile default</div>
                <div className="text-[0.7rem] text-muted-foreground">
                  All toolsets enabled (equivalent to "Full"). Turn off to pick specific toolsets.
                </div>
              </div>
              <Switch
                aria-label="Use profile default"
                checked={draft.useDefault}
                onCheckedChange={on => toggleUseDefault(on)}
              />
            </div>

            {/* Toolsets */}
            <div className="grid gap-1.5">
              <SectionHeading icon={SlidersHorizontal} title="Toolsets" />
              {catalog.toolsets.length === 0 ? (
                <span className="px-1 text-[0.7rem] text-muted-foreground">No toolsets available.</span>
              ) : (
                catalog.toolsets.map((toolset: ToolCatalogToolset) => (
                  <GroupRow
                    description={toolset.description}
                    draft={draft}
                    est_tokens={toolset.est_tokens}
                    key={toolset.name}
                    name={toolset.name}
                    onToggleGroup={() => toggleGroup(toolset.name)}
                    onToggleTool={tool => toggleTool(toolset.name, tool)}
                    tools={toolset.tools}
                    toolset={toolset.name}
                    toolsetLocked={draft.useDefault}
                  />
                ))
              )}
            </div>

            {/* MCP servers */}
            {catalog.mcp_servers.length > 0 && (
              <div className="grid gap-1.5">
                <SectionHeading icon={Globe} title="MCP Servers" />
                {catalog.mcp_servers.map((server: ToolCatalogMcpServer) => (
                  <GroupRow
                    draft={draft}
                    est_tokens={server.est_tokens}
                    key={server.toolset}
                    name={server.name}
                    onToggleGroup={() => toggleGroup(server.toolset)}
                    onToggleTool={tool => toggleTool(server.toolset, tool)}
                    tools={server.tools}
                    toolset={server.toolset}
                    toolsetLocked={draft.useDefault}
                  />
                ))}
              </div>
            )}

            {/* Skills */}
            {catalog.skills.length > 0 && (
              <div className="grid gap-1.5">
                <SectionHeading icon={Zap} meta={`${formatTokens(skillsTotal)} enabled`} title="Skills" />
                <div className="grid gap-1 rounded-lg bg-background/55 p-2">
                  {catalog.skills.map(skill => (
                    <label className="flex cursor-pointer items-center gap-2 px-0.5 py-1" key={skill.name}>
                      <Checkbox
                        aria-label={skill.name}
                        checked={!draft.disabledSkills.includes(skill.name)}
                        onCheckedChange={() => toggleSkill(skill.name)}
                      />
                      <span className="min-w-0 flex-1 truncate text-[0.72rem]">
                        {skill.name}
                        {skill.category && <span className="ml-1.5 text-muted-foreground">{skill.category}</span>}
                      </span>
                      <TokenBadge tokens={skill.est_tokens} />
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </SettingsContent>
  )
}

/**
 * ReasoningPicker — sets the main model's reasoning effort from the dashboard
 * Chat sidebar, mirroring the desktop app's composer effort radio.
 *
 * The dashboard previously only showed a read-only "Reasoning" capability
 * badge (see ModelInfoCard) with no way to actually choose the effort level —
 * unlike the desktop app, which exposes a radio in its model menu. This closes
 * that parity gap.
 *
 * Storage: the effort persists to config.yaml at `agent.reasoning_effort`
 * (the same key the TUI's `/reasoning <level>` command and the desktop radio
 * write). We read the whole config and write it back — the established
 * single-key pattern on the dashboard (see ConfigPage) — so the value lands in
 * the config the agent boots a fresh chat from. As with the model picker, the
 * running chat session adopts the change on the next `/new` or page reload;
 * we surface that hint rather than forcing a reload here.
 *
 * Profile scoping: the sidebar passes the chat profile explicitly, so this
 * reads/writes the same config the chat PTY was launched from.
 *
 * Per-skill row invariants (addressed in review #93378 blocker #4):
 *   - Row identity is a STABLE local id, NOT the editable skill name, so
 *     renaming a skill does not remount the row and drop its commit closure.
 *   - The map is serialized to config ONLY when a valid skill name is
 *     committed (blur / Enter), never on keystroke or on adding a blank row.
 *   - Selecting Off persists the literal "off" sentinel (which the resolver
 *     reads as "skip this skill's suggestion / fall through"), instead of
 *     deleting the key — deleting would let the skill's own frontmatter
 *     suggestion run. The trash button is the only delete.
 */

import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Brain, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { api } from "@/lib/api";
import {
  EFFORT_OPTIONS,
  normalizeEffort,
  normalizeSkillEffort,
  VALID_EFFORTS,
} from "@/lib/reasoning-effort";

interface ReasoningPickerProps {
  /** Current model string from config — re-reads the saved effort when it
   *  changes (a different model may have been selected). */
  currentModel: string;
  /** Profile whose config should be read/written. */
  profile?: string;
  /** Bumped after the model picker saves, to re-read config in lockstep. */
  refreshKey?: number;
  /** Called after a successful change so the sidebar can show an "apply on
   *  /new or reload" notice, matching the model-switch UX. */
  onChanged?: (effort: string) => void;
}

/** A per-skill override row kept in local editor state. `skill` may be an
 *  in-progress (uncommitted) name; only a non-empty committed name is
 *  serialized to the config map. `id` is stable across renames so the row
 *  never remounts mid-edit. */
interface SkillRow {
  id: number;
  skill: string;
  effort: string;
}

export function ReasoningPicker({
  currentModel,
  profile,
  refreshKey = 0,
  onChanged,
}: ReasoningPickerProps) {
  const [effort, setEffort] = useState("medium");
  const [skillRows, setSkillRows] = useState<SkillRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const lastFetchKeyRef = useRef("");
  const nextRowIdRef = useRef(1);

  useEffect(() => {
    const fetchKey = `${profile ?? ""}:${currentModel}:${refreshKey}`;
    if (fetchKey === lastFetchKeyRef.current) return;
    lastFetchKeyRef.current = fetchKey;
    void api
      .getConfig(profile)
      .then((cfg) => {
        const agent = (cfg?.agent as Record<string, unknown> | undefined) ?? {};
        setEffort(normalizeEffort(agent.reasoning_effort));
        const map = agent.reasoning_by_skill;
        const raw =
          map && typeof map === "object"
            ? (map as Record<string, string>)
            : {};
        // Materialize each committed entry as a stable row.
        setSkillRows(
          Object.entries(raw).map(([skill, eff]) => ({
            id: nextRowIdRef.current++,
            skill,
            effort: normalizeSkillEffort(eff),
          })),
        );
        setLoaded(true);
      })
      .catch(() => {
        // Best-effort: keep the last known value rather than blanking it.
        setLoaded(true);
      });
  }, [currentModel, profile, refreshKey]);

  /** Serialize only the committed (non-empty-skill) rows to the config map,
   *  preserving the literal "off" sentinel for disabled skills. Never called
   *  with an uncommitted/blank row. */
  const persistSkillMap = useCallback(
    (rows: SkillRow[]) => {
      const nextMap: Record<string, string> = {};
      for (const row of rows) {
        const name = row.skill.trim();
        if (!name) continue; // never persist an empty-string key
        nextMap[name] = row.effort;
      }
      setSaving(true);
      void api
        .getConfig(profile)
        .then((cfg) => {
          const base = (cfg ?? {}) as Record<string, unknown>;
          const agent =
            base.agent && typeof base.agent === "object"
              ? { ...(base.agent as Record<string, unknown>) }
              : {};
          agent.reasoning_by_skill = nextMap;
          return api.saveConfig({ ...base, agent }, profile);
        })
        .catch(() => {
          // Revert on failure is best-effort; keep current state.
        })
        .finally(() => setSaving(false));
    },
    [profile],
  );

  const onSelect = useCallback(
    (next: string) => {
      if (!VALID_EFFORTS.has(next) || next === effort) return;
      const prev = effort;
      setEffort(next); // optimistic
      setSaving(true);
      void api
        .getConfig(profile)
        .then((cfg) => {
          const base = (cfg ?? {}) as Record<string, unknown>;
          const agent =
            base.agent && typeof base.agent === "object"
              ? { ...(base.agent as Record<string, unknown>) }
              : {};
          agent.reasoning_effort = next;
          return api.saveConfig({ ...base, agent }, profile);
        })
        .then(() => {
          onChanged?.(next);
        })
        .catch(() => {
          setEffort(prev); // revert on failure
        })
        .finally(() => setSaving(false));
    },
    [effort, onChanged, profile],
  );

  const setRowSkill = (id: number, skill: string) => {
    // Update local state only. Persisting here would fire a full config
    // GET+PUT on EVERY keystroke (e.g. typing "planning" → ~8 writes of
    // junk intermediates that race concurrent writers). The name is committed
    // to config on blur / Enter via commitRowSkill.
    setSkillRows((rows) =>
      rows.map((r) => (r.id === id ? { ...r, skill } : r)),
    );
  };

  const commitRowSkill = (id: number) => {
    // Commit (persist) a row only when it has a valid, non-empty skill name.
    const row = skillRows.find((r) => r.id === id);
    if (!row) return;
    if (!row.skill.trim()) return; // empty name — leave the row, don't persist
    persistSkillMap(skillRows);
  };

  const setRowEffort = (id: number, eff: string) => {
    const row = skillRows.find((r) => r.id === id);
    if (!row) return;
    const normalized = normalizeSkillEffort(eff);
    // Persist the literal "off" sentinel (resolver: "skip this skill's layer,
    // fall through") rather than deleting the key, which would let the
    // skill's own frontmatter suggestion run.
    setSkillRows((rows) =>
      rows.map((r) => (r.id === id ? { ...r, effort: normalized } : r)),
    );
    if (!row.skill.trim()) return; // don't persist an uncommitted row
    persistSkillMap(skillRows.map((r) => (r.id === id ? { ...r, effort: normalized } : r)));
  };

  const addRow = () => {
    // Add a blank row to LOCAL state only; nothing is persisted until a valid
    // skill name is committed (fixes the old `{"": "xhigh"}` early write).
    setSkillRows((rows) => [
      ...rows,
      { id: nextRowIdRef.current++, skill: "", effort: "medium" },
    ]);
  };

  const removeRow = (id: number) => {
    const nextRows = skillRows.filter((r) => r.id !== id);
    setSkillRows(nextRows);
    persistSkillMap(nextRows);
  };

  return (
    <div className="px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1.5 text-text-tertiary">
          <Brain className="h-3.5 w-3.5" />
          <span className="text-display tracking-wider">reasoning</span>
        </div>
        <Select
          className="ml-auto min-w-0"
          disabled={!loaded || saving}
          onValueChange={onSelect}
          value={effort}
        >
          {EFFORT_OPTIONS.map((opt) => (
            <SelectOption key={opt.value} value={opt.value}>
              {opt.label}
            </SelectOption>
          ))}
        </Select>
      </div>

      <div className="mt-2 flex flex-col gap-1.5 border-t border-line pt-2">
        <span className="text-display tracking-wider text-text-tertiary">
          per-skill
        </span>
        {skillRows.map((row) => (
          <div key={row.id} className="flex items-center gap-1.5">
            <input
              className="min-w-0 flex-1 rounded border border-line bg-transparent px-1.5 py-1"
              value={row.skill}
              disabled={saving}
              placeholder="skill name (e.g. plan)"
              onChange={(e) => setRowSkill(row.id, e.target.value)}
              onBlur={() => commitRowSkill(row.id)}
              onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
                if (e.key === "Enter") {
                  e.currentTarget.blur();
                }
              }}
            />
            <Select
              className="min-w-0"
              disabled={saving}
              onValueChange={(v) => setRowEffort(row.id, v)}
              value={normalizeSkillEffort(row.effort)}
            >
              <SelectOption value="off">Off</SelectOption>
              {EFFORT_OPTIONS.map((opt) => (
                <SelectOption key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectOption>
              ))}
            </Select>
            <button
              className="shrink-0 text-text-tertiary hover:text-danger"
              disabled={saving}
              aria-label={`Remove ${row.skill || "skill"} override`}
              onClick={() => removeRow(row.id)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
        <button
          data-testid="add-row"
          className="flex items-center gap-1 self-start text-text-tertiary hover:text-text-secondary"
          disabled={saving}
          onClick={addRow}
        >
          <Plus className="h-3 w-3" />
          Add skill override
        </button>
      </div>
    </div>
  );
}

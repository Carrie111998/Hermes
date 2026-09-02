/**
 * Pure reasoning-effort helpers shared by the dashboard ReasoningPicker.
 *
 * Kept DOM-free so the node-environment vitest harness can cover the
 * resolution logic without loading React or the UI kit.
 *
 * Values mirror hermes_constants.VALID_REASONING_EFFORTS plus `none`
 * (thinking-off). An empty/unset config value means the Hermes default,
 * which is `medium`.
 */

export interface EffortOption {
  value: string;
  label: string;
}

export const EFFORT_OPTIONS: ReadonlyArray<EffortOption> = [
  { value: "none", label: "Off (no thinking)" },
  { value: "minimal", label: "Minimal" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Extra High" },
  { value: "max", label: "Max" },
  { value: "ultra", label: "Ultra" },
];

export const VALID_EFFORTS: ReadonlySet<string> = new Set(
  EFFORT_OPTIONS.map((o) => o.value),
);

/** Normalize a raw `agent.reasoning_effort` config value to a selectable
 *  option. Empty/unknown → `medium` (Hermes' default when unset). */
export function normalizeEffort(raw: unknown): string {
  const value = String(raw ?? "").trim().toLowerCase();
  if (!value) return "medium";
  return VALID_EFFORTS.has(value) ? value : "medium";
}

/** The falsey spellings the per-skill reasoning map accepts to disable a
 *  skill's suggestion. Mirrors the resolver's `_SKILL_OFF` set (the map
 *  accepts a broader falsey set than the global effort, which stays
 *  global-safe on {"none","false","disabled"}). */
export const SKILL_OFF_VALUES: ReadonlySet<string> = new Set([
  "off", "false", "no", "none", "0", "disable", "disabled",
]);

/** Normalize a per-skill `agent.reasoning_by_skill` value to a selectable
 *  option, where "off" is the explicit "disable this skill's suggestion"
 *  sentinel. A valid effort passes through; anything falsey/unknown maps to
 *  "off" (disabled) rather than guessing a level. */
export function normalizeSkillEffort(raw: unknown): string {
  if (raw === false) return "off";
  const value = String(raw ?? "").trim().toLowerCase();
  if (!value) return "off";
  if (SKILL_OFF_VALUES.has(value)) return "off";
  return VALID_EFFORTS.has(value) ? value : "off";
}


import type { CronJob, CronJobMutation } from "./api";
import { VALID_EFFORTS } from "./reasoning-effort";

export interface CronJobFormState {
  name: string;
  prompt: string;
  schedule: string;
  deliver: string;
  skills: string[];
  provider: string;
  model: string;
  base_url: string;
  script: string;
  no_agent: boolean;
  reasoning_effort: string | null;
  /** Preserve a malformed legacy stored value when the reasoning selector was
   *  not changed, instead of rewriting it to null on an unrelated edit. */
  preserve_reasoning_on_save: boolean;
  context_from: string;
  enabled_toolsets: string[];
  workdir: string;
}

/** Split a comma/newline list (or array) into trimmed, non-empty items. */
export function splitCronList(value: unknown): string[] {
  const items = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(/[\n,]/)
      : [];
  return items.map((item) => String(item).trim()).filter(Boolean);
}

/** Trim to a non-empty string, or null. Optionally strip trailing slashes
 * (base URLs). Mirrors the backend's `_cron_optional_text`. */
function optionalText(value: string, stripTrailingSlash = false): string | null {
  const text = stripTrailingSlash ? value.trim().replace(/\/+$/, "") : value.trim();
  return text || null;
}

/** Coerce a stored list/string field back into the textarea's newline form. */
function listToText(value: unknown): string {
  if (Array.isArray(value)) return splitCronList(value).join("\n");
  return typeof value === "string" ? value : "";
}

/** Read a stored string field as a plain string ("" when absent). */
function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function normalizeReasoningEffort(value: unknown): string | null {
  if (value === false) return "none";
  if (typeof value !== "string") return null;
  const effort = value.trim().toLowerCase();
  return VALID_EFFORTS.has(effort) ? effort : null;
}

/** True when a stored value is present but not canonical: legacy/hand-edited
 *  values the backend keeps readable but should not be rewritten on read. */
function hasMalformedReasoning(value: unknown): boolean {
  if (value === false) return false;
  if (typeof value !== "string") return value !== null && value !== undefined;
  return normalizeReasoningEffort(value) === null;
}

/** Build the create/update payload. A valid effort is always emitted; an
 *  explicit null clears the override; a malformed legacy value that was not
 *  touched is omitted so an unrelated dashboard edit preserves it. */
function optionalReasoningEffort(
  value: string | null,
  preserve: boolean,
): string | null | undefined {
  const normalized = normalizeReasoningEffort(value);
  if (normalized !== null) return normalized;
  return preserve ? undefined : null;
}
export function buildCronJobPayload(form: CronJobFormState): CronJobMutation {
  const contextFrom = splitCronList(form.context_from);
  const enabledToolsets = form.enabled_toolsets.filter(Boolean);
  const reasoningEffort = optionalReasoningEffort(
    form.reasoning_effort,
    form.preserve_reasoning_on_save,
  );
  const payload: CronJobMutation = {
    name: form.name.trim(),
    prompt: form.prompt.trim(),
    schedule: form.schedule.trim(),
    deliver: form.deliver.trim() || "local",
    skills: form.skills.filter(Boolean),
    provider: optionalText(form.provider),
    model: optionalText(form.model),
    base_url: optionalText(form.base_url, true),
    script: optionalText(form.script),
    no_agent: Boolean(form.no_agent),
    context_from: contextFrom.length > 0 ? contextFrom : null,
    enabled_toolsets: enabledToolsets.length > 0 ? enabledToolsets : null,
    workdir: optionalText(form.workdir),
  };
  if (reasoningEffort !== undefined) payload.reasoning_effort = reasoningEffort;
  return payload;
}

export function cronJobHasExecutionContent(
  job: Pick<CronJobMutation, "prompt" | "skills" | "script">,
): boolean {
  const skills = Array.isArray(job.skills) ? job.skills.filter(Boolean) : [];
  return Boolean(asString(job.prompt).trim() || asString(job.script).trim() || skills.length);
}

export function cronJobFormFromJob(job: CronJob): CronJobFormState {
  const reasoningEffort = normalizeReasoningEffort(job.reasoning_effort);
  const malformedReasoning = hasMalformedReasoning(job.reasoning_effort);
  return {
    name: asString(job.name),
    prompt: asString(job.prompt),
    schedule:
      asString(job.schedule?.expr) ||
      asString(job.schedule?.run_at) ||
      asString(job.schedule_display),
    deliver: asString(job.deliver) || "local",
    skills: Array.isArray(job.skills) ? job.skills.filter(Boolean) : [],
    provider: asString(job.provider),
    model: asString(job.model),
    base_url: asString(job.base_url),
    script: asString(job.script),
    no_agent: Boolean(job.no_agent),
    reasoning_effort:
      reasoningEffort ??
      (malformedReasoning && typeof job.reasoning_effort === "string"
        ? job.reasoning_effort.trim().toLowerCase()
        : null),
    preserve_reasoning_on_save: malformedReasoning,
    context_from: listToText(job.context_from),
    enabled_toolsets: splitCronList(job.enabled_toolsets),
    workdir: asString(job.workdir),
  };
}

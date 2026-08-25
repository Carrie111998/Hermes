#!/usr/bin/env -S node --experimental-strip-types
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// ── Types ───────────────────────────────────────────────────────────────────

type Skill = {
  name: string;
  baseName: string;
  description: string;
  path: string;
  realPath: string;
  dir: string;
  root: string;
  realRoot: string;
  scope: string;
  descChars: number;
  lineChars: number;
  lineBytes: number;
  bodyHash: string;
  bodyKey: string;
  descKey: string;
};

type Usage = {
  dollar: number;
  fileRead: number;
  text: number;
};

type Budget = {
  model: string;
  contextTokens: number;
  contextSource: string;
  budgetPercent: number;
  budgetTokens: number;
  renderedLineChars: number;
  unbudgetedFullTokens: number;
  minimumTokens: number;
  budgetedTokens: number;
  charsPerToken: number;
  unbudgetedBudgetUsedRatio: number;
  budgetedBudgetUsedRatio: number;
  unbudgetedContextUsedRatio: number;
  budgetedContextUsedRatio: number;
  remainingBudgetTokens: number;
  includedSkills: number;
  omittedSkills: number;
  truncatedDescriptionChars: number;
  truncatedDescriptionCount: number;
};

// ── CLI Args ────────────────────────────────────────────────────────────────

const home = os.homedir();
const args = new Set(process.argv.slice(2));

function argValue(name: string, fallback: string): string {
  const raw = process.argv.slice(2);
  const index = raw.indexOf(name);
  return index >= 0 && raw[index + 1] ? raw[index + 1] : fallback;
}

const months = Number(argValue("--months", "3"));
const noLogs = args.has("--no-logs");
const deepLogs = args.has("--deep-logs");
const json = args.has("--json");
const budgetPercent = Number(argValue("--budget-percent", "2"));
const contextTokensOverride = argValue("--context-tokens", "");
const charsPerToken = Number(argValue("--chars-per-token", "4"));
const maxLogBytes = Number(argValue("--max-log-mb", "300")) * 1024 * 1024;
const cutoffMs = Date.now() - Math.max(0, months) * 31 * 24 * 60 * 60 * 1000;
const extraRoots = process.argv
  .slice(2)
  .flatMap((arg, index, all) => (arg === "--root" && all[index + 1] ? [all[index + 1]] : []));

// ── Utilities ───────────────────────────────────────────────────────────────

function expandHome(input: string): string {
  return input.replace(/^~(?=$|\/)/, home);
}

function exists(input: string): boolean {
  try { fs.accessSync(input); return true; } catch { return false; }
}

function numberArg(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function sanitizeSingleLine(value: string): string {
  return value.replace(/[\r\n\t]+/g, " ").replace(/\s+/g, " ").trim();
}

function parseYamlScalar(raw: string): string {
  const value = raw.trim();
  if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
    return value.slice(1, -1);
  }
  return value;
}

function fnv1a(input: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function normalizeWords(input: string): string {
  return input.toLowerCase().replace(/[`"'’().,;:!?/\\[\]{}_-]+/g, " ").replace(/\s+/g, " ").trim();
}

function wordSet(input: string): Set<string> {
  return new Set(normalizeWords(input).split(" ").filter((w) => w.length >= 2));
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let intersection = 0;
  for (const item of a) if (b.has(item)) intersection++;
  return intersection / (a.size + b.size - intersection);
}

function groupBy<T>(items: T[], key: (item: T) => string): Map<string, T[]> {
  const map = new Map<string, T[]>();
  for (const item of items) {
    const k = key(item);
    map.set(k, [...(map.get(k) ?? []), item]);
  }
  return map;
}

function countTokens(values: string[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const v of values) map.set(v, (map.get(v) ?? 0) + 1);
  return map;
}

function formatPct(value: number): string { return `${Math.round(value * 100)}%`; }
function formatOnePct(value: number): string { return `${(value * 100).toFixed(1)}%`; }
function formatNumber(value: number): string { return Math.round(value).toLocaleString("en-US"); }
function tokenCost(text: string): number { return Math.ceil(Buffer.byteLength(text, "utf8") / 4); }

// ── YAML Config Reader (minimal, zero-dependency) ───────────────────────────

function readYamlPath(file: string, keyPath: string[]): string | null {
  if (!exists(file)) return null;
  const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
  let targetDepth = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    const indent = line.search(/\S/);
    if (indent < 0) continue;
    if (indent !== targetDepth * 2) continue;
    const trimmed = line.trim();
    const colonIdx = trimmed.indexOf(":");
    if (colonIdx < 0) continue;
    const key = trimmed.slice(0, colonIdx).trim();
    if (key !== keyPath[targetDepth]) continue;
    if (targetDepth === keyPath.length - 1) {
      const value = trimmed.slice(colonIdx + 1).trim();
      return value ? parseYamlScalar(value) : null;
    }
    targetDepth++;
    if (targetDepth >= keyPath.length) return null;
  }
  return null;
}

function hermesModelContext(): { tokens: number; source: string; model: string } {
  const configPath = path.join(home, ".hermes", "config" + ".yaml");
  const override = numberArg(contextTokensOverride, 0);

  // Read model name from config
  const modelName = argValue("--model", readYamlPath(configPath, ["model", "default"]) ?? "unknown");

  // Read context window: --context-tokens > config > fallback
  let tokens = override;
  let source = "--context-tokens";
  if (!tokens) {
    const configCtx = readYamlPath(configPath, ["model", "context_length"]);
    if (configCtx) {
      tokens = numberArg(configCtx, 0);
      if (tokens) source = configPath;
    }
  }
  if (!tokens) {
    tokens = 272_000;
    source = "fallback";
  }
  return { tokens, source, model: modelName };
}

// ── File Walking ────────────────────────────────────────────────────────────

function walkFiles(
  root: string,
  predicate: (file: string) => boolean,
  maxDepth = 8,
  timeFilter?: (mtimeMs: number) => boolean,
): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  function walk(dir: string, depth: number) {
    if (depth > maxDepth) return;
    let real = dir;
    try { real = fs.realpathSync(dir); } catch { return; }
    if (seen.has(real)) return;
    seen.add(real);
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (entry.name === "node_modules" || entry.name === ".git" || entry.name === ".archive") continue;
      const file = path.join(dir, entry.name);
      if (entry.isDirectory() || entry.isSymbolicLink()) {
        let stat: fs.Stats;
        try { stat = fs.statSync(file); } catch { continue; }
        if (stat.isDirectory()) {
          if (timeFilter && depth > 0 && !timeFilter(stat.mtimeMs)) continue;
          walk(file, depth + 1);
        }
      } else if (entry.isFile() && predicate(file)) {
        if (timeFilter) {
          try { if (!timeFilter(fs.statSync(file).mtimeMs)) continue; } catch { continue; }
        }
        out.push(file);
      }
    }
  }
  if (exists(root)) walk(root, 0);
  return out;
}

// ── Frontmatter Parsing ─────────────────────────────────────────────────────

function parseFrontmatter(file: string): { name?: string; description?: string; body: string } | null {
  const text = fs.readFileSync(file, "utf8");
  const lines = text.split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return null;
  const fm: string[] = [];
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i]?.trim() === "---") { end = i; break; }
    fm.push(lines[i] ?? "");
  }
  if (end < 0) return null;
  let name: string | undefined;
  let description: string | undefined;
  for (let i = 0; i < fm.length; i++) {
    const line = fm[i] ?? "";
    const match = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (!match) continue;
    const key = match[1];
    const raw = match[2] ?? "";
    if (key === "name") name = sanitizeSingleLine(parseYamlScalar(raw));
    if (key === "description") {
      if (raw.trim() === "|" || raw.trim() === ">") {
        const block: string[] = [];
        for (let j = i + 1; j < fm.length; j++) {
          if (/^[A-Za-z0-9_-]+:\s*/.test(fm[j] ?? "")) break;
          block.push((fm[j] ?? "").replace(/^\s{2}/, ""));
        }
        description = sanitizeSingleLine(block.join(" "));
      } else {
        description = sanitizeSingleLine(parseYamlScalar(raw));
      }
    }
  }
  return { name, description, body: lines.slice(end + 1).join("\n") };
}

// ── Skill Discovery ─────────────────────────────────────────────────────────

function skillRootScope(root: string): string {
  const normalized = root.split(path.sep).join("/");
  if (normalized.includes("/.hermes/skills")) return "hermes";
  if (normalized.includes("/.agents/skills")) return "repo";
  return "extra";
}

function discoverRoots(): string[] {
  const rootsByRealPath = new Map<string, string>();
  // Primary: ~/.hermes/skills
  const hermesSkills = path.join(home, ".hermes/skills");
  if (exists(hermesSkills)) {
    const real = fs.realpathSync(hermesSkills);
    rootsByRealPath.set(real, hermesSkills);
  }
  // Project .agents/skills
  const projects = path.join(home, "Projects");
  if (exists(projects)) {
    for (const entry of fs.readdirSync(projects, { withFileTypes: true })) {
      if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
      const skillRoot = path.join(projects, entry.name, ".agents/skills");
      if (exists(skillRoot)) {
        const real = fs.realpathSync(skillRoot);
        const current = rootsByRealPath.get(real);
        if (!current || skillRoot.length < current.length) rootsByRealPath.set(real, skillRoot);
      }
    }
  }
  // Extra roots from --root
  for (const root of extraRoots.map(expandHome)) {
    if (!exists(root)) continue;
    const real = fs.realpathSync(root);
    const current = rootsByRealPath.get(real);
    if (!current || root.length < current.length) rootsByRealPath.set(real, root);
  }
  return [...rootsByRealPath.values()].sort();
}

function preferredDisplaySkill(a: Skill, b: Skill): Skill {
  // Prefer the shorter path (less nested)
  return a.path.length <= b.path.length ? a : b;
}

function discoverSkills(): Skill[] {
  const skillsByRealPath = new Map<string, Skill>();
  for (const root of discoverRoots()) {
    for (const file of walkFiles(root, (c) => path.basename(c) === "SKILL.md", 10)) {
      const parsed = parseFrontmatter(file);
      if (!parsed) continue;
      const baseName = parsed.name || path.basename(path.dirname(file));
      const name = baseName;
      const description = parsed.description ?? "";
      const rendered = description
        ? `- ${name}: ${description} (file: ${file})`
        : `- ${name}: (file: ${file})`;
      const bodyKey = normalizeWords(parsed.body);
      const skill: Skill = {
        name,
        baseName,
        description,
        path: file,
        realPath: fs.realpathSync(file),
        dir: path.dirname(file),
        root,
        realRoot: fs.realpathSync(root),
        scope: skillRootScope(root),
        descChars: [...description].length,
        lineChars: [...`${rendered}\n`].length,
        lineBytes: Buffer.byteLength(`${rendered}\n`, "utf8"),
        bodyHash: fnv1a(bodyKey),
        bodyKey,
        descKey: normalizeWords(description),
      };
      const existing = skillsByRealPath.get(skill.realPath);
      skillsByRealPath.set(skill.realPath, existing ? preferredDisplaySkill(existing, skill) : skill);
    }
  }
  return [...skillsByRealPath.values()];
}

// ── Log File Discovery ──────────────────────────────────────────────────────

function recentLogFiles(): string[] {
  if (noLogs) return [];
  const files = new Set<string>();
  const timeFilter = (mtimeMs: number) => mtimeMs >= cutoffMs;

  // History files
  for (const hist of [".hermes/history.jsonl"]) {
    const history = path.join(home, hist);
    if (exists(history)) files.add(history);
  }

  // Session files
  const sessionsDir = path.join(home, ".hermes/sessions");
  for (const file of walkFiles(sessionsDir, (c) => c.endsWith(".jsonl") || c.endsWith(".json"), 8, timeFilter)) {
    files.add(file);
  }

  // Deep logs: archived sessions
  if (deepLogs) {
    const archived = path.join(home, ".hermes/archived_sessions");
    for (const file of walkFiles(archived, (c) => c.endsWith(".jsonl") || c.endsWith(".json"), 8, timeFilter)) {
      files.add(file);
    }
  }

  return [...files].sort();
}

// ── Usage Scanning ──────────────────────────────────────────────────────────

function scanUsage(skills: Skill[], logFiles: string[]): Map<string, Usage> {
  const aliases = new Map<string, string[]>();
  for (const skill of skills) {
    const values = new Set([skill.name, skill.baseName]);
    aliases.set(skill.name, [...values].map((v) => v.toLowerCase()));
  }
  const usage = new Map<string, Usage>();
  for (const skill of skills) usage.set(skill.name, { dollar: 0, fileRead: 0, text: 0 });
  let consumedBytes = 0;
  for (const file of logFiles) {
    let text = "";
    try {
      const stat = fs.statSync(file);
      if (stat.size > 150 * 1024 * 1024) continue;
      if (consumedBytes + stat.size > maxLogBytes) break;
      consumedBytes += stat.size;
      text = fs.readFileSync(file, "utf8");
    } catch { continue; }

    // $skill references (Codex-style, kept for compatibility)
    const dollarCounts = countTokens(
      [...text.matchAll(/\$([A-Za-z][A-Za-z0-9_.:-]{1,80})/g)].map((m) => (m[1] ?? "").toLowerCase()),
    );
    // File path references
    const pathCounts = countTokens(
      [...text.matchAll(/(?:^|[/"'`\\])skills\/([^/"'`\\\s]+)\/SKILL\.md/g)].map((m) => (m[1] ?? "").toLowerCase()),
    );
    // Natural language references
    const textCounts = countTokens(
      [...text.matchAll(/\b(?:use|using|load|read)\s+`?\$?([A-Za-z][A-Za-z0-9_.:-]{1,80})`?/gi)].map((m) =>
        (m[1] ?? "").toLowerCase(),
      ),
    );
    // Hermes: skill_view / skill_manage tool calls in JSONL
    // Parse each line as JSON for reliable extraction
    const hermesToolCounts = new Map<string, number>();
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const parsed = JSON.parse(trimmed);
        if (
          parsed.name &&
          (parsed.name === "skill_view" || parsed.name === "skill_manage")
        ) {
          let skillName: string | null = null;
          // From content field (tool result)
          if (typeof parsed.content === "string") {
            try {
              const inner = JSON.parse(parsed.content);
              if (typeof inner.name === "string") skillName = inner.name;
            } catch {}
          }
          // From arguments field (tool request)
          if (!skillName && typeof parsed.arguments === "string") {
            try {
              const inner = JSON.parse(parsed.arguments);
              if (typeof inner.name === "string") skillName = inner.name;
            } catch {}
          }
          if (skillName) {
            const lower = skillName.toLowerCase();
            hermesToolCounts.set(lower, (hermesToolCounts.get(lower) ?? 0) + 1);
          }
        }
      } catch {}
    }
    // Hermes: available_skills block lines
    const hermesAvailableCounts = countTokens(
      [...text.matchAll(/^- ([a-z][a-z0-9_.:-]{1,80}):\s/gm)].map((m) => (m[1] ?? "").toLowerCase()),
    );

    for (const [name, names] of aliases) {
      const item = usage.get(name);
      if (!item) continue;
      for (const candidate of names) {
        const htc = hermesToolCounts.get(candidate) ?? 0;
        item.dollar += dollarCounts.get(candidate) ?? 0;
        item.fileRead += pathCounts.get(candidate) ?? 0;
        item.text += (textCounts.get(candidate) ?? 0) + htc + (hermesAvailableCounts.get(candidate) ?? 0);
      }
    }
  }
  return usage;
}

// ── Description Suggestions ─────────────────────────────────────────────────

function suggestDescription(skill: Skill): string {
  const source = normalizeWords(`${skill.baseName} ${skill.description}`);
  const cues: string[] = [];
  const add = (label: string, pattern: RegExp) => {
    if (pattern.test(source) && !cues.includes(label)) cues.push(label);
  };
  add("GitHub", /\b(github|issue|pr|ci)\b|pull request/);
  add("deploy", /\b(deploy|ops|server|ssh|vm|cloudflare|worker)\b/);
  add("debug", /\b(debug|trace|inspect|profile|diagnos)\b/);
  add("search", /\b(search|archive|crawl|sync|history)\b/);
  add("docs", /\b(doc|docs|markdown|write|review)\b/);
  add("release", /\b(release|publish|ship|notar)\b/);
  add("create", /\b(create|scaffold|build|generate)\b/);
  const verbs = cues.length ? cues.slice(0, 5).join(", ") : skill.baseName.replace(/-/g, " ");
  return `${verbs}: ${shortAction(source)}.`;
}

function shortAction(source: string): string {
  if (/\btriage|review\b/.test(source)) return "triage, review, proof";
  if (/\bdebug|diagnos|inspect\b/.test(source)) return "debug, inspect, fix";
  if (/\bsearch|sync|archive\b/.test(source)) return "search, sync, summarize";
  if (/\bdeploy|release|publish|ship\b/.test(source)) return "deploy, release, verify";
  if (/\bcreate|scaffold|build\b/.test(source)) return "create, build, validate";
  return "audit, clean, verify";
}

// ── Duplicate Detection ─────────────────────────────────────────────────────

function similarity(a: Skill, b: Skill): { description: number; body: number; overall: number } {
  const desc = jaccard(wordSet(a.description), wordSet(b.description));
  const body = a.bodyHash === b.bodyHash ? 1 : jaccard(wordSet(a.bodyKey), wordSet(b.bodyKey));
  return { description: desc, body, overall: body * 0.8 + desc * 0.2 };
}

function isLikelyCopy(score: { description: number; body: number }): boolean {
  return score.body >= 0.95 || (score.body >= 0.85 && score.description >= 0.85);
}

function deletePriority(skill: Skill): number {
  // Prefer keeping: repo (project-specific) > hermes (user skills) > extra
  if (skill.scope === "repo") return 0;
  if (skill.scope === "hermes") return 1;
  return 2;
}

function preferredKeepSkill(list: Skill[]): Skill {
  return [...list].sort((a, b) => {
    const byPriority = deletePriority(a) - deletePriority(b);
    if (byPriority !== 0) return byPriority;
    return a.realPath.length - b.realPath.length || a.realPath.localeCompare(b.realPath);
  })[0]!;
}

function duplicateDeleteSuggestions(groups: [string, Skill[]][]): string[] {
  const lines: string[] = [];
  for (const [name, list] of groups.slice(0, 80)) {
    const keep = preferredKeepSkill(list);
    const candidates = list
      .filter((s) => s.realPath !== keep.realPath)
      .map((s) => ({ skill: s, score: similarity(keep, s) }))
      .filter(({ score }) => isLikelyCopy(score))
      .sort((a, b) => b.score.body - a.score.body || b.score.description - a.score.description);
    if (candidates.length === 0) continue;
    lines.push(`- ${name}`);
    lines.push(`  keep: ${keep.scope}: ${keep.path}`);
    for (const { skill, score } of candidates) {
      lines.push(
        `  delete: ${skill.scope}: ${skill.path} (similarity body=${formatPct(score.body)}, description=${formatPct(score.description)})`,
      );
    }
  }
  return lines.length ? lines : ["- none"];
}

// ── Budget Calculation ──────────────────────────────────────────────────────

function skillOrderRank(skill: Skill): number {
  if (skill.scope === "hermes") return 0;
  if (skill.scope === "repo") return 1;
  return 2;
}

function orderedSkillsForBudget(skills: Skill[]): Skill[] {
  return [...skills].sort((a, b) => {
    const byScope = skillOrderRank(a) - skillOrderRank(b);
    if (byScope !== 0) return byScope;
    return a.name.localeCompare(b.name) || a.path.localeCompare(b.path);
  });
}

function renderSkillLine(skill: Skill, description: string): string {
  return description
    ? `- ${skill.name}: ${description} (file: ${skill.path})`
    : `- ${skill.name}: (file: ${skill.path})`;
}

function renderSkillDescriptionPrefix(skill: Skill, descriptionChars: number): string {
  if (descriptionChars <= 0) return "";
  return [...skill.description].slice(0, descriptionChars).join("");
}

function lineTokenCost(line: string): number {
  return tokenCost(`${line}\n`);
}

function minimumLineTokenCost(skill: Skill): number {
  return lineTokenCost(renderSkillLine(skill, ""));
}

function fullLineTokenCost(skill: Skill): number {
  return lineTokenCost(renderSkillLine(skill, skill.description));
}

function extraDescriptionCosts(skill: Skill): number[] {
  const minimumLine = renderSkillLine(skill, "");
  const minimumBytes = Buffer.byteLength(`${minimumLine}\n`, "utf8");
  const minimumCost = Math.ceil(minimumBytes / 4);
  const costs = [0];
  let prefixBytes = 0;
  for (const char of skill.description) {
    prefixBytes += Buffer.byteLength(char, "utf8");
    const renderedBytes = minimumBytes + prefixBytes + 1;
    costs.push(Math.ceil(renderedBytes / 4) - minimumCost);
  }
  return costs;
}

function budgetedSkillCost(skills: Skill[], budgetTokens: number): {
  fullTokens: number;
  minimumTokens: number;
  budgetedTokens: number;
  includedSkills: number;
  omittedSkills: number;
  truncatedDescriptionChars: number;
  truncatedDescriptionCount: number;
} {
  const ordered = orderedSkillsForBudget(skills);
  const fullTokens = ordered.reduce((sum, s) => sum + fullLineTokenCost(s), 0);
  if (fullTokens <= budgetTokens) {
    return {
      fullTokens,
      minimumTokens: ordered.reduce((sum, s) => sum + minimumLineTokenCost(s), 0),
      budgetedTokens: fullTokens,
      includedSkills: ordered.length,
      omittedSkills: 0,
      truncatedDescriptionChars: 0,
      truncatedDescriptionCount: 0,
    };
  }

  const minimumTokens = ordered.reduce((sum, s) => sum + minimumLineTokenCost(s), 0);
  if (minimumTokens <= budgetTokens) {
    const remainingByIndex = ordered.map((s) => [...s.description].length);
    const allocatedByIndex = ordered.map(() => 0);
    const currentExtraCosts = ordered.map(() => 0);
    const extraCostsByIndex = ordered.map(extraDescriptionCosts);
    let remaining = budgetTokens - minimumTokens;
    while (true) {
      let changed = false;
      for (let i = 0; i < ordered.length; i++) {
        if (allocatedByIndex[i] >= remainingByIndex[i]) continue;
        const nextChars = allocatedByIndex[i] + 1;
        const nextCost = extraCostsByIndex[i]?.[nextChars] ?? currentExtraCosts[i];
        const delta = nextCost - currentExtraCosts[i];
        if (delta <= remaining) {
          allocatedByIndex[i] = nextChars;
          currentExtraCosts[i] = nextCost;
          remaining -= delta;
          changed = true;
        }
      }
      if (!changed) break;
    }
    const rendered = ordered.map((s, i) =>
      renderSkillLine(s, renderSkillDescriptionPrefix(s, allocatedByIndex[i] ?? 0)),
    );
    const truncatedChars = ordered.reduce(
      (sum, s, i) => sum + Math.max(0, [...s.description].length - (allocatedByIndex[i] ?? 0)), 0,
    );
    const truncatedCount = ordered.filter(
      (s, i) => (allocatedByIndex[i] ?? 0) < [...s.description].length,
    ).length;
    return {
      fullTokens,
      minimumTokens,
      budgetedTokens: rendered.reduce((sum, line) => sum + lineTokenCost(line), 0),
      includedSkills: ordered.length,
      omittedSkills: 0,
      truncatedDescriptionChars: truncatedChars,
      truncatedDescriptionCount: truncatedCount,
    };
  }

  let budgetedTokens = 0;
  let includedSkills = 0;
  let omittedSkills = 0;
  let truncatedChars = 0;
  let truncatedCount = 0;
  for (const skill of ordered) {
    const cost = minimumLineTokenCost(skill);
    if (budgetedTokens + cost <= budgetTokens) {
      budgetedTokens += cost;
      includedSkills++;
    } else {
      omittedSkills++;
    }
    const dc = [...skill.description].length;
    truncatedChars += dc;
    if (dc > 0) truncatedCount++;
  }
  return { fullTokens, minimumTokens, budgetedTokens, includedSkills, omittedSkills, truncatedDescriptionChars: truncatedChars, truncatedDescriptionCount: truncatedCount };
}

function skillBudget(skills: Skill[]): Budget {
  const context = hermesModelContext();
  const percent = numberArg(String(budgetPercent), 2);
  const renderedLineChars = skills.reduce((sum, s) => sum + s.lineChars, 0);
  const budgetTokens = Math.floor(context.tokens * (percent / 100));
  const cost = budgetedSkillCost(skills, budgetTokens);
  return {
    model: context.model,
    contextTokens: context.tokens,
    contextSource: context.source,
    budgetPercent: percent,
    budgetTokens,
    renderedLineChars,
    unbudgetedFullTokens: cost.fullTokens,
    minimumTokens: cost.minimumTokens,
    budgetedTokens: cost.budgetedTokens,
    charsPerToken: numberArg(String(charsPerToken), 4),
    unbudgetedBudgetUsedRatio: cost.fullTokens / budgetTokens,
    budgetedBudgetUsedRatio: cost.budgetedTokens / budgetTokens,
    unbudgetedContextUsedRatio: cost.fullTokens / context.tokens,
    budgetedContextUsedRatio: cost.budgetedTokens / context.tokens,
    remainingBudgetTokens: budgetTokens - cost.budgetedTokens,
    includedSkills: cost.includedSkills,
    omittedSkills: cost.omittedSkills,
    truncatedDescriptionChars: cost.truncatedDescriptionChars,
    truncatedDescriptionCount: cost.truncatedDescriptionCount,
  };
}

// ── Report Rendering ────────────────────────────────────────────────────────

function render(skills: Skill[], usage: Map<string, Usage>, logFiles: string[]): string {
  const enabled = skills; // All discovered skills are considered (no disabled concept in Hermes)
  const roots = groupBy(skills, (s) => s.root);
  const byBase = [...groupBy(enabled, (s) => s.baseName.toLowerCase()).entries()].filter(([, list]) => list.length > 1);
  const byBody = [...groupBy(enabled, (s) => s.bodyHash).entries()].filter(([hash, list]) => hash !== "811c9dc5" && list.length > 1);
  const longDescriptions = enabled
    .filter((s) => s.descChars >= 110 || s.lineChars >= 180)
    .sort((a, b) => b.descChars - a.descChars)
    .slice(0, 30);
  const unused = enabled
    .filter((s) => {
      const item = usage.get(s.name);
      return !item || item.dollar + item.fileRead + item.text === 0;
    })
    .sort((a, b) => a.scope.localeCompare(b.scope) || a.name.localeCompare(b.name))
    .slice(0, 80);
  const totalLineChars = enabled.reduce((sum, s) => sum + s.lineChars, 0);
  const totalDescChars = enabled.reduce((sum, s) => sum + s.descChars, 0);
  const budget = skillBudget(enabled);
  const lines: string[] = [];

  lines.push("# Hermes Skill Cleaner Report", "");
  lines.push(`generated: ${new Date().toISOString()}`);
  lines.push(`months: ${months}`);
  lines.push(`skills: ${skills.length} discovered`);
  lines.push(`description_chars: ${totalDescChars}`);
  lines.push(`rendered_line_chars: ${totalLineChars}`);
  lines.push(`log_files_scanned: ${logFiles.length}`, "");

  lines.push("## Skill Budget", "");
  lines.push(`model: ${budget.model}`);
  lines.push(`context_tokens: ${formatNumber(budget.contextTokens)}`);
  lines.push(`context_source: ${budget.contextSource}`);
  lines.push(`${budget.budgetPercent}%_budget_tokens: ${formatNumber(budget.budgetTokens)}`);
  lines.push(`cost_rule: ceil(utf8_bytes / ${budget.charsPerToken})`);
  lines.push(`unbudgeted_full_tokens: ${formatNumber(budget.unbudgetedFullTokens)}`);
  lines.push(`minimum_no_description_tokens: ${formatNumber(budget.minimumTokens)}`);
  lines.push(`budgeted_tokens_used: ${formatNumber(budget.budgetedTokens)}`);
  lines.push(`used_of_${budget.budgetPercent}%_budget: ${formatOnePct(budget.budgetedBudgetUsedRatio)}`);
  lines.push(`unbudgeted_used_of_${budget.budgetPercent}%_budget: ${formatOnePct(budget.unbudgetedBudgetUsedRatio)}`);
  lines.push(`used_of_context: ${formatOnePct(budget.budgetedContextUsedRatio)}`);
  lines.push(`remaining_${budget.budgetPercent}%_budget_tokens: ${formatNumber(budget.remainingBudgetTokens)}`);
  lines.push(`included_skills_after_budget: ${budget.includedSkills}`);
  lines.push(`omitted_skills_after_budget: ${budget.omittedSkills}`);
  lines.push(`truncated_description_chars: ${formatNumber(budget.truncatedDescriptionChars)}`, "");

  lines.push("## Description Candidates", "");
  for (const skill of longDescriptions) {
    lines.push(`- ${skill.name}`);
    lines.push(`  path: ${skill.path}`);
    lines.push(`  chars: description=${skill.descChars}, rendered_line=${skill.lineChars}`);
    lines.push(`  current: ${skill.description}`);
    lines.push(`  suggested: ${suggestDescription(skill)}`);
  }
  if (longDescriptions.length === 0) lines.push("- none");
  lines.push("");

  lines.push("## Duplicates By Name", "");
  for (const [name, list] of byBase.slice(0, 40)) {
    lines.push(`- ${name}`);
    const keep = preferredKeepSkill(list);
    lines.push(`  keep-default: ${keep.scope}: ${keep.path}`);
    for (const skill of list) {
      const score = skill.realPath === keep.realPath ? { body: 1, description: 1 } : similarity(keep, skill);
      lines.push(`  - ${skill.scope}: ${skill.path} (body=${formatPct(score.body)}, description=${formatPct(score.description)})`);
    }
  }
  if (byBase.length === 0) lines.push("- none");
  lines.push("");

  lines.push("## Duplicate Delete Suggestions", "");
  lines.push(...duplicateDeleteSuggestions(byBase));
  lines.push("");

  lines.push("## Duplicates By Body Hash", "");
  for (const [, list] of byBody.slice(0, 30)) {
    lines.push(`- ${list.map((s) => s.name).join(", ")}`);
    for (const skill of list) lines.push(`  - ${skill.scope}: ${skill.path}`);
  }
  if (byBody.length === 0) lines.push("- none");
  lines.push("");

  lines.push("## Unused Candidates", "");
  for (const skill of unused) {
    const item = usage.get(skill.name) ?? { dollar: 0, fileRead: 0, text: 0 };
    lines.push(`- ${skill.name}: ${skill.scope}; usage=$${item.dollar}, reads=${item.fileRead}, text=${item.text}; ${skill.path}`);
  }
  if (unused.length === 0) lines.push("- none");
  lines.push("");

  lines.push("## Root Summary", "");
  for (const [root, list] of [...roots.entries()].sort((a, b) => b[1].length - a[1].length)) {
    lines.push(`- ${root}: ${list.length} skills`);
  }
  return lines.join("\n");
}

// ── Main ────────────────────────────────────────────────────────────────────

const skills = discoverSkills();
const logFiles = recentLogFiles();
const usage = scanUsage(skills, logFiles);
const output = json
  ? JSON.stringify({ skills, usage: Object.fromEntries(usage), logFiles, budget: skillBudget(skills) }, null, 2)
  : render(skills, usage, logFiles);
console.log(output);

const PROFILE_COLORS = [
  "#d9485f",
  "#8b5cf6",
  "#0f9d8a",
  "#e07a2f",
  "#2563eb",
  "#b45309",
  "#be3f8f",
  "#4f7d22",
] as const;

function normalizedProfile(profile: string | null | undefined): string {
  return (profile ?? "").trim();
}

/** A compact, human-readable identity derived from the selected profile slug. */
export function agentDisplayName(profile: string | null | undefined): string {
  const normalized = normalizedProfile(profile);
  if (!normalized || normalized === "default") return "Hermes";

  return normalized
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(" ");
}

function colorForProfile(profile: string | null | undefined): string {
  const normalized = normalizedProfile(profile).toLowerCase() || "default";
  let hash = 0;
  for (const char of normalized) {
    hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  }
  return PROFILE_COLORS[hash % PROFILE_COLORS.length];
}

/** A profile-specific SVG favicon with the agent's initial and stable color. */
export function profileFaviconHref(profile: string | null | undefined): string {
  const name = agentDisplayName(profile);
  const initial = name.charAt(0).toUpperCase() || "H";
  const color = colorForProfile(profile);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="${color}"/><text x="32" y="41" text-anchor="middle" font-family="system-ui, sans-serif" font-size="34" font-weight="700" fill="white">${initial}</text></svg>`;

  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Browser-tab title for an agent's currently selected chat. */
export function profileTabTitle(
  profile: string | null | undefined,
  sessionTitle: string | null | undefined,
): string {
  const title = sessionTitle?.trim() || "Untitled";
  return `${agentDisplayName(profile)} - ${title} - Hermes`;
}

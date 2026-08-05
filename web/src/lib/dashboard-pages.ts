import type { DashboardPage } from "./api";

export interface DashboardContext {
  active: DashboardPage;
  group: DashboardPage["group"];
  pages: DashboardPage[];
}

/** Resolve the active dashboard section for a right-side contextual rail. */
export function resolveDashboardContext(
  pages: DashboardPage[],
  pathname: string,
): DashboardContext | null {
  const normalized = pathname.replace(/\/$/, "") || "/";
  const active = [...pages]
    .sort((a, b) => b.path.length - a.path.length)
    .find(
      (page) =>
        normalized === page.path || normalized.startsWith(`${page.path}/`),
    );

  // Workspace pages are primary destinations, not nested settings. Chat also
  // owns a model/tools inspector on the right; never stack or clutter rails.
  if (!active || active.group === "workspace") return null;

  const siblings = pages.filter((page) => page.group === active.group);
  if (siblings.length < 2) return null;

  return { active, group: active.group, pages: siblings };
}

const SENSITIVE_QUERY_KEYS = new Set([
  "token",
  "access_token",
  "refresh_token",
  "id_token",
  "code",
  "state",
  "secret",
  "api_key",
  "apikey",
  "key",
]);

const NON_PAGE_PREFIXES = [
  "/api",
  "/assets",
  "/auth",
  "/dashboard-plugins",
];

export type DashboardLinkResolution =
  | { kind: "internal"; route: string }
  | { kind: "external"; url: string }
  | { kind: "reject" };

/** Classify a terminal hyperlink without conflating unsafe and external URLs. */
export function classifyDashboardLink(
  rawUrl: string,
  currentHref: string,
  basePath = "",
): DashboardLinkResolution {
  try {
    const current = new URL(currentHref);
    const target = new URL(rawUrl, current);

    if (!["http:", "https:"].includes(target.protocol)) return { kind: "reject" };
    if (target.username || target.password) return { kind: "reject" };
    for (const key of target.searchParams.keys()) {
      if (SENSITIVE_QUERY_KEYS.has(key.toLowerCase())) return { kind: "reject" };
    }
    if (target.origin !== current.origin) {
      return { kind: "external", url: target.href };
    }

    const path = target.pathname || "/";
    const normalizedBase = basePath
      ? `/${basePath.replace(/^\/+|\/+$/g, "")}`
      : "";
    if (
      normalizedBase &&
      path !== normalizedBase &&
      !path.startsWith(`${normalizedBase}/`)
    ) {
      return { kind: "reject" };
    }
    const routerPath = normalizedBase
      ? path.slice(normalizedBase.length) || "/"
      : path;

    if (
      NON_PAGE_PREFIXES.some(
        (prefix) =>
          routerPath === prefix || routerPath.startsWith(`${prefix}/`),
      )
    ) {
      return { kind: "reject" };
    }

    return {
      kind: "internal",
      route: `${routerPath}${target.search}${target.hash}`,
    };
  } catch {
    return { kind: "reject" };
  }
}

/** Resolve only safe same-origin SPA links for React Router. */
export function resolveDashboardRoute(
  rawUrl: string,
  currentHref: string,
  basePath = "",
): string | null {
  const resolution = classifyDashboardLink(rawUrl, currentHref, basePath);
  return resolution.kind === "internal" ? resolution.route : null;
}

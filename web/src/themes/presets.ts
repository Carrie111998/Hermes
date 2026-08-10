import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `hermes_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const CHATGPT_SANS =
  '-apple-system-body, ui-sans-serif, -apple-system, system-ui, "Segoe UI", Helvetica, "Apple Color Emoji", Arial, sans-serif, "Segoe UI Emoji", "Segoe UI Symbol"';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.55",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

/**
 * Hermes components intentionally carry a strong display identity through
 * utility classes such as font-expanded/font-compressed, uppercase labels,
 * wide tracking, and square bordered panels.  Palette tokens alone cannot
 * neutralize those component-level declarations, so the ChatGPT themes use
 * the theme system's scoped CSS layer to make the rendered controls consume
 * the ChatGPT typography and shape language.  The style element is removed
 * automatically whenever another theme becomes active.
 */
const CHATGPT_COMPONENT_CSS = `
  #root .font-expanded,
  #root .font-compressed,
  #root .font-mondwest,
  #root .font-display {
    font-family: var(--theme-font-sans) !important;
    font-stretch: normal !important;
    letter-spacing: normal !important;
  }

  #root {
    font-weight: 450;
  }

  #app-sidebar nav a,
  #app-sidebar nav button,
  #app-sidebar [class*="tracking-"],
  #app-sidebar [class*="uppercase"] {
    font-family: var(--theme-font-sans) !important;
    font-size: 0.9375rem !important;
    font-weight: 450 !important;
    letter-spacing: normal !important;
    text-transform: none !important;
  }

  #app-sidebar nav a {
    margin-inline: 0.5rem;
    padding-inline: 0.75rem;
    border-radius: 0.5rem;
  }

  header h1,
  main h1,
  main h2,
  main h3,
  main [class*="tracking-"] {
    font-family: var(--theme-font-sans) !important;
    font-stretch: normal !important;
    letter-spacing: normal !important;
    text-transform: none !important;
  }

  header h1 {
    font-size: 1.125rem !important;
    font-weight: 600 !important;
    line-height: 1.5rem !important;
  }

  main p,
  main li,
  main td,
  main th,
  main label,
  main input,
  main textarea,
  main select {
    font-weight: 450;
  }

  main button,
  #app-sidebar button {
    font-weight: 500;
  }

  main .font-compressed,
  main .font-mondwest {
    font-weight: 500 !important;
  }

  main .border {
    border-color: var(--color-border) !important;
    border-radius: var(--radius) !important;
  }

  main div.border:not([role="radiogroup"]),
  main section.border,
  main article.border {
    background-color: var(--color-card) !important;
  }

  main button,
  main input,
  main textarea,
  main select,
  [role="dialog"],
  [role="listbox"] {
    border-radius: var(--radius) !important;
  }

  main pre,
  main code,
  main kbd {
    letter-spacing: normal !important;
  }
`;

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Hermes Teal",
  description: "Classic dark teal — the canonical Hermes look",
  palette: {
    background: { hex: "#041c1c", alpha: 1 },
    midground: { hex: "#ffe6cb", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 189, 56, 0.35)",
    noiseOpacity: 1,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
  terminalBackground: "#000000",
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.75rem",
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.25rem",
  },
  colorOverrides: {
    destructive: "#c92d0f",
    warning: "#f97316",
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "1rem",
  },
};

/** Light mode — vivid Nous-blue accents on a cream canvas. */
export const nousBlueTheme: DashboardTheme = {
  name: "nous-blue",
  label: "Nous Blue",
  description: "Light mode — vivid Nous-blue accents on cream canvas",
  palette: {
    background: { hex: "#E8F2FD", alpha: 1 },
    midground: { hex: "#0053FD", alpha: 1 },
    foreground: { hex: "#170d02", alpha: 0 },
    warmGlow: "rgba(0, 83, 253, 0.12)",
    noiseOpacity: 0,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
  terminalBackground: "#f5f8fc",
  terminalForeground: "#170d02",
  seriesColors: {
    inputTokenAccent: "#001934",
    outputTokenAccent: "#0053fd",
  },
  swatchColors: ["#170d02", "#0053FD", "#E8F2FD"],
};

/**
 * Same look as ``defaultTheme`` but with a larger root font size, looser
 * line-height, and ``spacious`` density so every rem-based size in the
 * dashboard scales up. For users who find the default 15px UI too dense.
 */
export const defaultLargeTheme: DashboardTheme = {
  name: "default-large",
  label: "Hermes Teal (Large)",
  description: "Hermes Teal with bigger fonts and roomier spacing",
  palette: defaultTheme.palette,
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    baseSize: "18px",
    lineHeight: "1.65",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    density: "spacious",
  },
};

export const chatgptDarkTheme: DashboardTheme = {
  name: "chatgpt-dark",
  label: "ChatGPT Dark",
  description: "OpenAI-inspired neutral dark theme",
  palette: {
    background: { hex: "#212121", alpha: 1 },
    midground: { hex: "#ECECEC", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255,255,255,0)",
    noiseOpacity: 0,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: CHATGPT_SANS,
    fontMono: `"SF Mono", ${SYSTEM_MONO}`,
    baseSize: "17px",
    lineHeight: "1.5",
    letterSpacing: "0",
  },
  layout: { ...DEFAULT_LAYOUT, radius: "0.625rem" },
  componentStyles: {
    sidebar: {
      background: "#171717",
      boxShadow: "1px 0 0 rgba(255, 255, 255, 0.05)",
    },
  },
  customCSS: CHATGPT_COMPONENT_CSS,
  terminalBackground: "#212121",
  terminalForeground: "#ECECEC",
  colorOverrides: {
    card: "#2F2F2F",
    cardForeground: "#ECECEC",

    popover: "#303030",
    popoverForeground: "#ECECEC",

    primary: "#ECECEC",
    primaryForeground: "#0D0D0D",

    secondary: "#303030",
    secondaryForeground: "#ECECEC",

    muted: "#2F2F2F",
    mutedForeground: "#CDCDCD",

    accent: "#414141",
    accentForeground: "#ECECEC",

    destructive: "#E86872",
    destructiveForeground: "#FFFFFF",

    success: "#57C785",

    /* Hermes gold/yellow -> warm OpenAI-style off-white */
    warning: "#F1EEE7",

    border: "#525252",
    input: "#414141",
    ring: "#9B9B9B",
  },
  seriesColors: {
    inputTokenAccent: "#F1EEE7",
    outputTokenAccent: "#9B9B9B",
  },
  swatchColors: ["#212121", "#ECECEC", "#414141"],
};


export const chatgptLightTheme: DashboardTheme = {
  name: "chatgpt-light",
  label: "ChatGPT Light",
  description: "ChatGPT's neutral light interface palette",
  palette: {
    /* Current ChatGPT neutral canvas and primary text. */
    background: { hex: "#FCFCFC", alpha: 1 },
    midground: { hex: "#0D0D0D", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0,0,0,0)",
    noiseOpacity: 0,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: CHATGPT_SANS,
    fontMono: `"SF Mono", ${SYSTEM_MONO}`,
    baseSize: "17px",
    lineHeight: "1.5",
    letterSpacing: "0",
  },
  layout: { ...DEFAULT_LAYOUT, radius: "0.625rem" },
  componentStyles: {
    sidebar: {
      background: "#F9F9F9",
      boxShadow: "1px 0 0 rgba(0, 0, 0, 0.05)",
    },
  },
  customCSS: CHATGPT_COMPONENT_CSS,
  terminalBackground: "#FCFCFC",
  terminalForeground: "#0D0D0D",
  colorOverrides: {
    /* Elevated surfaces sit one step above the #fcfcfc canvas. */
    card: "#FFFFFF",
    cardForeground: "#0D0D0D",

    popover: "#FFFFFF",
    popoverForeground: "#0D0D0D",

    primary: "#0D0D0D",
    primaryForeground: "#FFFFFF",

    secondary: "#E8E8E8",
    secondaryForeground: "#0D0D0D",

    muted: "#F3F3F3",
    mutedForeground: "#5D5D5D",

    accent: "#ECECEC",
    accentForeground: "#0D0D0D",

    destructive: "#D94B56",
    destructiveForeground: "#FFFFFF",

    success: "#2F9D65",

    /* Warm neutral replacement for Hermes yellow */
    warning: "#71695C",

    border: "#E6E6E6",
    input: "#D8D8D8",
    ring: "#676767",
  },
  seriesColors: {
    inputTokenAccent: "#6C6458",
    outputTokenAccent: "#555555",
  },
  swatchColors: ["#FCFCFC", "#0D0D0D", "#ECECEC"],
};

export const chatgptAutoTheme: DashboardTheme = {
  // Virtual built-in resolved by ThemeProvider to the current system variant.
  name: "chatgpt-auto",
  label: "ChatGPT Auto",
  description: "Follows your system light/dark appearance automatically",
  palette: chatgptDarkTheme.palette,
  typography: chatgptDarkTheme.typography,
  layout: chatgptDarkTheme.layout,
  terminalBackground: chatgptDarkTheme.terminalBackground,
  terminalForeground: chatgptDarkTheme.terminalForeground,
  colorOverrides: chatgptDarkTheme.colorOverrides,
  componentStyles: chatgptDarkTheme.componentStyles,
  customCSS: CHATGPT_COMPONENT_CSS,
  seriesColors: chatgptDarkTheme.seriesColors,
  swatchColors: ["#171717", "#F7F7F5", "#AFAFAF"],
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  default: defaultTheme,
  "default-large": defaultLargeTheme,
  "nous-blue": nousBlueTheme,

  "chatgpt-auto": chatgptAutoTheme,
  "chatgpt-dark": chatgptDarkTheme,
  "chatgpt-light": chatgptLightTheme,

  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
};

#!/usr/bin/env python3
"""Apply all welcome banner config patches."""
import os

REPO = os.path.dirname(os.path.abspath(__file__))
PATHS = {
    'gatewayTypes.ts': 'ui-tui/src/gatewayTypes.ts',
    'types.ts': 'ui-tui/src/types.ts',
    'interfaces.ts': 'ui-tui/src/app/interfaces.ts',
    'uiStore.ts': 'ui-tui/src/app/uiStore.ts',
    'details.ts': 'ui-tui/src/domain/details.ts',
    'useConfigSync.ts': 'ui-tui/src/app/useConfigSync.ts',
    'branding.tsx': 'ui-tui/src/components/branding.tsx',
    'appLayout.tsx': 'ui-tui/src/components/appLayout.tsx',
    'config.yaml': 'cli-config.yaml.example',
}

def read(name):
    with open(os.path.join(REPO, PATHS[name])) as f:
        return f.read()

def write(name, content):
    with open(os.path.join(REPO, PATHS[name]), 'w') as f:
        f.write(content)
    print(f'  OK {PATHS[name]}')

# 1. gatewayTypes.ts
content = read('gatewayTypes.ts')
old = (
    '  /** Theme mode pin: \'light\' / \'dark\' beat background auto-detection; \'auto\'\n'
    '   *  (default) trusts the OSC-11 probe + env signals. */\n'
    '  tui_theme?: string\n'
    '}'
)
new = old + (
    '\n'
    '\n'
    '  /**\n'
    '   * Welcome banner section configuration.\n'
    '   *\n'
    '   * Controls which accordion sections appear in the TUI welcome panel, their\n'
    '   * default open/closed state, and custom plugin-provided sections.\n'
    '   *\n'
    '   * Sections omitted from config use their built-in defaults (tools=open,\n'
    '   * skills=closed, system_prompt=closed, mcp_servers=closed). Set\n'
    '   * `enabled: false` to hide a section entirely.\n'
    '   *\n'
    '   * `plugin_sections` renders custom Accordion sections after the built-in\n'
    '   * ones. Data for plugin sections is expected on SessionInfo under the\n'
    '   * matching key (future - currently renders a placeholder label).\n'
    '   */\n'
    '  welcome_banner?: {\n'
    '    sections?: Record<string, { default_open?: boolean; enabled?: boolean }>\n'
    '    plugin_sections?: Array<{ id: string; title: string; default_open?: boolean }>\n'
    '  }\n'
    '}'
)
assert old in content, 'gatewayTypes.ts: old not found'
content = content.replace(old, new, 1)
write('gatewayTypes.ts', content)

# 2. types.ts
content = read('types.ts')
old2 = (
    'export interface SlashCatalog {\n'
    '  canon: Record<string, string>\n'
    '  categories: SlashCategory[]\n'
    '  pairs: [string, string][]\n'
    '  skillCount: number\n'
    '  sub: Record<string, string[]>\n'
    '}'
)
new2 = (
    old2 + '\n'
    '\n'
    '/**\n'
    ' * Welcome banner section config as resolved from display.welcome_banner.\n'
    ' * Each built-in section (tools, skills, system_prompt, mcp_servers) can be\n'
    ' * independently enabled/disabled and default-open/closed.\n'
    ' *\n'
    ' * Plugin sections render custom Accordion entries after built-in sections.\n'
    ' */\n'
    'export interface WelcomeBannerSectionConfig {\n'
    '  default_open: boolean\n'
    '  enabled: boolean\n'
    '}\n'
    '\n'
    'export interface WelcomeBannerPluginSection {\n'
    '  id: string\n'
    '  title: string\n'
    '  default_open: boolean\n'
    '}\n'
    '\n'
    'export interface WelcomeBannerConfig {\n'
    '  sections: Record<string, WelcomeBannerSectionConfig>\n'
    '  plugin_sections: WelcomeBannerPluginSection[]\n'
    '}\n'
    '\n'
    '/**\n'
    ' * Welcome banner section default defaults for each built-in section.\n'
    ' * These match the hardcoded values in branding.tsx before this feature.\n'
    ' */\n'
    'export const WELCOME_BANNER_DEFAULTS: Record<string, WelcomeBannerSectionConfig> = {\n'
    '  tools: { default_open: true, enabled: true },\n'
    '  skills: { default_open: false, enabled: true },\n'
    '  system_prompt: { default_open: false, enabled: true },\n'
    '  mcp_servers: { default_open: false, enabled: true }\n'
    '}'
)
assert old2 in content, 'types.ts: old not found'
content = content.replace(old2, new2, 1)
write('types.ts', content)

# 3. interfaces.ts
content = read('interfaces.ts')
old3 = (
    "import type {\n"
    "  ApprovalReq,\n"
    "  ClarifyReq,\n"
    "  ConfirmReq,\n"
    "  DetailsMode,\n"
    "  Msg,\n"
    "  PanelSection,\n"
    "  SecretReq,\n"
    "  SectionVisibility,\n"
    "  SessionInfo,\n"
    "  SlashCatalog,\n"
    "  SudoReq,\n"
    "  Usage\n"
    "} from '../types.js'"
)
new3 = (
    "import type {\n"
    "  ApprovalReq,\n"
    "  ClarifyReq,\n"
    "  ConfirmReq,\n"
    "  DetailsMode,\n"
    "  Msg,\n"
    "  PanelSection,\n"
    "  SecretReq,\n"
    "  SectionVisibility,\n"
    "  SessionInfo,\n"
    "  SlashCatalog,\n"
    "  SudoReq,\n"
    "  Usage,\n"
    "  WelcomeBannerConfig\n"
    "} from '../types.js'"
)
assert old3 in content, 'interfaces.ts import: old not found'
content = content.replace(old3, new3, 1)

old3b = (
    "  streaming: boolean\n"
    "  theme: Theme\n"
    "  usage: Usage"
)
new3b = (
    "  streaming: boolean\n"
    "  theme: Theme\n"
    "\n"
    "  /** Resolved welcome banner section configuration. */\n"
    "  welcomeBanner: WelcomeBannerConfig\n"
    "  usage: Usage"
)
assert old3b in content, 'interfaces.ts field: old not found'
content = content.replace(old3b, new3b, 1)
write('interfaces.ts', content)

# 4. uiStore.ts
content = read('uiStore.ts')
old4 = (
    "import { DEFAULT_THEME } from '../theme.js'\n"
    "\n"
    "import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'"
)
new4 = (
    "import { DEFAULT_THEME } from '../theme.js'\n"
    "import { WELCOME_BANNER_DEFAULTS } from '../types.js'\n"
    "import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'"
)
assert old4 in content, 'uiStore.ts import: old not found'
content = content.replace(old4, new4, 1)

old4b = (
    "  theme: bootTheme ?? DEFAULT_THEME,\n"
    "  usage: ZERO\n"
    "})"
)
new4b = (
    "  theme: bootTheme ?? DEFAULT_THEME,\n"
    "  usage: ZERO,\n"
    "  welcomeBanner: { sections: { ...WELCOME_BANNER_DEFAULTS }, plugin_sections: [] }\n"
    "})"
)
assert old4b in content, 'uiStore.ts default: old not found'
content = content.replace(old4b, new4b, 1)
write('uiStore.ts', content)

# 5. details.ts
content = read('details.ts')
old5 = "import type { DetailsMode, SectionName, SectionVisibility } from '../types.js'"
new5 = old5 + "\nimport type { WelcomeBannerConfig, WelcomeBannerPluginSection, WelcomeBannerSectionConfig } from '../types.js'\nimport { WELCOME_BANNER_DEFAULTS } from '../types.js'"
assert old5 in content, 'details.ts import: old not found'
content = content.replace(old5, new5, 1)

old5b = "export const nextDetailsMode = (m: DetailsMode): DetailsMode => MODES[(MODES.indexOf(m) + 1) % MODES.length]!"
new5b = (
    "export const nextDetailsMode = (m: DetailsMode): DetailsMode => MODES[(MODES.indexOf(m) + 1) % MODES.length]\n"
    "\n"
    "/**\n"
    " * Build a WelcomeBannerConfig from the raw display.welcome_banner blob.\n"
    " * Merges user overrides atop the built-in defaults, dropping unknown keys.\n"
    " * Plugin sections are validated for required fields.\n"
    " */\n"
    "export const resolveWelcomeBanner = (raw: unknown): WelcomeBannerConfig => {\n"
    "  const sections: Record<string, WelcomeBannerSectionConfig> = { ...WELCOME_BANNER_DEFAULTS }\n"
    "  const plugin_sections: WelcomeBannerPluginSection[] = []\n"
    "\n"
    "  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {\n"
    "    return { sections, plugin_sections }\n"
    "  }\n"
    "\n"
    "  const cfg = raw as Record<string, unknown>\n"
    "\n"
    "  // Merge user section overrides\n"
    "  if (cfg.sections && typeof cfg.sections === 'object' && !Array.isArray(cfg.sections)) {\n"
    "    for (const [key, val] of Object.entries(cfg.sections as Record<string, unknown>)) {\n"
    "      if (!sections[key]) continue\n"
    "      if (val && typeof val === 'object' && !Array.isArray(val)) {\n"
    "        const entry = val as Record<string, unknown>\n"
    "        if (typeof entry.default_open === 'boolean') {\n"
    "          sections[key] = { ...sections[key], default_open: entry.default_open }\n"
    "        }\n"
    "        if (typeof entry.enabled === 'boolean') {\n"
    "          sections[key] = { ...sections[key], enabled: entry.enabled }\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "\n"
    "  if (cfg.plugin_sections && Array.isArray(cfg.plugin_sections)) {\n"
    "    for (const item of cfg.plugin_sections) {\n"
    "      if (item && typeof item === 'object' && typeof item.id === 'string' && typeof item.title === 'string') {\n"
    "        plugin_sections.push({\n"
    "          id: item.id,\n"
    "          title: item.title,\n"
    "          default_open: typeof item.default_open === 'boolean' ? item.default_open : false\n"
    "        })\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "\n"
    "  return { sections, plugin_sections }\n"
    "}"
)
assert old5b in content, 'details.ts resolver: old not found'
content = content.replace(old5b, new5b, 1)
write('details.ts', content)

# 6. useConfigSync.ts
content = read('useConfigSync.ts')
old6 = "import { resolveDetailsMode, resolveSections } from '../domain/details.js'"
new6 = "import { resolveDetailsMode, resolveSections, resolveWelcomeBanner } from '../domain/details.js'"
assert old6 in content, 'useConfigSync.ts import: old not found'
content = content.replace(old6, new6, 1)

old6b = (
    "    sections: resolveSections(d.sections),\n"
    "    showReasoning: !!d.show_reasoning,\n"
    "    statusBar: normalizeStatusBar(d.tui_statusbar),\n"
    "    streaming: d.streaming !== false\n"
    "  })"
)
new6b = (
    "    sections: resolveSections(d.sections),\n"
    "    showReasoning: !!d.show_reasoning,\n"
    "    statusBar: normalizeStatusBar(d.tui_statusbar),\n"
    "    streaming: d.streaming !== false,\n"
    "    welcomeBanner: resolveWelcomeBanner(d.welcome_banner)\n"
    "  })"
)
assert old6b in content, 'useConfigSync.ts patch: old not found'
content = content.replace(old6b, new6b, 1)
write('useConfigSync.ts', content)

# 7. branding.tsx
content = read('branding.tsx')

old7a = "import type { Theme } from '../theme.js'\nimport type { PanelSection, SessionInfo } from '../types.js'"
new7a = "import type { Theme } from '../theme.js'\nimport type { PanelSection, SessionInfo, WelcomeBannerConfig } from '../types.js'"
assert old7a in content, 'branding.tsx import: old not found'
content = content.replace(old7a, new7a, 1)

old7b = 'export function SessionPanel({ info, maxWidth, sid, t }: SessionPanelProps) {'
new7b = 'export function SessionPanel({ info, maxWidth, sid, t, welcomeBanner }: SessionPanelProps) {'
assert old7b in content, 'branding.tsx sig: old not found'
content = content.replace(old7b, new7b, 1)

old7c = (
    "  // ── Local collapse state for each section ──\n"
    "  const [toolsOpen, setToolsOpen] = useState(true)\n"
    "  const [skillsOpen, setSkillsOpen] = useState(false)\n"
    "  const [systemOpen, setSystemOpen] = useState(false)\n"
    "  const [mcpOpen, setMcpOpen] = useState(false)"
)
new7c = (
    "  // ── Collapse state for each section, driven by config defaults ──\n"
    "  const [toolsOpen, setToolsOpen] = useState(welcomeBanner.sections.tools?.default_open ?? true)\n"
    "  const [skillsOpen, setSkillsOpen] = useState(welcomeBanner.sections.skills?.default_open ?? false)\n"
    "  const [systemOpen, setSystemOpen] = useState(welcomeBanner.sections.system_prompt?.default_open ?? false)\n"
    "  const [mcpOpen, setMcpOpen] = useState(welcomeBanner.sections.mcp_servers?.default_open ?? false)\n"
    "  // Dynamic state for plugin sections\n"
    "  const [pluginOpen, setPluginOpen] = useState<Record<string, boolean>>(() =>\n"
    "    Object.fromEntries(\n"
    "      welcomeBanner.plugin_sections.map(s => [s.id, s.default_open])\n"
    "    )\n"
    "  )"
)
assert old7c in content, 'branding.tsx state: old not found'
content = content.replace(old7c, new7c, 1)

old7d = (
    "      {/* ── Tools (expanded by default) ── */}\n"
    '      <Box flexDirection="column" marginTop={1}>\n'
    "        <Accordion onToggle={() => setToolsOpen(v => !v)} open={toolsOpen} t={t} title=\"Available Tools\">\n"
    "          {toolsBody()}\n"
    "        </Accordion>\n"
    "      </Box>\n"
    "\n"
    "      {/* ── Skills (collapsed by default) ── */}\n"
    '      <Box flexDirection="column" marginTop={1}>\n'
    "        <Accordion\n"
    "          count={skillsTotal}\n"
    "          onToggle={() => setSkillsOpen(v => !v)}\n"
    "          open={skillsOpen}\n"
    "          suffix={skillsCatCount > 0 ? `in ${skillsCatCount} categor${skillsCatCount === 1 ? 'y' : 'ies'}` : undefined}\n"
    "          t={t}\n"
    "          title=\"Available Skills\"\n"
    "        >\n"
    "          {skillsBody()}\n"
    "        </Accordion>\n"
    "      </Box>\n"
    "\n"
    "      {/* ── System Prompt (collapsed by default) ── */}\n"
    "      {sysPromptLen > 0 && (\n"
    '        <Box flexDirection="column" marginTop={1}>\n'
    "          <Accordion\n"
    "            onToggle={() => setSystemOpen(v => !v)}\n"
    "            open={systemOpen}\n"
    "            suffix={`— ${sysPromptLen.toLocaleString()} chars`}\n"
    "            t={t}\n"
    "            title=\"System Prompt\"\n"
    "          >\n"
    "            {systemBody()}\n"
    "          </Accordion>\n"
    "        </Box>\n"
    "      )}\n"
    "\n"
    "      {/* ── MCP Servers (collapsed by default) ── */}\n"
    "      {mcpServers.length > 0 && (\n"
    '        <Box flexDirection="column" marginTop={1}>\n'
    "          <Accordion\n"
    "            count={mcpConnected}\n"
    "            onToggle={() => setMcpOpen(v => !v)}\n"
    "            open={mcpOpen}\n"
    '            suffix="connected"\n'
    "            t={t}\n"
    "            title=\"MCP Servers\"\n"
    "          >\n"
    "            {mcpBody()}\n"
    "          </Accordion>\n"
    "        </Box>\n"
    "      )}"
)
new7d = (
    "      {/* ── Tools (default: expanded) ── */}\n"
    "      {welcomeBanner.sections.tools?.enabled !== false && (\n"
    '        <Box flexDirection="column" marginTop={1}>\n'
    "        <Accordion onToggle={() => setToolsOpen(v => !v)} open={toolsOpen} t={t} title=\"Available Tools\">\n"
    "          {toolsBody()}\n"
    "        </Accordion>\n"
    "      </Box>\n"
    "      )}\n"
    "\n"
    "      {/* ── Skills (default: collapsed) ── */}\n"
    "      {welcomeBanner.sections.skills?.enabled !== false && (\n"
    '        <Box flexDirection="column" marginTop={1}>\n'
    "        <Accordion\n"
    "          count={skillsTotal}\n"
    "          onToggle={() => setSkillsOpen(v => !v)}\n"
    "          open={skillsOpen}\n"
    "          suffix={skillsCatCount > 0 ? `in ${skillsCatCount} categor${skillsCatCount === 1 ? 'y' : 'ies'}` : undefined}\n"
    "          t={t}\n"
    "          title=\"Available Skills\"\n"
    "        >\n"
    "          {skillsBody()}\n"
    "        </Accordion>\n"
    "      </Box>\n"
    "      )}\n"
    "\n"
    "      {/* ── System Prompt (default: collapsed) ── */}\n"
    "      {sysPromptLen > 0 && welcomeBanner.sections.system_prompt?.enabled !== false && (\n"
    '        <Box flexDirection="column" marginTop={1}>\n'
    "        <Accordion\n"
    "          onToggle={() => setSystemOpen(v => !v)}\n"
    "          open={systemOpen}\n"
    "          suffix={`— ${sysPromptLen.toLocaleString()} chars`}\n"
    "          t={t}\n"
    "          title=\"System Prompt\"\n"
    "        >\n"
    "          {systemBody()}\n"
    "        </Accordion>\n"
    "      </Box>\n"
    "      )}\n"
    "\n"
    "      {/* ── MCP Servers (default: collapsed) ── */}\n"
    "      {mcpServers.length > 0 && welcomeBanner.sections.mcp_servers?.enabled !== false && (\n"
    '        <Box flexDirection="column" marginTop={1}>\n'
    "        <Accordion\n"
    "          count={mcpConnected}\n"
    "          onToggle={() => setMcpOpen(v => !v)}\n"
    "          open={mcpOpen}\n"
    '          suffix="connected"\n'
    "          t={t}\n"
    "          title=\"MCP Servers\"\n"
    "        >\n"
    "          {mcpBody()}\n"
    "        </Accordion>\n"
    "      </Box>\n"
    "      )}\n"
    "\n"
    "      {/* ── Plugin sections ── */}\n"
    "      {welcomeBanner.plugin_sections.map(ps => {\n"
    "        const isOpen = pluginOpen[ps.id] ?? ps.default_open\n"
    "        const toggle = () => setPluginOpen(p => ({ ...p, [ps.id]: !(p[ps.id] ?? ps.default_open) }))\n"
    "        return (\n"
    '          <Box flexDirection="column" key={ps.id} marginTop={1}>\n'
    "            <Accordion onToggle={toggle} open={isOpen} t={t} title={ps.title}>\n"
    "              <Text color={t.color.muted}>\n"
    '                Plugin section \u2014 data binding for &ldquo;{ps.title}&rdquo; is\n'
    "                not yet wired on the gateway side.\n"
    "              </Text>\n"
    "            </Accordion>\n"
    "          </Box>\n"
    "        )\n"
    "      })}\n"
)
assert old7d in content, 'branding.tsx sections: old not found'
content = content.replace(old7d, new7d, 1)

old7e = (
    "interface SessionPanelProps {\n"
    "  info: SessionInfo\n"
    "  maxWidth?: number\n"
    "  sid?: string | null\n"
    "  t: Theme\n"
    "}"
)
new7e = (
    "interface SessionPanelProps {\n"
    "  info: SessionInfo\n"
    "  maxWidth?: number\n"
    "  sid?: string | null\n"
    "  t: Theme\n"
    "  welcomeBanner: WelcomeBannerConfig\n"
    "}"
)
assert old7e in content, 'branding.tsx props: old not found'
content = content.replace(old7e, new7e, 1)
write('branding.tsx', content)

# 8. appLayout.tsx
content = read('appLayout.tsx')
old8 = (
    "                  {row.msg.info && (\n"
    "                    <SessionPanel\n"
    "                      info={row.msg.info}\n"
    "                      maxWidth={Math.max(1, composer.cols - 2)}\n"
    "                      sid={ui.sid}\n"
    "                      t={ui.theme}\n"
    "                    />\n"
    "                  )}"
)
new8 = (
    "                  {row.msg.info && (\n"
    "                    <SessionPanel\n"
    "                      info={row.msg.info}\n"
    "                      maxWidth={Math.max(1, composer.cols - 2)}\n"
    "                      sid={ui.sid}\n"
    "                      t={ui.theme}\n"
    "                      welcomeBanner={ui.welcomeBanner}\n"
    "                    />\n"
    "                  )}"
)
assert old8 in content, 'appLayout.tsx: old not found'
content = content.replace(old8, new8, 1)
write('appLayout.tsx', content)

# 9. cli-config.yaml.example
content = read('config.yaml')
old9 = (
    "  # timestamps: false\n"
    "\n"
    "  # ───────────────────────────────────────────────────────────────────────────\n"
    "  # Skin / Theme\n"
    "  # ───────────────────────────────────────────────────────────────────────────"
)
new9 = (
    "  # timestamps: false\n"
    "\n"
    "  # ───────────────────────────────────────────────────────────────────────────\n"
    "  # TUI Welcome Banner Sections\n"
    "  # ───────────────────────────────────────────────────────────────────────────\n"
    '  # Configure which accordion sections appear in the TUI welcome panel and their\n'
    "  # default open/closed state. Sections omitted from config use built-in defaults\n"
    "  # (tools=open, skills=closed, system_prompt=closed, mcp_servers=closed).\n"
    "  #\n"
    "  # Each section supports:\n"
    "  #   default_open: true|false - initial collapse state\n"
    "  #   enabled: true|false     - show or hide the section entirely\n"
    "  #\n"
    "  # Plugin sections render custom Accordion entries after the built-in sections.\n"
    "  # Data binding for plugin section content is a future extension (currently\n"
    "  # shows a placeholder label).\n"
    "  #\n"
    "  # Example:\n"
    "  #   welcome_banner:\n"
    "  #     sections:\n"
    "  #       tools:\n"
    "  #         default_open: true\n"
    "  #         enabled: true\n"
    "  #       skills:\n"
    "  #         default_open: false\n"
    "  #         enabled: true\n"
    "  #       system_prompt:\n"
    "  #         default_open: false\n"
    "  #         enabled: true\n"
    "  #       mcp_servers:\n"
    "  #         default_open: false\n"
    "  #         enabled: true\n"
    "  #     plugin_sections:\n"
    '  #       - id: my_custom_data\n'
    '  #         title: "Custom Data"\n'
    "  #         default_open: false\n"
    "\n"
    "  # ───────────────────────────────────────────────────────────────────────────\n"
    "  # Skin / Theme\n"
    "  # ───────────────────────────────────────────────────────────────────────────"
)
assert old9 in content, 'config.yaml: old not found'
content = content.replace(old9, new9, 1)
write('config.yaml', content)

print('\nAll 9 files patched successfully!')

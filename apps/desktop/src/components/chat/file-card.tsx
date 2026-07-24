import { useCallback } from 'react'

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger
} from '@/components/ui/context-menu'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { readDesktopFileText } from '@/lib/desktop-fs'
import { BarChart3, ExternalLink, FileImage, FileText, Globe, ImageIcon } from '@/lib/icons'
import { localPreviewTarget, normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { notifyError } from '@/store/notifications'
import { openPreview } from '@/store/preview'

const FILE_ICONS: Record<string, typeof FileText> = {
  md: FileText,
  html: Globe,
  htm: Globe,
  pptx: FileImage,
  ppt: FileImage,
  xlsx: BarChart3,
  xls: BarChart3,
  docx: FileText,
  doc: FileText,
  pdf: FileText,
  png: ImageIcon,
  jpg: ImageIcon,
  jpeg: ImageIcon,
  gif: ImageIcon,
  svg: FileText,
  json: FileText,
  csv: BarChart3,
  txt: FileText
}

const PREVIEWABLE_EXTENSIONS = new Set([
  'md', 'html', 'htm',
  'pdf',
  'pptx', 'ppt',
  'xlsx', 'xls',
  'docx', 'doc',
  'png', 'jpg', 'jpeg',
  'bmp', 'gif', 'jpeg', 'jpg', 'png', 'svg', 'webp',
  'txt', 'json',
  'c', 'conf', 'cpp', 'css', 'csv', 'go', 'graphql',
  'h', 'hpp', 'java', 'js', 'jsx', 'log', 'lua', 'mjs',
  'py', 'rb', 'rs', 'sh', 'sql', 'toml', 'ts', 'tsx',
  'xml', 'yaml', 'yml', 'zsh'
])

function fileIcon(path: string) {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return FILE_ICONS[ext] ?? FileText
}

function fileName(path: string) {
  return path.split(/[/\\]/).pop() ?? path
}

function fileExtension(path: string) {
  const idx = path.lastIndexOf('.')
  return idx >= 0 ? path.slice(idx + 1).toLowerCase() : ''
}

export function isPreviewable(path: string) {
  return PREVIEWABLE_EXTENSIONS.has(fileExtension(path))
}

interface FileCardProps {
  path: string
}

export function FileCard({ path }: FileCardProps) {
  const { t } = useI18n()
  const Icon = fileIcon(path)
  const name = fileName(path)

  // NOTE: deliberately NOT named `openPreview` — that name is the store's
  // entry point imported above, and a local function shadowing it would call
  // itself instead of the rail.
  const openInPreview = useCallback(async () => {
    try {
      await readDesktopFileText(path)
    } catch {
      notifyError(new Error(`File not found: ${name}`), t.preview.unavailable)
      return
    }

    // Same road as every other preview open: normalize (Electron IPC when
    // available, local fallback otherwise), then openPreview — the rail's
    // only entry point. 'explicit-link' so an HTML file renders live rather
    // than as source.
    const resolved = await normalizeOrLocalPreviewTarget(path)

    if (resolved) {
      openPreview(resolved, 'explicit-link')
    }
  }, [path, name, t.preview.unavailable])

  const openExternally = useCallback(() => {
    const target = localPreviewTarget(path)
    void window.hermesDesktop?.openExternal?.(target?.url ?? path)
  }, [path])

  const handleClick = useCallback(() => {
    if (isPreviewable(path)) {
      void openInPreview()
    } else {
      openExternally()
    }
  }, [path, openInPreview, openExternally])

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <span>
          <Tip label={t.preview.openFile(name)}>
            <span
              className="inline-flex cursor-pointer items-center gap-2.5 rounded-2xl border border-[#E5E2DB] bg-[#F4F1EB] px-3.5 py-2.5 text-sm transition-colors hover:bg-[#EDE8E0]"
              onClick={handleClick}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  handleClick()
                }
              }}
              role="button"
              tabIndex={0}
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-black/[0.06]">
                <Icon aria-hidden className="size-3.5 text-[#555]" />
              </span>
              <span className="min-w-0 truncate font-medium text-[#2C2C2C]">{name}</span>
            </span>
          </Tip>
        </span>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onClick={() => void openInPreview()}>
          <span aria-hidden className="mr-1 text-sm">🖥️</span>
          {t.preview.openInPane}
        </ContextMenuItem>
        {(fileExtension(path) === 'html' || fileExtension(path) === 'htm') && (
          <ContextMenuItem
            onClick={() => {
              void normalizeOrLocalPreviewTarget(path).then(resolved => {
                // 'manual' routes HTML to source mode (read the markup), the
                // inverse of the default preview open above.
                if (resolved) {
                  openPreview(resolved, 'manual')
                }
              })
            }}
          >
            <FileText aria-hidden className="size-3.5" />
            {t.preview.source}
          </ContextMenuItem>
        )}
        <ContextMenuItem onClick={openExternally}>
          <ExternalLink aria-hidden className="size-3.5" />
          {t.preview.openExternally}
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}

const FILE_PATH_RE =
  /(?<!\w)[A-Za-z]:[/\\][^\s<>()[\]"']+\.[\w]{2,5}\b|(?<![\w/:.~])\/[^\s<>()[\]"']+\/[^\s<>()[\]"']+\.[\w]{2,5}\b/gi

export function splitFilePathTokens(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null

  const re = new RegExp(FILE_PATH_RE.source, 'gi')

  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index))
    }

    parts.push(<FileCard key={match[0]} path={match[0]} />)
    lastIndex = match.index + match[0].length
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }

  return parts
}

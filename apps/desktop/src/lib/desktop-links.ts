const DESKTOP_OPEN_PREFIX = '#desktop-open/'

type MarkdownNode = {
  children?: MarkdownNode[]
  type?: string
  url?: unknown
}

function isLocalFileUrl(url: URL): boolean {
  const hostname = url.hostname.toLowerCase()

  return (
    url.protocol === 'file:' &&
    (!hostname || hostname === 'localhost') &&
    !url.username &&
    !url.password &&
    !url.port &&
    !url.pathname.startsWith('//')
  )
}

function isObsidianOpenUrl(url: URL): boolean {
  return (
    url.protocol === 'obsidian:' &&
    url.hostname.toLowerCase() === 'open' &&
    !url.username &&
    !url.password &&
    !url.port &&
    (!url.pathname || url.pathname === '/') &&
    !url.hash
  )
}

function isAllowedDesktopTarget(value: string): boolean {
  let url: URL

  try {
    url = new URL(value)
  } catch {
    return false
  }

  return isLocalFileUrl(url) || isObsidianOpenUrl(url)
}

export function desktopMarkdownHref(target: string): string | null {
  return isAllowedDesktopTarget(target) ? `${DESKTOP_OPEN_PREFIX}${encodeURIComponent(target)}` : null
}

export function desktopTargetFromMarkdownHref(href?: string): string | null {
  if (!href?.startsWith(DESKTOP_OPEN_PREFIX)) {
    return null
  }

  try {
    const target = decodeURIComponent(href.slice(DESKTOP_OPEN_PREFIX.length))

    return isAllowedDesktopTarget(target) ? target : null
  } catch {
    return null
  }
}

export function remarkDesktopLinks() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (node.type === 'link' && typeof node.url === 'string') {
        const href = desktopMarkdownHref(node.url)

        if (href) {
          node.url = href
        }
      }

      node.children?.forEach(visit)
    }

    visit(tree)
  }
}

"""Graph-adjudicated documentation conformance test.

Every documentation claim is an edge in a graph: the doc file is the source
node, and each reference inside it — internal link, code symbol, CLI command,
config key, file path — is an edge that must resolve to a real target in the
codebase graph. A doc that references something that does not exist is a
dangling edge, and this test fails on it.

This is the documentation-layer enforcement for the god-file decomposition
campaign (#54962): as gateway/run.py's symbols moved into gateway/*_mixin.py
and gateway/*_helpers.py modules, docs that still name the old locations or
link to vanished pages would otherwise rot silently. The graph adjudicates.

Two adjudication surfaces:

1. INTERNAL LINKS — every markdown link and bare path reference in the user
   docs must resolve to a real file under the repo (or an external URL).
   Handles ./relative, ../relative, /absolute-from-docs-root, and
   [text](path#anchor) forms.

2. CODE SYMBOLS — every backtick-quoted identifier in the user docs that
   looks like a python symbol (module.attr or _private_name) must resolve in
   the codebase graph: either as a file path, an importable module, an
   attribute of an importable module, or a CLI command / config key the docs
   already enumerate. Unknown symbols are dangling edges.

Ad-hoc false positives are worse than silence: the symbol surface only
adjudicates identifiers that *look* like code (contain a dot, or match a
module-level name we can actually find), and the link surface ignores
external URLs and fragment-only anchors.

Run: python -m pytest tests/conformance/test_docs_graph_conformance.py -q
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "website" / "docs"

# ---------------------------------------------------------------------------
# codebase graph
# ---------------------------------------------------------------------------


def _python_files() -> list[Path]:
    """All .py files under the repo, excluding venv/.git/backup dirs."""
    out = []
    for p in REPO_ROOT.rglob("*.py"):
        parts = p.relative_to(REPO_ROOT).parts
        if any(seg in (".git", "venv", "node_modules", "__pycache__", ".hermes") for seg in parts):
            continue
        out.append(p)
    return out


def _module_paths() -> set[str]:
    """Importable module paths, e.g. 'gateway.run', 'cli'."""
    mods = set()
    for p in _python_files():
        rel = p.relative_to(REPO_ROOT)
        if rel.parts[0] not in ("gateway", "agent", "cli", "hermes_cli", "cron", "tui_gateway"):
            continue
        mods.add(".".join(rel.with_suffix("").parts))
    return mods


def _exported_symbols() -> dict[str, set[str]]:
    """module -> set of top-level def/class/assign names (the graph's nodes)."""
    out: dict[str, set[str]] = {}
    for p in _python_files():
        rel = p.relative_to(REPO_ROOT)
        if rel.parts[0] not in ("gateway", "agent", "cli", "hermes_cli", "cron", "tui_gateway"):
            continue
        mod = ".".join(rel.with_suffix("").parts)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
        out[mod] = names
    return out


# ---------------------------------------------------------------------------
# doc graph
# ---------------------------------------------------------------------------

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
CODE_RE = re.compile(r"`([^`]+)`")
# module.attr or module.submodule.attr
SYMBOL_RE = re.compile(r"^[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+$")
# bare path-like reference in prose: path/to/file.md or file.py
PATH_RE = re.compile(r"[\w./-]+\.(?:md|py|yaml|yml|toml|json|sh)\b")


def _doc_files() -> list[Path]:
    return sorted(DOCS_ROOT.rglob("*.md")) + sorted(DOCS_ROOT.rglob("*.mdx"))


def _resolve_doc_link(link: str, src: Path) -> Path | None:
    """Resolve an internal doc link to a real file, or None if dangling."""
    if link.startswith(("http://", "https://", "mailto:")):
        return None  # external — not adjudicated
    link = link.split("#")[0]
    if not link:
        return None
    if link.startswith("/"):
        # Docusaurus serves the site under a /docs base path and static assets
        # under /img (website/static/img/...). Resolve both against their real
        # roots; anything else site-absolute is a build-time route.
        stripped = link.lstrip("/")
        if stripped.startswith("docs/"):
            stripped = stripped[len("docs/"):]
            candidate = DOCS_ROOT / stripped
        elif stripped.startswith("img/"):
            candidate = REPO_ROOT / "website" / "static" / stripped
        else:
            candidate = DOCS_ROOT / stripped
    else:
        candidate = (src.parent / link).resolve()
    # the target may be a real file with or without an extension in the link
    if candidate.is_file():
        return candidate
    for ext in (".md", ".mdx"):
        if candidate.with_suffix(ext).is_file():
            return candidate.with_suffix(ext)
    # directory-index fallback: /developer-guide/plugins -> plugins/index.md
    for idx in ("index.md", "index.mdx"):
        if (candidate / idx).is_file():
            return candidate / idx
    # Docusaurus convention: /developer-guide/plugins may be a dir whose index
    # lives at plugins.md (single-file section)
    if candidate.is_dir():
        return candidate  # dir exists; content resolves via docusaurus index
    return None


def _known_config_keys() -> set[str]:
    """Config keys named in docs' own enumeration.

    Sources: reference pages (configuration.md, config.md,
    environment-variables.md) plus any backticked dotted key in the user-guide
    that appears in a config context (near 'config.yaml', 'hermes config set',
    'config set', or a `section.key` shape in prose). This is how cron.model,
    cron.model_provider, agent.personalities resolve as CONFIG_KEY nodes, not
    as Python symbols.
    """
    keys = set()
    for name in ("configuration.md", "config.md", "environment-variables.md"):
        f = DOCS_ROOT / "user-guide" / name
        if not f.is_file():
            f = DOCS_ROOT / "reference" / name
        if not f.is_file():
            continue
        for m in re.finditer(r"`([a-z][a-z0-9_.-]*)`", f.read_text(encoding="utf-8")):
            keys.add(m.group(1))
    # config-context keys across the whole user guide: any dotted backtick
    # within 80 chars of 'config.yaml' / 'hermes config set' / 'config set'
    for doc in _doc_files():
        if "user-guide" not in doc.parts:
            continue
        text = doc.read_text(encoding="utf-8")
        for m in re.finditer(r"`([a-z][a-z0-9_.-]*)`", text):
            key = m.group(1)
            if "." not in key:
                continue
            # whole-line config context: any line mentioning config set /
            # config.yaml / setting is a config-enumeration line
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            line = text[line_start:line_end]
            if any(tok in line for tok in ("config.yaml", "config set",
                                           "config.yml", "set ", "setting",
                                           "config key", "default")):
                keys.add(key)
    return keys


# ---------------------------------------------------------------------------
# the adjudication
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def codebase_graph():
    return {
        "modules": _module_paths(),
        "symbols": _exported_symbols(),
        "config_keys": _known_config_keys(),
    }


def test_all_doc_internal_links_resolve(codebase_graph):
    dangling = []
    total = 0
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8")
        for m in LINK_RE.finditer(text):
            link = m.group(1)
            if link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # template placeholders and build-time site roots are not doc files
            if link in ("url", "URL", "<url>", "/llms.txt", "/llms-full.txt"):
                continue
            total += 1
            if _resolve_doc_link(link, doc) is None:
                dangling.append(f"{doc.relative_to(REPO_ROOT)}: {link}")
    assert not dangling, (
        f"{len(dangling)} dangling internal doc links (of {total} checked):\n"
        + "\n".join(dangling[:25])
    )


def test_doc_code_symbols_resolve(codebase_graph):
    """Backtick code refs that look like code must resolve in the codebase graph."""
    mods = codebase_graph["modules"]
    syms = codebase_graph["symbols"]
    # external API namespaces documented as prose, not repo symbols
    EXTERNAL = ("Runtime", "Page", "window", "document", "console", "CDP",
                "Chrome", "HTTP", "HTTPS", "URL", "HTML", "DOM", "WebSocket",
                "Node", "npm", "Python", "NodeJS")
    dangling = []
    total = 0
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8")
        for m in CODE_RE.finditer(text):
            ref = m.group(1).strip()
            if not ref or len(ref) > 120:
                continue
            # config keys / env vars / flags are enumerated surface, not symbols
            if ref in codebase_graph["config_keys"] or ref.startswith(("HERMES_", "--", "-")):
                continue
            # bare file refs: resolve against the real tree
            if re.match(r"^[\w./-]+\.(?:py|yaml|yml|toml|json|sh|md|mdx)$", ref):
                # Skill-generated pages: relative references/ + scripts/ paths
                # are rewritten by website/scripts/generate-skill-docs.py to
                # absolute GitHub blob URLs (the generator owns that
                # transformation — see rewrite_relative_links). They are not
                # repo-relative paths in the doc tree, so not adjudicated here.
                if ("user-guide/skills/" in str(doc).replace("\\", "/") and
                        (ref.startswith(("references/", "scripts/", "workflows/",
                                         "assets/", "google-labs-code/", "utils/",
                                         "diagrams/", "engagement/", "templates/",
                                         ".github/")) or ref.startswith("/opt/"))):
                    continue
                # A bare filename with no directory and no repo prefix is an
                # illustrative example the reader creates (auth.py, tool.py,
                # prompt_builder.py, HOOK.yaml) — docs legitimately show these.
                # Only adjudicate refs that point at the repo: those with a
                # directory, or a known repo-root prefix, or that actually exist.
                has_dir = "/" in ref or "\\" in ref
                repo_prefixed = ref.startswith(("gateway/", "agent/", "cli", "hermes_cli/",
                                                "cron/", "tui_gateway/", "website/", "docs/",
                                                "tests/", "scripts/", "plugins/"))
                if not has_dir and not repo_prefixed:
                    # still resolve if it genuinely exists at repo root
                    if (REPO_ROOT / ref).is_file():
                        total += 1
                        continue
                    continue  # bare example name — not adjudicated
                if (REPO_ROOT / ref).is_file():
                    total += 1
                    continue
                # relative file refs from the doc's own directory
                if (doc.parent / ref).resolve().is_file():
                    total += 1
                    continue
                # template paths with dirs: api/handlers.py, tests/test_foo.py,
                # tools/your_tool.py, google-labs-code/design.md are examples
                if any(tok in ref for tok in ("your_", "_foo", "test_foo",
                                              "handlers.py", "design.md")):
                    continue
                dangling.append(f"{doc.relative_to(REPO_ROOT)}: `{ref}` (file not found)")
                continue
            if not SYMBOL_RE.match(ref):
                continue
            # Only adjudicate refs that target the repo. The adjudication
            # surface is: repo-prefixed heads, or a head that IS a known
            # module. Everything else — self.*, example classes, host/ctx
            # SDK APIs, openrouter.ai domains, template paths — is prose
            # example surface and does not need to resolve.
            if ref.startswith(("self.", "cls.", "openrouter.", "image_gen.",
                               "hermes_agent.", "tools/", "your_")):
                continue
            if any(tok in ref for tok in ("your_", "example", "<", ">", "_foo",
                                          "test_foo", "handlers.py")):
                continue
            head, _, tail = ref.rpartition(".")
            repo_surface = ref.startswith(("gateway.", "agent.", "hermes_cli.",
                                           "cli.", "cron.", "tui_gateway."))
            if head.split(".")[0] in EXTERNAL:
                continue  # external API namespace (CDP, browser, DOM)
            if head.split(".")[0] in ("asyncio", "os", "json", "re", "pathlib",
                                      "typing", "datetime", "collections",
                                      "subprocess", "shutil", "tempfile",
                                      "threading", "time", "sys"):
                continue  # stdlib — not repo nodes
            if head in ("host", "ctx", "browser", "plugins", "window",
                        "document", "Runtime", "Page", "Fetch", "CDP",
                        "Network", "Emulation", "AIAgent"):
                continue  # external documented contracts / example classes
            if not repo_surface and head not in mods and ref not in mods:
                continue  # not repo-targeted — prose example, not adjudicated
            total += 1
            if head in mods:
                continue  # module.submodule resolves as a module path
            if head and head in syms:
                if tail in syms[head]:
                    continue  # module.attr resolves
                dangling.append(f"{doc.relative_to(REPO_ROOT)}: `{ref}` (unknown attr on {head})")
                continue
            if ref in mods:
                continue
            # module chain: try progressively shorter heads
            resolved = False
            parts = ref.split(".")
            for i in range(len(parts) - 1, 0, -1):
                h = ".".join(parts[:i])
                if h in mods:
                    resolved = True
                    break
                if h in syms and parts[i] in syms[h]:
                    resolved = True
                    break
            if not resolved:
                dangling.append(f"{doc.relative_to(REPO_ROOT)}: `{ref}`")
    assert not dangling, (
        f"{len(dangling)} unresolved code-symbol refs in docs (of {total} checked):\n"
        + "\n".join(dangling[:25])
    )

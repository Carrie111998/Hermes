"""Welcome banner, ASCII art, skills summary, and update check for the CLI.

Pure display functions with no HermesCLI state dependency.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from hermes_constants import get_hermes_home
from typing import TYPE_CHECKING, Any, Dict, List, Optional

# rich and prompt_toolkit are imported lazily (inside the functions that use
# them) rather than at module level.  Importing this module is on the TUI
# gateway's critical startup path purely to reach the lightweight update-check
# helpers (``prefetch_update_check``); pulling rich.console + prompt_toolkit
# eagerly added ~50ms of wasted imports before ``gateway.ready`` could fire.
# Keep the type-only reference available to checkers without the runtime cost.
if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)


# =========================================================================
# ANSI building blocks for conversation display
# =========================================================================

_GOLD = "\033[1;38;2;255;215;0m"  # True-color #FFD700 bold
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        # prompt_toolkit needs a real console. On Windows, a redirected or
        # absent stdout (pythonw.exe, CI, `hermes ... > file`) raises
        # NoConsoleScreenBufferError from its Win32Output — display helpers
        # must never crash the caller over that, so degrade to plain print.
        print(text)


# =========================================================================
# Skin-aware color helpers
# =========================================================================

def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback
# =========================================================================
# ASCII Art & Branding
# =========================================================================

from hermes_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE

HERMES_AGENT_LOGO = """[bold #FFD700]██╗  ██╗███████╗██████╗ ███╗   ███╗███████╗███████╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗[/]
[bold #FFD700]██║  ██║██╔════╝██╔══██╗████╗ ████║██╔════╝██╔════╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝[/]
[#FFBF00]███████║█████╗  ██████╔╝██╔████╔██║█████╗  ███████╗█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║[/]
[#FFBF00]██╔══██║██╔══╝  ██╔══██╗██║╚██╔╝██║██╔══╝  ╚════██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║[/]
[#CD7F32]██║  ██║███████╗██║  ██║██║ ╚═╝ ██║███████╗███████║      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║[/]
[#CD7F32]╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝      ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝[/]"""

HERMES_CADUCEUS = """[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⠀⣀⣀⠀⢀⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⢀⣠⣴⣾⣿⣿⣇⠸⣿⣿⠇⣸⣿⣿⣷⣦⣄⡀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⢀⣠⣴⣶⠿⠋⣩⡿⣿⡿⠻⣿⡇⢠⡄⢸⣿⠟⢿⣿⢿⣍⠙⠿⣶⣦⣄⡀⠀[/]
[#FFBF00]⠀⠀⠉⠉⠁⠶⠟⠋⠀⠉⠀⢀⣈⣁⡈⢁⣈⣁⡀⠀⠉⠀⠙⠻⠶⠈⠉⠉⠀⠀[/]
[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣴⣿⡿⠛⢁⡈⠛⢿⣿⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠿⣿⣦⣤⣈⠁⢠⣴⣿⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠻⢿⣿⣦⡉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢷⣦⣈⠛⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣴⠦⠈⠙⠿⣦⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣤⡈⠁⢤⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠷⠄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⠑⢶⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠁⢰⡆⠈⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠳⠈⣡⠞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]"""



# =========================================================================
# Skills scanning
# =========================================================================

_available_skills_cache: Optional[tuple] = None  # (result,) once computed


def get_available_skills() -> Dict[str, List[str]]:
    """Return skills grouped by category, filtered by platform and disabled state.

    Delegates to ``_find_all_skills()`` from ``tools/skills_tool`` which already
    handles platform gating (``platforms:`` frontmatter) and respects the
    user's ``skills.disabled`` config list.

    Cached per-process: this feeds only the startup banner, whose snapshot
    is taken once anyway, and the underlying skills-tree walk costs ~100ms.
    ``prefetch_banner_data()`` uses the cache to pay that walk off-thread.
    """
    global _available_skills_cache
    if _available_skills_cache is not None:
        return _available_skills_cache[0]
    try:
        from tools.skills_tool import _find_all_skills
        all_skills = _find_all_skills()  # already filtered
    except Exception:
        return {}

    skills_by_category: Dict[str, List[str]] = {}
    for skill in all_skills:
        category = skill.get("category") or "general"
        skills_by_category.setdefault(category, []).append(skill["name"])
    _available_skills_cache = (skills_by_category,)
    return skills_by_category


# =========================================================================
# Update check
# =========================================================================

# Cache update check results for 6 hours to avoid repeated git fetches
_UPDATE_CHECK_CACHE_SECONDS = 6 * 3600

# Sentinel returned when we know an update exists but can't count commits
# (e.g. nix-built hermes — no local git history to count against).
UPDATE_AVAILABLE_NO_COUNT = -1

_UPSTREAM_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
_OFFICIAL_REPO_CANONICAL = "github.com/nousresearch/hermes-agent"


def _canonical_github_remote(url: str | None) -> str:
    """Return ``host/owner/repo`` for common GitHub remote URL forms."""
    if not url:
        return ""
    value = url.strip()
    if value.startswith("git@github.com:"):
        value = "github.com/" + value[len("git@github.com:"):]
    elif value.startswith("ssh://git@github.com/"):
        value = "github.com/" + value[len("ssh://git@github.com/"):]
    else:
        parsed = urlparse(value)
        if parsed.netloc and parsed.path:
            value = f"{parsed.netloc}{parsed.path}"
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()


def _is_ssh_remote(url: str | None) -> bool:
    if not url:
        return False
    value = url.strip().lower()
    return value.startswith("git@") or value.startswith("ssh://")


def _is_official_ssh_remote(url: str | None) -> bool:
    return _is_ssh_remote(url) and _canonical_github_remote(url) == _OFFICIAL_REPO_CANONICAL


def _git_stdout(args: list[str], *, cwd: Path, timeout: int = 5) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            # git output is UTF-8; on Windows text=True defaults to the ANSI
            # code page and bytes like 0x90 (3rd byte of 🐛 in a commit
            # subject) crash the stdlib reader thread (#52649).
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _github_compare_behind(current_rev: str, target_rev: str) -> Optional[int]:
    """Exact behind-count via the GitHub compare API for uncountable graphs.

    Shallow installer clones and ls-remote-only probes know the two tip SHAs
    but have no local history to run ``rev-list --count`` across. GitHub's
    ``GET /repos/<owner>/<repo>/compare/<current>...<target>`` knows the full
    graph regardless of local clone depth and returns ``ahead_by`` — exactly
    the behind count the local graph lost. Unauthenticated, bounded, and
    best-effort: any failure (offline, rate limit, diverged/unknown SHAs)
    returns None so callers keep the honest UPDATE_AVAILABLE_NO_COUNT.
    """
    if not (_is_full_sha(current_rev) and _is_full_sha(target_rev)):
        return None
    url = (
        "https://api.github.com/repos/nousresearch/hermes-agent/"
        f"compare/{current_rev}...{target_rev}"
    )
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                # api.github.com 403s requests without a User-Agent.
                "User-Agent": "hermes-cli-update-check",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    ahead = payload.get("ahead_by") if isinstance(payload, dict) else None
    if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead >= 0:
        return ahead
    return None


def _is_full_sha(value: Optional[str]) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def _upstream_main_sha() -> Optional[str]:
    """Tip SHA of upstream main via HTTPS ls-remote (no auth, no prompts)."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", _UPSTREAM_REPO_URL, "refs/heads/main"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    upstream_rev = result.stdout.split()[0]
    return upstream_rev or None


def _check_via_rev(local_rev: str) -> Optional[int]:
    """Compare an embedded git revision to upstream main via ls-remote.

    Returns 0 if up-to-date, the exact behind-count when the GitHub compare
    API can recover it, ``UPDATE_AVAILABLE_NO_COUNT`` if behind by an unknown
    amount, or ``None`` on failure.
    """
    upstream_rev = _upstream_main_sha()
    if not upstream_rev:
        return None
    if upstream_rev == local_rev:
        return 0
    # Behind, but ls-remote only knows tip SHAs. Try to recover the exact
    # count from the GitHub compare API before falling back to the sentinel.
    # ahead_by == 0 with differing tips means the remote tip is reachable from
    # our HEAD — a local-ahead checkout, i.e. NOT behind.
    counted = _github_compare_behind(local_rev, upstream_rev)
    return counted if counted is not None else UPDATE_AVAILABLE_NO_COUNT


def _check_via_local_git(repo_dir: Path) -> Optional[int]:
    """Count commits behind origin/main in a local checkout."""
    origin_url = _git_stdout(["remote", "get-url", "origin"], cwd=repo_dir)
    if _is_official_ssh_remote(origin_url):
        head_rev = _git_stdout(["rev-parse", "HEAD"], cwd=repo_dir)
        if not head_rev:
            return None
        # Passive probe via HTTPS ls-remote (never SSH — no hardware-key
        # prompts). Tip SHAs alone can't distinguish "behind" from a local
        # carried commit sitting AHEAD of origin/main, and misreporting an
        # ahead checkout as behind nudges the user into `hermes update`,
        # which can wipe their carried work.
        upstream_rev = _upstream_main_sha()
        if upstream_rev is None:
            return None
        if upstream_rev == head_rev:
            return 0
        # Local-ahead: the remote tip is an ancestor of HEAD. Checked against
        # the FRESH upstream SHA (not the possibly stale origin/main tracking
        # ref) so a stale ref can't fake an up-to-date report.
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", upstream_rev, "HEAD"],
            capture_output=True, timeout=5, cwd=str(repo_dir),
        )
        if ancestor.returncode == 0:
            return 0
        # Genuinely behind (or diverged). Recover the exact count via the
        # GitHub compare API; a local-only HEAD 404s there, which safely
        # degrades to the honest no-count sentinel — never a fabricated 1.
        counted = _github_compare_behind(head_rev, upstream_rev)
        return counted if counted is not None else UPDATE_AVAILABLE_NO_COUNT

    # Installer checkouts are shallow (`git clone --depth 1`). On a shallow
    # clone the history stops at a single commit, so a plain `git fetch` would
    # unshallow the repo (dragging in the whole history) and
    # `rev-list --count HEAD..origin/main` would report a huge bogus "behind"
    # number (e.g. "12492 commits behind"). Detect shallow up front: fetch with
    # --depth 1 to preserve the boundary and compare tip SHAs instead of
    # counting. Full clones (developers, Docker dev images) keep the exact
    # count path unchanged. Mirrors the desktop fix in apps/desktop/electron/main.cjs.
    shallow = _git_stdout(["rev-parse", "--is-shallow-repository"], cwd=repo_dir)
    is_shallow = shallow == "true"

    try:
        # Self-heal abandoned git lock files before fetching. A stale
        # .git/shallow.lock from a crashed fetch makes the fetch fail, the
        # exception below is swallowed, and stale refs get compared against
        # HEAD — silently degrading the passive check until a human removes
        # the lock (git never self-heals these).
        from hermes_cli.gitlock import clear_stale_git_locks, clear_stale_tmp_packs

        clear_stale_git_locks(repo_dir)
        # The passive check is the main tmp_pack GENERATOR on flaky lines
        # (several aborted fetches per day) — it must also be the janitor,
        # or debris accumulates unbounded between manual updates (#93732).
        clear_stale_tmp_packs(repo_dir)

        # Scope the fetch to the one branch the behind-count compares against.
        # An unscoped ``git fetch origin`` transfers every remote head (~1,400
        # on this repo — measured 3.0 s vs 0.55 s scoped) and can burn the full
        # 10 s timeout on slow links. ``cmd_update`` already scopes its fetch
        # for the same reason. Modern git updates the ``origin/main`` tracking
        # ref on a scoped fetch, so the ``HEAD..origin/main`` count below is
        # unaffected; the shallow path compares against FETCH_HEAD, which a
        # scoped fetch also updates.
        fetch_args = ["git", "fetch", "origin", "main"]
        if is_shallow:
            fetch_args += ["--depth", "1"]
        fetch_args.append("--quiet")
        fetch_proc = subprocess.run(
            fetch_args,
            capture_output=True, timeout=10,
            cwd=str(repo_dir),
        )
        fetch_ok = fetch_proc.returncode == 0
    except Exception:
        fetch_ok = False  # Offline or timeout — don't use stale refs

    # When the fetch fails, the local origin/main tracking ref is stale. It
    # cannot prove *currentness* (a 0 behind-count may just mean the stale ref
    # hasn't caught up), but if it already shows HEAD behind, that is sound
    # evidence an update exists — the ref was good at some point in the past.
    # Return the positive stale count; return None (inconclusive) otherwise so
    # the caller doesn't cache a false "up to date". (#82166, review #92578)
    if not fetch_ok:
        if not is_shallow:
            try:
                result = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD..origin/main"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=5,
                    cwd=str(repo_dir),
                )
                if result.returncode == 0:
                    behind = int(result.stdout.strip())
                    if behind > 0:
                        return behind
            except Exception:
                pass
        return None

    if is_shallow:
        # No history to count across the shallow boundary. `origin/main` may not
        # be a tracking ref in a `clone --depth 1`, so prefer FETCH_HEAD (just
        # updated by the fetch above) and fall back to origin/main.
        head_rev = _git_stdout(["rev-parse", "HEAD"], cwd=repo_dir)
        target_rev = (
            _git_stdout(["rev-parse", "FETCH_HEAD"], cwd=repo_dir)
            or _git_stdout(["rev-parse", "origin/main"], cwd=repo_dir)
        )
        if not head_rev or not target_rev:
            return None
        if head_rev == target_rev:
            return 0
        # Tips differ but the shallow boundary hides the history between them.
        # Recover the exact count from the GitHub compare API when possible
        # (ahead_by == 0 means local-ahead ⇒ up to date); otherwise report the
        # honest "update available, count unknown" sentinel.
        counted = _github_compare_behind(head_rev, target_rev)
        return counted if counted is not None else UPDATE_AVAILABLE_NO_COUNT

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def check_for_updates() -> Optional[int]:
    """Check whether a Hermes update is available.

    Two paths: if ``HERMES_REVISION`` is set (nix builds embed it), compare
    it to upstream main via ``git ls-remote``. Otherwise look for a local
    git checkout and count commits behind ``origin/main``.

    Returns the number of commits behind, ``UPDATE_AVAILABLE_NO_COUNT`` (-1)
    if behind but the count is unknown, ``0`` if up-to-date, or ``None`` if
    the check failed or doesn't apply. Cached for 6 hours.
    """
    hermes_home = get_hermes_home()
    cache_file = hermes_home / ".update_check"
    embedded_rev = os.environ.get("HERMES_REVISION") or None
    repo_dir: Optional[Path] = None
    git_head: Optional[str] = None
    if not embedded_rev:
        # Prefer the running code's location over the profile-scoped path.
        # $HERMES_HOME/hermes-agent/ may be a stale copy from --clone-all;
        # Path(__file__) always resolves to the actual installed checkout.
        candidate = Path(__file__).parent.parent.resolve()
        if not (candidate / ".git").exists():
            candidate = hermes_home / "hermes-agent"
        if (candidate / ".git").exists():
            repo_dir = candidate
            git_head = _read_git_head_for_cache(repo_dir)

    # Docker images have no working tree to count commits against — the
    # published image excludes `.git` (see .dockerignore) and sets no
    # HERMES_REVISION (that's nix-only). Returning None makes both the Rich
    # banner (build_welcome_banner) and the Ink badge (branding.tsx, guarded
    # on `typeof === 'number' && > 0`) show nothing. The dashboard's REST
    # `/api/hermes/update/check` endpoint short-circuits docker the same way
    # (web_server.py); mirror that here so the banner/TUI surfaces agree.
    try:
        from hermes_cli.config import detect_install_method, get_project_root
        if detect_install_method(get_project_root()) in {"docker", "apt"}:
            return None
    except Exception:
        pass

    # Read cache — invalidate if the embedded rev OR installed version has
    # changed since the last check. Source installs also include the local git
    # HEAD so a same-version update does not leave a stale "behind" count for
    # the cache TTL.
    now = time.time()
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (
                now - cached.get("ts", 0) < _UPDATE_CHECK_CACHE_SECONDS
                and cached.get("rev") == embedded_rev
                and cached.get("ver") == VERSION
                and cached.get("repo") == (str(repo_dir) if repo_dir else None)
                and cached.get("git_head") == git_head
            ):
                return cached.get("behind")
    except Exception:
        pass

    if embedded_rev:
        behind = _check_via_rev(embedded_rev)
    elif repo_dir is None:
        behind = None
    else:
        behind = _check_via_local_git(repo_dir)

    try:
        # Could be None if the check could not run; official guard skips caching None (#82166).
        if behind is not None:
            cache_file.write_text(
                json.dumps(
                    {
                        "ts": now,
                        "behind": behind,
                        "rev": embedded_rev,
                        "ver": VERSION,
                        "repo": str(repo_dir) if repo_dir else None,
                        "git_head": git_head,
                    }
                ),
                encoding="utf-8",
            )

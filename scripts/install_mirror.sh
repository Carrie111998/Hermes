#!/usr/bin/env bash
# ============================================================================
# install_mirror.sh — mirror resolver for install.sh
# ============================================================================
#
# Region-aware fallback mirrors for the two endpoints install.sh hits that
# are commonly blocked, throttled, or otherwise unreliable from some networks
# (PR for issue #95167 — Chinese user on Windows 10 VM with persistent install
# failures; the user comments confirmed the resolution was "use a domestic
# mirror"):
#
#   * PyPI (`https://pypi.org/simple/`)   -- used by `uv sync` / `uv pip install`
#   * GitHub  (`https://github.com/...`)  -- used by `git clone` and the
#                                            archive ZIP fallback
#
# Public functions:
#
#   hermes_pypi_mirror          Echo the first reachable PyPI mirror URL,
#                               or the canonical PyPI URL when none of the
#                               candidates responded within the probe window.
#   hermes_github_clone_url     Rewrite a github.com URL to a working proxy
#                               when one exists, or echo the input unchanged.
#   hermes_mirror_reachable     curl -fsI helper: 0 = reachable, 1 = blocked.
#   hermes_mirror_status        Print reachable/blocked status for every
#                               candidate; primarily used to surface a
#                               "we picked a mirror" log line at install.
#
# Honours these env vars (the same names install_mirror.ps1 uses, so a user
# who set one of them once sees consistent behaviour across installers):
#
#   HERMES_PYPI_MIRROR          Force the PyPI mirror (skip the resolver).
#   HERMES_GITHUB_PROXY         Force the GitHub proxy URL.
#
# Probe timeouts are deliberately tight (4 seconds). The installer runs this
# at startup, before the user has waited long enough to regret starting it.
# Each reachable probe adds about 1-3 seconds on a healthy network; an
# unreachable probe times out in 4s. Six probes worst case = ~25s, which is
# still faster than the 5-minute uv install it precedes.

HERMES_PYPI_MIRROR_CANDIDATES=(
    # Aliyun (China; widely mirrored). Stable URL, returns PyPA simple API.
    "https://mirrors.aliyun.com/pypi/simple/"
    # Tsinghua TUNA. Stable, returns PyPA simple API.
    "https://pypi.tuna.tsinghua.edu.cn/simple/"
    # USTC. Returns PyPA simple API.
    "https://pypi.mirrors.ustc.edu.cn/simple/"
    # Tencent Cloud. Returns PyPA simple API.
    "https://mirrors.cloud.tencent.com/pypi/simple/"
    # Default. Last so the resolver never prefers an unreachable mirror
    # over a reachable canonical PyPI.
    "https://pypi.org/simple/"
)

# GitHub proxy candidates. Each entry is a URL prefix; hermes_github_clone_url
# replaces `https://github.com/` with the chosen prefix and keeps the path.
HERMES_GITHUB_PROXY_CANDIDATES=(
    # ghfast.top — community-maintained GitHub raw/archive proxy (best-effort;
    # not affiliated with GitHub). Hostname-only because it's a transparent
    # reverse proxy.
    "https://ghfast.top/"
    # gh-proxy.com — same shape.
    "https://gh-proxy.com/"
    # Canonical GitHub. Last so the resolver prefers a working proxy when
    # one exists, instead of always reporting github.com as the answer.
    "https://github.com/"
)

HERMES_MIRROR_PROBE_TIMEOUT_SEC=4

hermes_mirror_reachable() {
    # curl -fsI: silent, fail on error, HEAD request. 0 = reachable, 1 = blocked.
    # -L follows one redirect (some mirrors reply with a 3xx to a CDN; further
    # than one would turn a 4-second probe into a multi-minute chase).
    # --connect-timeout caps the SYN-SENT stall when a hostname doesn't
    # resolve; --max-time caps the entire probe.
    #
    # Why HEAD, not GET: PyPI simple indexes return a 200 with the full
    # package directory on GET (kilobytes-to-megabytes). HEAD asks for the
    # response headers only — same status code, no body — which is what we
    # want for a reachability check that's run on every install.
    local url="$1"
    if [ -z "$url" ]; then return 1; fi
    curl -fsIL --connect-timeout "$HERMES_MIRROR_PROBE_TIMEOUT_SEC" \
        --max-time "$HERMES_MIRROR_PROBE_TIMEOUT_SEC" \
        "$url" >/dev/null 2>&1
}

hermes_pypi_mirror() {
    # Returns the URL of the first reachable PyPI mirror, or the canonical
    # PyPI URL when none of the candidates responded inside the probe
    # window. Honour HERMES_PYPI_MIRROR first (so a user who's already
    # configured their own mirror doesn't see a different one picked for
    # them), then probe.
    if [ -n "${HERMES_PYPI_MIRROR:-}" ]; then
        # Trim trailing slash for consistency; mirror ops append their own.
        echo "${HERMES_PYPI_MIRROR%/}/"
        return 0
    fi

    local candidate
    for candidate in "${HERMES_PYPI_MIRROR_CANDIDATES[@]}"; do
        if hermes_mirror_reachable "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    # None reachable. Return the canonical URL anyway -- the resolver has
    # already logged which mirrors timed out, so the user knows their
    # network is the problem. Better to let `uv` try with a clear
    # network error than to fail the install on the resolver itself.
    echo "https://pypi.org/simple/"
    return 0
}

hermes_github_clone_url() {
    # Rewrite a github.com URL to a proxy that is currently reachable, or
    # echo the input unchanged when no proxy responds. Honours
    # HERMES_GITHUB_PROXY first.
    #
    # Only handles http(s) URLs the installer actually emits; ssh URLs
    # (git@github.com:...) are returned unchanged because the SSH
    # transport has no mirror equivalent in this list.
    local url="$1"
    if [ -z "$url" ]; then echo "$url"; return 0; fi
    if [ -n "${HERMES_GITHUB_PROXY:-}" ]; then
        # Strip the canonical github.com hostname; the user-provided proxy
        # is expected to take the same path components.
        echo "$url" | sed -E "s|^https?://github\\.com/|${HERMES_GITHUB_PROXY%/}/|"
        return 0
    fi
    # Only mirror http(s) github.com URLs; SSH clones pass through.
    case "$url" in
        https://github.com/*|http://github.com/*) ;;
        *) echo "$url"; return 0 ;;
    esac

    local proxy
    for proxy in "${HERMES_GITHUB_PROXY_CANDIDATES[@]}"; do
        if [ "$proxy" = "https://github.com/" ]; then continue; fi
        if hermes_mirror_reachable "$proxy"; then
            echo "$url" | sed -E "s|^https?://github\\.com/|${proxy%/}/|"
            return 0
        fi
    done
    echo "$url"
    return 0
}

hermes_mirror_status() {
    # Diagnostic output: print reachable/blocked status for every candidate
    # in a stable, human-readable format. The PowerShell equivalent
    # (Get-HermesMirrorStatus) emits objects; here we emit lines because the
    # consumer is the terminal, and tshooting a real user doesn't need
    # machine-readable output.
    local endpoint url reachable
    for url in "${HERMES_PYPI_MIRROR_CANDIDATES[@]}"; do
        if hermes_mirror_reachable "$url"; then
            reachable="OK"; else reachable="BLOCKED"
        fi
        echo "pypi    ${reachable}  ${url}"
    done
    for url in "${HERMES_GITHUB_PROXY_CANDIDATES[@]}"; do
        if hermes_mirror_reachable "$url"; then
            reachable="OK"; else reachable="BLOCKED"
        fi
        echo "github  ${reachable}  ${url}"
    done
}
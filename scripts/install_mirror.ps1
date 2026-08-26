# ============================================================================
# install_mirror.ps1 — mirror resolver for install.ps1
# ============================================================================
#
# Region-aware fallback mirrors for the two endpoints install.ps1 hits that
# are commonly blocked, throttled, or otherwise unreliable from some networks
# (PR for issue #95167 — Chinese user on Windows 10 VM with persistent install
# failures; the user comments confirmed the resolution was "use a domestic
# mirror"):
#
#   * PyPI (`https://pypi.org/simple/`)   -- used by `uv sync` / `uv pip install`
#   * GitHub  (`https://github.com/...`)  -- used by `git clone` and the
#                                            archive ZIP fallback
#
# Why a separate file:
#
#   * Both `scripts/install.ps1` and the future `scripts/install.sh` (POSIX)
#     can dot-source the same conceptual table. PowerShell lives here; the
#     bash table lives next door in `install_mirror.sh`. Keeping the mirror
#     list in one place per language makes it easy for a future PR to add a
#     region without touching either installer's control flow.
#
# Public functions:
#
#   Get-HermesPypiMirror          Return the first reachable PyPI mirror, or
#                                 the empty string when none is reachable.
#   Get-HermesGithubCloneUrl      Rewrite a github.com URL to a proxy when one
#                                 is reachable, or return the input unchanged.
#   Test-HermesMirrorReachable    HEAD-probe helper, returns $true/$false.
#   Get-HermesMirrorStatus        Print reachable/blocked status for every
#                                 candidate; primarily used to surface a
#                                 "we picked a mirror" log line at install.
#
# Honours these env vars:
#
#   HERMES_PYPI_MIRROR            Force the PyPI mirror (skip the resolver).
#                                 e.g. HERMES_PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple/
#   HERMES_GITHUB_PROXY           Force the GitHub proxy URL.
#
# Probe timeouts are deliberately tight (4 seconds). The installer runs this
# at startup, before the user has waited long enough to regret starting it.
# Each reachable probe adds about 1-3 seconds on a healthy network; an
# unreachable probe times out in 4s. Six probes worst case = ~25s, which is
# still faster than the 5-minute uv install it precedes.

$script:HermesPypiMirrorCandidates = @(
    # Aliyun (China; widely mirrored). Stable URL, returns PyPA simple API.
    'https://mirrors.aliyun.com/pypi/simple/'
    # Tsinghua TUNA. Stable, returns PyPA simple API.
    'https://pypi.tuna.tsinghua.edu.cn/simple/'
    # USTC. Returns PyPA simple API.
    'https://pypi.mirrors.ustc.edu.cn/simple/'
    # Tencent Cloud. Returns PyPA simple API.
    'https://mirrors.cloud.tencent.com/pypi/simple/'
    # Default. Last so the resolver never prefers an unreachable mirror
    # over a reachable canonical PyPI.
    'https://pypi.org/simple/'
)

# GitHub proxy candidates. Each entry is a hostname (the proxy takes the same
# path components as github.com — see `Get-HermesGithubCloneUrl`).
$script:HermesGithubProxyCandidates = @(
    # ghfast.top — community-maintained GitHub raw/archive proxy (best-effort;
    # not affiliated with GitHub). Hostname-only because it's a transparent
    # reverse proxy.
    'https://ghfast.top/'
    # gh-proxy.com — same shape.
    'https://gh-proxy.com/'
    # Canonical GitHub. Last so the resolver prefers a working proxy when
    # one exists, instead of always reporting github.com as the answer.
    'https://github.com/'
)

$script:HermesMirrorProbeTimeoutSec = 4

function Test-HermesMirrorReachable {
    # HEAD-probe a URL. Returns $true when the server answers 2xx/3xx AND
    # redirected to a non-empty destination within the probe budget.
    #
    # Why HEAD, not GET: PyPI simple indexes return a 200 with the full
    # package directory on GET (kilobytes-to-megabytes). HEAD asks for the
    # response headers only — same status code, no body — which is what we
    # want for a reachability check that's run on every install.
    #
    # Why we don't just trust the resolver to retry on failure: `uv pip
    # install` against an unreachable mirror fails with a confusing
    # "Could not find a version that satisfies the requirement" message
    # AFTER waiting for the connect timeout (30+ seconds). Pre-probing
    # trades 25s of bounded probes for that 30s+ of unexplained wait.
    param([string]$Url)

    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }
    try {
        # -UseBasicParsing so this works on Windows PowerShell 5.1 (no
        # -UseBasicParsing flag was added in PowerShell 6+, but 5.1's
        # default HTML parser is gone). MaximumRedirection 1 because some
        # mirrors reply with a 3xx to a CDN; following further than that
        # would turn a 4-second probe into a multi-minute chase.
        $resp = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing `
            -TimeoutSec $script:HermesMirrorProbeTimeoutSec `
            -MaximumRedirection 1 `
            -ErrorAction Stop
        $code = [int]$resp.StatusCode
        # Any 2xx is reachable; 3xx after one redirect is reachable.
        return ($code -ge 200 -and $code -lt 400)
    } catch {
        # Connection timeout, DNS failure, TLS error, redirect loop, etc.
        # The catch swallows everything because the caller wants a
        # boolean, not a diagnostic.
        return $false
    }
}

function Get-HermesPypiMirror {
    # Returns the URL of the first reachable PyPI mirror, or '' when none of
    # the candidates responded inside the probe window. Honour
    # HERMES_PYPI_MIRROR first (so a user who's already configured their own
    # mirror doesn't see a different one picked for them), then probe.
    param()

    if ($env:HERMES_PYPI_MIRROR) {
        return $env:HERMES_PYPI_MIRROR.TrimEnd('/') + '/'
    }

    foreach ($candidate in $script:HermesPypiMirrorCandidates) {
        if (Test-HermesMirrorReachable -Url $candidate) {
            return $candidate
        }
    }
    # None reachable. Return the canonical URL anyway -- the resolver has
    # already logged which mirrors timed out, so the user knows their
    # network is the problem. Better to let `uv` try with a clear
    # network error than to fail the install on the resolver itself.
    return 'https://pypi.org/simple/'
}

function Get-HermesGithubCloneUrl {
    # Rewrite a github.com URL to a proxy that is currently reachable, or
    # return the input unchanged when no proxy responds. Honours
    # HERMES_GITHUB_PROXY first.
    #
    # Only handles the http(s) URLs the installer actually emits; ssh URLs
    # (git@github.com:...) are returned unchanged because the SSH
    # transport has no mirror equivalent in this list.
    param([string]$Url)

    if ([string]::IsNullOrWhiteSpace($Url)) { return $Url }
    if ($env:HERMES_GITHUB_PROXY) {
        # Strip the canonical github.com hostname; the user-provided proxy
        # is expected to take the same path components.
        return $Url -replace '^https?://github\.com/', $env:HERMES_GITHUB_PROXY.TrimEnd('/') + '/'
    }
    # Only mirror http(s) github.com URLs; SSH clones pass through.
    if ($Url -notmatch '^https?://github\.com/') { return $Url }

    foreach ($proxy in $script:HermesGithubProxyCandidates) {
        if ($proxy -eq 'https://github.com/') { continue }  # checked last
        if (Test-HermesMirrorReachable -Url $proxy) {
            # The replacement pattern is a static prefix; passing it as a
            # variable to -replace is supported, but `-replace` only takes
            # TWO arguments (pattern, replacement) -- constructing the
            # arguments by hand avoids the 3-arg RuntimeException that
            # bit the first attempt.
            $prefix = $proxy.TrimEnd('/') + '/'
            return ($Url -replace '^https?://github\.com/', $prefix)
        }
    }
    return $Url
}

function Get-HermesMirrorStatus {
    # Diagnostic output: print the reachable/blocked status of every
    # candidate. Used by the install script's own "we picked a mirror"
    # line. Returns a hashtable so callers can script against it.
    param()

    $results = @()
    foreach ($candidate in $script:HermesPypiMirrorCandidates) {
        $reachable = Test-HermesMirrorReachable -Url $candidate
        $results += [pscustomobject]@{
            Endpoint  = 'pypi'
            Url       = $candidate
            Reachable = $reachable
        }
    }
    foreach ($proxy in $script:HermesGithubProxyCandidates) {
        $reachable = Test-HermesMirrorReachable -Url $proxy
        $results += [pscustomobject]@{
            Endpoint  = 'github'
            Url       = $proxy
            Reachable = $reachable
        }
    }
    return $results
}

# Public surface: the four functions above. Dot-source callers (install.ps1)
# see everything; nothing else needs to be exported because PS 5.1's
# `Export-ModuleMember` is only meaningful inside an Advanced Function module
# manifest, and this file is a dot-source library, not a module.
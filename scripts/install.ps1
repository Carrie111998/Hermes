# ============================================================================
# Hermes Agent Installer for Windows
# ============================================================================
# Installation script for Windows (PowerShell).
# Uses uv for fast Python provisioning and package management.
#
# Usage:
#   iex (irm https://hermes-agent.nousresearch.com/install.ps1)
#
# Or download and run with options:
#   .\install.ps1 -NoVenv -SkipSetup
#
# ============================================================================

param(
    [switch]$NoVenv,
    [switch]$SkipSetup,
    [string]$Branch = "main",
    # -Commit and -Tag are higher-precedence variants of -Branch for users
    # who need reproducible installs (desktop installer pinning, CI, release
    # bundles).  When set, the repository stage clones $Branch (faster than
    # cloning the full default-branch history) and then `git checkout`s the
    # exact ref.  Precedence: Commit > Tag > Branch.
    [string]$Commit = "",
    # Apply -Commit even when it would roll an existing install BACKWARDS.
    # Without this the repository stage skips a pin that is already an ancestor
    # of HEAD, so a stale baked-in BUILD_PIN_COMMIT can't downgrade a current
    # checkout. Reproducible/CI installs that genuinely want an older SHA on an
    # existing tree pass -ForceCommit.
    [switch]$ForceCommit,
    [string]$Tag = "",
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [string]$InstallDir = $(if ($env:HERMES_HOME) { "$env:HERMES_HOME\hermes-agent" } else { "$env:LOCALAPPDATA\hermes\hermes-agent" }),

    # --- Stage protocol (additive; default invocation behaves as before) ----
    # See the "Stage protocol" section near the bottom of the file for the
    # full contract.  Intended for programmatic drivers (the desktop GUI's
    # onboarding wizard, CI, future install.sh parity, etc.).  CLI users
    # running the canonical `irm | iex` one-liner never touch these flags.
    [switch]$Manifest,
    [string]$Stage,
    [switch]$ProtocolVersion,
    [switch]$NonInteractive,
    [switch]$Json,

    # Print the paths this install would use, as JSON, and exit without
    # touching anything. The first question on any "installer says a path
    # doesn't exist" report is which paths it actually resolved -- especially
    # on profiles Windows exposes through an 8.3 alias, where what the user
    # sees in Explorer and what the installer receives differ.
    #
    #   powershell -File install.ps1 -ShowResolvedPaths
    [switch]$ShowResolvedPaths,

    # --- Ensure mode (dep_ensure.py entry point) ---
    [string]$Ensure = "",
    [switch]$PostInstall,

    # --- Desktop GUI build (opt-in) ---
    # When set, install.ps1 includes Stage-Desktop in the manifest and
    # builds apps/desktop into a launchable Hermes.exe.
    #
    # Why opt-in:
    #   * Hermes-Setup.exe (the signed Tauri bootstrap installer) passes
    #     -IncludeDesktop so a user who installed via the GUI ends up
    #     with a launchable desktop binary.
    #   * The Electron desktop's own bootstrap-runner.ts runs install.ps1
    #     from inside an already-launched Hermes.exe; if THAT recursively
    #     built apps/desktop it would try to overwrite the live Hermes.exe
    #     on disk and fail. The recursive path omits the flag.
    #   * The canonical CLI one-liner (irm | iex) omits the flag too;
    #     terminal users don't need a desktop binary built for them, and
    #     `hermes desktop` already builds on demand.
    [switch]$IncludeDesktop
)

$ErrorActionPreference = "Stop"

# Suppress Invoke-WebRequest's per-chunk progress bar.  Windows PowerShell
# 5.1's progress UI repaints synchronously on every received byte, which
# pegs CPU on a single core and throttles downloads by 10-100x (a 57MB
# PortableGit grab can take 5 minutes with progress on vs 20 seconds
# with progress off, on the same network).  Every IWR call in this
# script is fire-and-forget so we never need to see the bar.  Restored
# automatically when the script exits.
$ProgressPreference = "SilentlyContinue"

# Force the console to UTF-8 so non-ASCII output from native commands
# (e.g. playwright's box-drawing progress bars and download banners,
# git's bullet glyphs, npm's check marks) renders correctly instead of
# as IBM437/Windows-1252 mojibake (sequences like 0xE2 0x95 0x94 box-
# drawing chars decoded under the legacy DOS codepage).  This is a
# DISPLAY-only fix; the underlying bytes are already correct.  We do
# NOT change the file's own encoding (it remains pure ASCII for PS 5.1
# parser compatibility; see comments at the top of the entry-point
# dispatch).  This affects only what the user sees in their terminal
# during this install run, and reverts automatically when the script
# exits and the host's console encoding is restored.
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch {
    # Some constrained PowerShell hosts disallow encoding mutation.
    # Mojibake on output is then cosmetic-only, install still works.
}

# ============================================================================
# 8.3 short-path normalization
# ============================================================================
# Windows generates an 8.3 short alias for a user-profile folder whose name
# contains a space ("First Last" -> FIRST~1.LAS), a dot ("Stone.ZEN8" ->
# STONE~1.ZEN), or an accented character ("Ruben" spelled with an acute e ->
# RUBN~1). It can then expose %TEMP%, %TMP%, %LOCALAPPDATA%, %APPDATA% and
# %USERPROFILE% -- plus everything derived from them, including the default
# HERMES_HOME and InstallDir -- in that short form:
#   C:\Users\FIRST~1.LAS\AppData\Local\Temp
#
# PowerShell's FileSystem provider mishandles the aliased component when such a
# path reaches a provider cmdlet (`Tee-Object -FilePath`, `Out-File`,
# `New-Item`, `Test-Path`), throwing "An object at the specified path
# C:\Users\FIRST~1.LAS does not exist" -- localized on non-English hosts.
# Every Node/Electron stage streams its build log to %TEMP% via Tee-Object and
# the desktop stage probes the binary it produced under the profile-derived
# InstallDir, so the bootstrap aborts even though the artifact built fine.
# The Python/uv stages, which never hand a %TEMP% path to a provider cmdlet,
# sail through -- which is why the failure looks Node-specific.
#
# Expanding every profile-rooted path back to long form once, up front, lets
# every downstream cmdlet and child process see something the provider can
# resolve. Three resolvers, tried in order, because no single one covers every
# host:
#
#   1. kernel32!GetLongPathNameW -- expands any 8.3 component regardless of
#      locale, including the accented-username aliases the COM resolver misses.
#   2. Scripting.FileSystemObject -- fallback for hosts where P/Invoke is
#      blocked.
#   3. Profile-root substitution -- when the volume has 8.3 generation disabled
#      or the alias is stale, neither resolver can expand the name because it
#      no longer maps to anything on disk. The aliased component is always the
#      profile folder itself (everything below it was created long), so swap in
#      a profile root we can prove is long and reattach the tail.
#
# All three degrade to returning the input untouched, so a host where none of
# them apply -- including non-Windows -- behaves exactly as it did before.

$script:LongProfileRoot = $null

function Write-PathDiag {
    # Diagnostics for this block go to stderr, never stdout: the stage protocol
    # hands drivers a single line of JSON on stdout and a stray note would break
    # anything parsing it.
    #
    # Suppressed entirely under -ShowResolvedPaths, which is a machine-readable
    # query: Windows PowerShell 5.1 wraps any native-command stderr in a
    # NativeCommandError and folds it back into the caller's own stream, so a
    # child writing here at all is enough to corrupt a 5.1 caller's capture.
    # The JSON already carries everything these lines say.
    #
    # [Console]::Error.WriteLine specifically -- verified reaching a caller on a
    # windows-latest runner. $host.UI.WriteErrorLine was tried and silently
    # produced nothing there under a non-interactive host.
    param([string]$Message)
    if ($ShowResolvedPaths) { return }
    [Console]::Error.WriteLine("[hermes] $Message")
}

function Get-LongProfileRoot {
    # The user's profile directory in long form, or '' when every source we
    # can reach is itself aliased. Cached: this runs per env var.
    if ($null -ne $script:LongProfileRoot) { return $script:LongProfileRoot }
    $script:LongProfileRoot = ''

    # %USERPROFILE% first: it is what the rest of the install derives from, and
    # on a host handing us aliased paths the .NET known-folder lookup tends to
    # be aliased in exactly the same way. Then the HOMEDRIVE/HOMEPATH pair, then
    # the profile's parent (C:\Users never carries an alias) plus %USERNAME%,
    # which stays the long account name even when every path is short.
    $envProfile = [Environment]::GetEnvironmentVariable('USERPROFILE')
    $shellProfile = [Environment]::GetFolderPath('UserProfile')
    $candidates = @($envProfile, $shellProfile, "$env:HOMEDRIVE$env:HOMEPATH")
    foreach ($anchor in @($envProfile, $shellProfile)) {
        if ($anchor -and $env:USERNAME) {
            $parent = Split-Path -Parent $anchor.TrimEnd('\', '/')
            if ($parent) { $candidates += (Join-Path $parent $env:USERNAME) }
        }
    }

    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        # Trailing separators make Split-Path -Parent return the directory
        # itself, which would silently break the ancestry check downstream.
        $candidate = $candidate.TrimEnd('\', '/')
        if (-not $candidate) { continue }
        if ($candidate -match '~\d') { continue }
        try {
            if (Test-Path -LiteralPath $candidate -PathType Container) {
                $script:LongProfileRoot = $candidate
                break
            }
        } catch {
            # Unreadable candidate (denied, malformed): try the next one.
        }
    }

    # Say which root we landed on. When someone reports "still broken" this is
    # the first thing worth knowing, and it costs one line on the rare path
    # where an alias actually showed up.
    if ($script:LongProfileRoot) {
        Write-PathDiag "long profile root: $script:LongProfileRoot"
    } else {
        Write-PathDiag "no long profile root found; 8.3 paths left as-is (tried: $($candidates -join ', '))"
    }
    return $script:LongProfileRoot
}

function Expand-ShortProfileRoot {
    # Rebuild $Path onto a known-long profile root when its aliased component
    # is the profile folder. Returns $Path unchanged when it isn't, so a custom
    # TEMP on another volume (D:\SHORT~1\Temp) is never rewritten.
    param([string]$Path)

    $longRoot = Get-LongProfileRoot
    if (-not $longRoot) { return $Path }
    $longRootParent = Split-Path -Parent $longRoot
    if (-not $longRootParent) { return $Path }

    $node = $Path
    $tail = ''
    while ($node -and ($node -match '~\d')) {
        $leaf = Split-Path -Leaf $node
        $parent = Split-Path -Parent $node
        if (-not $parent) { return $Path }
        if ($leaf -match '~\d') {
            # Candidate profile folder. Only substitute when it sits in the
            # same directory as the real profile (both C:\Users).
            if ($parent -ne $longRootParent) { return $Path }
            if ($tail) { return (Join-Path $longRoot $tail) }
            return $longRoot
        }
        $tail = if ($tail) { Join-Path $leaf $tail } else { $leaf }
        $node = $parent
    }
    return $Path
}

function ConvertTo-LongPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    # Only 8.3 short names carry a tilde+digit ("~1"); skip every resolver for
    # ordinary long paths, which is the overwhelmingly common case.
    if ($Path -notmatch '~\d') { return $Path }

    # 1. kernel32. Compiled on first use only, so a normal profile never pays
    #    the Add-Type cost (this file is re-entered once per install stage).
    try {
        if (-not ([System.Management.Automation.PSTypeName]'HermesInstall.LongPath').Type) {
            Add-Type -Namespace 'HermesInstall' -Name 'LongPath' -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern int GetLongPathNameW(string lpszShortPath, System.Text.StringBuilder lpszLongPath, int cchBuffer);
'@
        }
        $buffer = New-Object System.Text.StringBuilder 4096
        $length = [HermesInstall.LongPath]::GetLongPathNameW($Path, $buffer, $buffer.Capacity)
        if ($length -gt $buffer.Capacity) {
            $buffer = New-Object System.Text.StringBuilder $length
            $length = [HermesInstall.LongPath]::GetLongPathNameW($Path, $buffer, $buffer.Capacity)
        }
        if ($length -gt 0) {
            $expanded = $buffer.ToString()
            if ($expanded -and $expanded -notmatch '~\d') {
                $script:LastResolver = 'kernel32'
                return $expanded
            }
        }
    } catch {
        # Not Windows, or P/Invoke denied by policy: try the next resolver.
    }

    # 2. COM. Validate the result the same way the kernel32 branch does: this
    # resolver can report success and still hand back a path that carries the
    # alias (observed on a windows-latest runner, where it "resolved"
    # C:\Users\FIRST~1.LAS\... to itself). Accepting that silently is what let a
    # short path reach the provider cmdlets in the first place, so an
    # unexpanded result counts as failure and falls through.
    try {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        $resolved = $null
        if ($fso.FolderExists($Path))   { $resolved = $fso.GetFolder($Path).Path }
        elseif ($fso.FileExists($Path)) { $resolved = $fso.GetFile($Path).Path }
        if ($resolved -and $resolved -notmatch '~\d') {
            $script:LastResolver = 'com'
            return $resolved
        }
    } catch {
        # COM unavailable / locked-down host: try the next resolver.
    }

    # 3. The alias resolves to nothing. Rebuild from a long profile root.
    $rebuilt = Expand-ShortProfileRoot $Path
    $script:LastResolver = if ($rebuilt -ne $Path) { 'profile-root' } else { 'none' }
    return $rebuilt
}

function Set-LongProfileEnvVars {
    # Normalize every profile-rooted variable the install reads, not juë_zöÚ$z{-®éÜj×ÒÒF†R6–ævÆR6÷W&6RöbG'WF‚âV6‚VçG'’Ö27F&ÆPĞ¢27FvRæÖR‡F†R’6öçG&7BG&—fW'2FWVæBöâ’FòF†Rv÷&¶W"gVæ7F–öâF†@Ğ¢2–×ÆVÖVçG2—BâF—FÆV—2v†BT—26†÷s²6FVv÷'–ÆWG2T—2w&÷W Ğ¢27FvW3²æVVG5W6W$–çWFFVÆÇ2T—2'F†—27FvR&ö×G2ÒÒV—F†W"6¶——@Ğ¢2÷"'&ævRFò&÷f–FRç7vW'2æ÷F†W"v’â Ğ¢D–ç7FÆÅ7FvW2Ò€Ğ¢²æÖRÒ'Wb#²F—FÆRÒ$–ç7FÆÆ–ærWb6¶vRÖævW"#²6FVv÷'’Ò'&W&W2#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕWb"ĞĞ¢²æÖRÒ'—F†öâ#²F—FÆRÒ%fW&–g––ær—F†öâE—F†öåfW'6–öâ#²6FVv÷'’Ò'&W&W2#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕ—F†öâ"ĞĞ¢²æÖRÒ&v—B#²F—FÆRÒ$–ç7FÆÆ–ærv—B#²6FVv÷'’Ò'&W&W2#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔv—B"ĞĞ¢²æÖRÒ&æöFR#²F—FÆRÒ$FWFV7F–æræöFRæ§2#²6FVv÷'’Ò'&W&W2#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔæöFR"ĞĞ¢²æÖRÒ'7—7FVÒ×6¶vW2#²F—FÆRÒ$–ç7FÆÆ–ær&—w&WæBff×Vr#²6FVv÷'’Ò'&W&W2#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕ7—7FVÕ6¶vW2"ĞĞ¢²æÖRÒ'&W÷6—F÷'’#²F—FÆRÒ$6Æöæ–ær†W&ÖW2&W÷6—F÷'’#²6FVv÷'’Ò&–ç7FÆÂ#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕ&W÷6—F÷'’"ĞĞ¢²æÖRÒ'fVçb#²F—FÆRÒ$7&VF–ær—F†öâf—'GVÂVçf—&öæÖVçB#²6FVv÷'’Ò&–ç7FÆÂ#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕfVçb"ĞĞ¢²æÖRÒ&FWVæFVæ6–W2#²F—FÆRÒ$–ç7FÆÆ–ær—F†öâFWVæFVæ6–W2#²6FVv÷'’Ò&–ç7FÆÂ#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔFWVæFVæ6–W2"ĞĞ¢²æÖRÒ&æöFRÖFW2#²F—FÆRÒ$–ç7FÆÆ–æræöFRæ§2FWVæFVæ6–W2#²6FVv÷'’Ò&–ç7FÆÂ#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔæöFTFW2"ĞĞ¢Ğ¦–b‚D–æ6ÇVFTFW6·F÷’°Ğ¢2–ç6W'BeDU"æöFRÖFW26òv÷&·76RçÒ—2Ç&VG’–ç7FÆÆVBv†VàĞ¢2F†RFW6·F÷'V–ÆB'Vç2â–ç6W'FVBöæÇ’v†VâW‡Æ–6—FÇ’&WVW7FV@Ğ¢2„†W&ÖW2Õ6WGWæW†R’ÂæWfW"f–F†R—&×Æ–W‚4Ä’öæRÖÆ–æW"àĞ¢D–ç7FÆÅ7FvW2³Ò²æÖRÒ&FW6·F÷#²F—FÆRÒ$'V–ÆF–ærFW6·F÷#²6FVv÷'’Ò&–ç7FÆÂ#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔFW6·F÷"ĞĞ§ĞĞ¢D–ç7FÆÅ7FvW2³Ò€Ğ¢²æÖRÒ'F‚#²F—FÆRÒ$FF–ær†W&ÖW2FòD‚#²6FVv÷'’Ò&f–æÆ—¦R#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕF‚"ĞĞ¢²æÖRÒ&6öæf–r×FV×ÆFW2#²F—FÆRÒ%w&—F–ær6öæf–wW&F–öâFV×ÆFW2#²6FVv÷'’Ò&f–æÆ—¦R#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔ6öæf–uFV×ÆFW2"ĞĞ¢²æÖRÒ'ÆFf÷&Ò×6F·2#²F—FÆRÒ$–ç7FÆÆ–ærÖW76v–ærÆFf÷&Ò4D·2#²6FVv÷'’Ò&f–æÆ—¦R#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÕÆFf÷&Õ6F·2"ĞĞ¢²æÖRÒ&&ö÷G7G&ÖÖ&¶W"#²F—FÆRÒ$Ö&¶–ær–ç7FÆÂ6ö×ÆWFR#²6FVv÷'’Ò&f–æÆ—¦R#²æVVG5W6W$–çWBÒFfÇ6S²v÷&¶W"Ò%7FvRÔ&ö÷G7G&Ö&¶W""ĞĞ¢2–çFW&7F—fR7FvW2â–âæöâÖ–çFW&7F—fRÖöFRF†W6R&V6öÖRæòÖ÷3²F†PĞ¢26ÆÆW"„uT’ò4’’†æFÆW2F†RWV—fÆVçBU‚F†V×6VÇfW2àĞ¢²æÖRÒ&6öæf–wW&R#²F—FÆRÒ$6öæf–wW&–ær’¶W—2æBÖöFVÇ2#²6FVv÷'’Ò'÷7BÖ–ç7FÆÂ#²æVVG5W6W$–çWBÒGG'VS²v÷&¶W"Ò%7FvRÔ6öæf–wW&R"ĞĞ¢²æÖRÒ&vFWv’#²F—FÆRÒ%7F'F–ærÖW76v–ærvFWv’#²6FVv÷'’Ò'÷7BÖ–ç7FÆÂ#²æVVG5W6W$–çWBÒGG'VS²v÷&¶W"Ò%7FvRÔvFWv’"ĞĞ¢Ğ Ğ¢27FvRv÷&¶W'2ÒÒF†–âw&W'2F†BFVÆVvFRFòF†RW†—7F–ær–ç7FÆÂÒ¢ğĞ¢2FW7BÒ¢ò–çfö¶RÒ¢gVæ7F–öç2v†–ÆR&W6W'f–ærF†V—"W'&÷"6VÖçF–72â¶W@Ğ¢226W&FRÆ–W"6òF†RW†—7F–ærgVæ7F–öç2&VÖ–â6ÆÆ&ÆRF—&V7FÇĞ¢2††VÇgVÂf÷"öæRÖöfb&V6÷fW'“¢â–ç7FÆÂç3²–ç7FÆÂÕfVçf’àĞ¢0Ğ¢27FvW2F†BFWVæBöâWb†ç—F†–ærgFW"7FvRÕWb’6ÆÂ&W6öÇfRÕWd6Ö@Ğ¢2f—'7B6òF†W’v÷&²–â7&÷72×&ö6W72G&—fW"ÖöFRv†W&RG67&—C¥Wd6Ö@Ğ¢26WB'’7FvRÕWb–â6–&Æ–ær÷vW'6†VÆÂ&ö6W72—2æ÷Bf—6–&ÆR†W&RàĞ¢2&W6öÇfRÕWd6ÖB—2f7BæòÖ÷v†VâG67&—C¥Wd6ÖB—2Ç&VG’÷VÆFV@Ğ¢2‡F†RFVfVÇBÖ–çfö6F–öâ66Rv†W&RÖ–â'Vç2WfW'—F†–ær–âöæPĞ¢2&ö6W72’ÂæBF‡&÷w26ÆVæÇ’–bWbG'VÇ’—6âwB–ç7FÆÆVB–WBàĞ¦gVæ7F–öâ7FvRÕWb²–b‚Öæ÷B„–ç7FÆÂÕWb’’²F‡&÷r'Wb–ç7FÆÆF–öâf–ÆVB"ÒĞĞ¦gVæ7F–öâ7FvRÕ—F†öâ²&W6öÇfRÕWd6ÖC²–b‚Öæ÷B…FW7BÕ—F†öâ’’²F‡&÷r%—F†öâE—F†öåfW'6–öâæ÷Bf–Æ&ÆR"ÒĞĞ¦gVæ7F–öâ7FvRÔv—B°Ğ¢–b‚Öæ÷B„–ç7FÆÂÔv—B’’°Ğ¢–b‚G67&—C¤v—D–ç7FÆÄf–ÇW&U&V6öâ’²F‡&÷rG67&—C¤v—D–ç7FÆÄf–ÇW&U&V6öâĞĞ¢F‡&÷r$v—Bæ÷Bf–Æ&ÆRæBWFòÖ–ç7FÆÂf–ÆVBÒÒ–ç7FÆÂg&öÒ‡GG3¢òöv—B×66Òæ6öÒöF÷væÆöB÷v–âF†Vâ&R×'Vâ Ğ¢ĞĞ§ĞĞ¢2æöFR—2÷F–öæÂ†'&÷w6W"FööÇ2FVw&FRw&6VgVÆÇ’v—F†÷WB—B’â7W&f6PĞ¢2f–ÇW&RFòF†R¥4ôâ6öçG&7B26¶—VC×G'VRò&V6öâ&F†W"F†âö³×G'VRÀĞ¢26òuT’G&—fW"6öç7VÖ–ærF†RÖæ–fW7B6âF—7F–æwV—6‚&æöFR&VG’"g&öĞĞ¢2&æöFRÖ—76–ær"â–ç7FÆÂfÆ÷r6öçF–çVW2V—F†W"v’ÒÒÖF6†W2F†PĞ¢2W†—7F–ærw&—FRÔ6ö×ÆWF–öâ&V†f–÷"F†B&–çG2$æ÷FS¢æöFRæ§26÷VÆ@Ğ¢2æ÷B&R–ç7FÆÆVB"†–çB–ç7FVBöb&÷'F–æràĞ¦gVæ7F–öâ7FvRÔæöFR°Ğ¢–b‚Öæ÷B…FW7BÔæöFR’’°Ğ¢G67&—C¥õ7FvU6¶—VE&V6öâÒ$æöFRæ§2æ÷Bf–Æ&ÆS²'&÷w6W"FööÇ2v–ÆÂ&RVæf–Æ&ÆRVçF–ÂæöFR—2–ç7FÆÆVBÖçVÆÇ’g&öÒ‡GG3¢òöæöFV§2æ÷&röVâöF÷væÆöBò Ğ¢ĞĞ§ĞĞ¦gVæ7F–öâ7FvRÕ7—7FVÕ6¶vW2²–ç7FÆÂÕ7—7FVÕ6¶vW2ĞĞ¦gVæ7F–öâ7FvRÕ&W÷6—F÷'’²–ç7FÆÂÕ&W÷6—F÷'’ĞĞ¦gVæ7F–öâ7FvRÕfVçb²&W6öÇfRÕWd6ÖC²–ç7FÆÂÕfVçbĞĞ¦gVæ7F–öâ7FvRÔFWVæFVæ6–W2²&W6öÇfRÕWd6ÖC²–ç7FÆÂÔFWVæFVæ6–W2ĞĞ¦gVæ7F–öâ7FvRÔæöFTFW2²–ç7FÆÂÔæöFTFW2ĞĞ¦gVæ7F–öâ7FvRÔFW6·F÷²–ç7FÆÂÔFW6·F÷fö–6TFW3²–ç7FÆÂÔFW6·F÷ĞĞ¦gVæ7F–öâ7FvRÕF‚²6WBÕF…f&–&ÆRĞĞ¦gVæ7F–öâ7FvRÔ6öæf–uFV×ÆFW2²6÷’Ô6öæf–uFV×ÆFW2ĞĞ¦gVæ7F–öâ7FvRÕÆFf÷&Õ6F·2²&W6öÇfRÕWd6ÖC²–ç7FÆÂÕÆFf÷&Õ6F·2ĞĞ¦gVæ7F–öâ7FvRÔ&ö÷G7G&Ö&¶W"²w&—FRÔ&ö÷G7G&Ö&¶W"ĞĞ¦gVæ7F–öâ7FvRÔ6öæf–wW&R²–çfö¶RÕ6WGWv—¦&BĞĞ¦gVæ7F–öâ7FvRÔvFWv’²7F'BÔvFWv”–d6öæf–wW&VBĞĞ Ğ¦gVæ7F–öâvWBÔ–ç7FÆÅ7FvR°Ğ¢&Ò…·7G&–æuÒDæÖRĞ¢f÷&V6‚‚G2–âD–ç7FÆÅ7FvW2’°Ğ¢–b‚G2äæÖRÖWDæÖR’²&WGW&âG2ĞĞ¢ĞĞ¢&WGW&âFçVÆÀĞ§ĞĞ Ğ¦gVæ7F–öâ7FWÔ÷WDöd–ç7FÆÄF—"°Ğ¢2v–æF÷w2&VgW6W2FòFVÆWFRF—&V7F÷'’ç’6†VÆÂ—27W'&VçFÇ’6Bv@Ğ¢2–ç6–FRÒÒæB6–ÆVçFÇ’ÆVfW2÷'†âf–ÆW2&V†–æBÂv†–6‚F†VâvVFvPĞ¢2&—2F†—2fÆ–Bv—B&Wò"&ö&W2öâ&RÖ–ç7FÆÂâ†&ÖÆW72v†VâF†PĞ¢26ÆÆW"&âF†R–ç7FÆÆW"g&öÒ6öÖWv†W&RVÇ6RàĞ¢G'’°Ğ¢F7W'&VçE&W6öÇfVBÒ„vWBÔÆö6F–öâ’å&÷f–FW%F€Ğ¢F–ç7FÆÅ&W6öÇfVBÒFçVÆÀĞ¢–b…FW7BÕF‚D–ç7FÆÄF—"’°Ğ¢F–ç7FÆÅ&W6öÇfVBÒ…&W6öÇfRÕF‚D–ç7FÆÄF—"ÔW'&÷$7F–öâ6–ÆVçFÇ”6öçF–çVR’å&÷f–FW%F€Ğ¢ĞĞ¢–b‚F–ç7FÆÅ&W6öÇfVBÖæBF7W'&VçE&W6öÇfVBåFôÆ÷vW"‚’å7F'G5v—F‚‚F–ç7FÆÅ&W6öÇfVBåFôÆ÷vW"‚’’’°Ğ¢w&—FRÔ–æfò%7FW–ær÷WBöbD–ç7FÆÄF—"6òv–æF÷w26â&WÆ6Rf–ÆW2F†W&R–bæVVFVBâââ Ğ¢6WBÔÆö6F–öâFVçc¥U4U%$ôd”ÄPĞ¢ĞĞ¢Ò6F6‚·ĞĞ§ĞĞ Ğ¦gVæ7F–öâ–çfö¶RÕ7FvR°Ğ¢&Ò€Ğ¢µ&ÖWFW"„ÖæFF÷'“ÒGG'VR•Ò¶†6‡F&ÆUÒE7FvTFV`Ğ¢Ğ Ğ¢2&Vg&W6‚D‚g&öÒ&Vv—7G'’6òF†—27FvR6VW2&–æ&–W2–ç7FÆÆVB'Ğ¢2&–÷"7FvW2ÂWfVâv†VâV6‚7FvR'Vç2–â—G2÷vâ÷vW'6†VÆÂ&ö6W72àĞ¢2æòÖ÷–â6÷7B×&VÆWfçB66W2†FVfVÇB–çfö6F–öâF‚7–æ72öæ6RW Ğ¢2f÷&V6‚73²7&÷72×&ö6W72G&—fW'2vWBF†RæV6W76'’g&W6†Væ–ær’àĞ¢7–æ2ÔVçeF€Ğ Ğ¢2W"×7FvR6ögB×6¶—6†ææVÂâv÷&¶W"6â÷VÆFPĞ¢2G67&—C¥õ7FvU6¶—VE&V6öâFò7W&f6R'&âÂ'WBF†RF†–ær—Bv0Ğ¢27W÷6VBFò6WBW—2æ÷Bf–Æ&ÆR"26¶—VC×G'VR–âF†R¥4ôàĞ¢2g&ÖRÂv—F†÷WBF‡&÷v–ærâW6VB'’7FvRÔæöFR6òF†R–ç7FÆÂfÆ÷pĞ¢2FöW6âwB&÷'Bv†Vââ÷F–öæÂ6&–Æ—G’—2Ö—76–ærv†–ÆR7F–ÆÀĞ¢2&V–ær†öæW7B–âF†R&÷Fö6öÂ6öçG&7Bâ&W6WB&Vf÷&RV6‚7FvR6ğĞ¢2&–÷"7FvRw2&V6öâ6âæWfW"ÆV²–çFòÆFW"7FvRw2g&ÖRàĞ¢G67&—C¥õ7FvU6¶—VE&V6öâÒFçVÆÀĞ Ğ¢G7F'BÒ´FFUF–ÖUÓ£¥WF4æ÷pĞ¢G&W7VÇBÒ°Ğ¢7FvRÒE7FvTFVbäæÖPĞ¢ö²ÒFfÇ6PĞ¢6¶—VBÒFfÇ6PĞ¢&V6öâÒFçVÆÀĞ¢GW&F–öåö×2Ò Ğ¢ĞĞ Ğ¢G'’°Ğ¢bE7FvTFVbåv÷&¶W Ğ¢G&W7VÇBæö²ÒGG'VPĞ¢–b‚G67&—C¥õ7FvU6¶—VE&V6öâ’°Ğ¢G&W7VÇBç6¶—VBÒGG'VPĞ¢G&W7VÇBç&V6öâÒG67&—C¥õ7FvU6¶—VE&V6öàĞ¢ĞĞ¢Ò6F6‚°Ğ¢G&W7VÇBæö²ÒFfÇ6PĞ¢G&W7VÇBç&V6öâÒ"Eò Ğ¢F‡&÷pĞ¢Òf–æÆÇ’°Ğ¢G&W7VÇBæGW&F–öåö×2Ò¶–çEÒ…´FFUF–ÖUÓ£¥WF4æ÷rÒG7F'B’åF÷FÄÖ–ÆÆ—6V6öæG0Ğ¢–b‚D§6öâÖ÷"E7FvR’°Ğ¢2–â7FvRÖG&—fW"ÖöFRWfW'’7FvRVÖ—G2¥4ôâÆ–æR6òF†PĞ¢26ÆÆW"6â7G&VÒ&öw&W72â–âFVfVÇB–çFW&7F—fRÖöFRvPĞ¢27F’6–ÆVçB†W&R‡F†Rv÷&¶W"Ç&VG’w&÷FR‡VÖâ÷WGWB’àĞ¢G&W7VÇBÂ6öçfW'EFòÔ§6öâÔ6ö×&W72Âw&—FRÔ÷WGW@Ğ¢2FVÆÂF†RVçG'’×ö–çB6F6‚F†BvRwfRÇ&VG’VÖ—GFVBĞ¢2g&ÖRf÷"F†—2f–ÇW&R‡v†VâG&W7VÇBæö²ÒFfÇ6R’Â6ò—@Ğ¢2FöW6âwBF÷V&ÆRÖVÖ—B6V6öæB¥4ôâö&¦V7BæB'&V²F†PĞ¢2öæRÖÆ–æR×W"×7FvR6öçG&7BF†RG&—fW"&÷Fö6öÂ&öÖ—6W2àĞ¢–b‚Öæ÷BG&W7VÇBæö²’°Ğ¢G67&—C¥õ7FvTVÖ—GFVDW'&÷$g&ÖRÒGG'VPĞ¢ĞĞ¢ĞĞ¢ĞĞ§ĞĞ Ğ¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞĞ¢2Ö–àĞ¢2ÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞĞ Ğ¦gVæ7F–öâ–çfö¶RÔÆÅ7FvW2°Ğ¢7FWÔ÷WDöd–ç7FÆÄF— Ğ¢f÷&V6‚‚G2–âD–ç7FÆÅ7FvW2’°Ğ¢–çfö¶RÕ7FvRÕ7FvTFVbG0Ğ¢ĞĞ§ĞĞ Ğ¦gVæ7F–öâ–çfö¶RÔVç7W&TÖöFR°Ğ¢&Ò…·7G&–æuÒDFW2Ğ¢FFWÆ—7BÒDFW2×7Æ—B"Â Ğ¢f÷&V6‚‚FFW–âFFWÆ—7B’°Ğ¢FFWÒFFWåG&–Ò‚Ğ¢7v—F6‚‚FFW’°Ğ¢&æöFR"°Ğ¢·fö–EÒ…FW7BÔæöFRĞ¢–b‚Öæ÷BG67&—C¤†4æöFR’°Ğ¢w&—FRÔW'"$æöFRæ§26÷VÆBæ÷B&R–ç7FÆÆVB Ğ¢W†—BĞ¢ĞĞ¢ĞĞ¢&'&÷w6W""°Ğ¢·fö–EÒ…FW7BÔæöFRĞ¢–b‚G67&—C¤†4æöFR’°Ğ¢–ç7FÆÂÔvVçD'&÷w6W Ğ¢ÒVÇ6R°Ğ¢w&—FRÔW'"$æöFRæ§2—2&WV—&VBf÷"'&÷w6W"FööÇ2'WB6÷VÆBæ÷B&R–ç7FÆÆVB Ğ¢W†—BĞ¢ĞĞ¢ĞĞ¢'&—w&W"°Ğ¢w&—FRÔ–æfò'&—w&W¢–ç7FÆÂÖçVÆÇ’öâv–æF÷w2‡66ö÷–ç7FÆÂ&—w&W’ Ğ¢ĞĞ¢&ff×Vr"°Ğ¢w&—FRÔ–æfò&ff×Vs¢–ç7FÆÂÖçVÆÇ’öâv–æF÷w2‡66ö÷–ç7FÆÂff×Vr’ Ğ¢ĞĞ¢FVfVÇB°Ğ¢w&—FRÔW'"%Væ¶æ÷vâFWVæFVæ7“¢FFW Ğ¢W†—BĞ¢ĞĞ¢ĞĞ¢ĞĞ§ĞĞ Ğ¦gVæ7F–öâ–çfö¶RÕ÷7D–ç7FÆÄÖöFR°Ğ¢w&—FRÔ–æfò%'Vææ–ær÷7BÖ–ç7FÆÂ6WGWâââ Ğ¢–çfö¶RÔVç7W&TÖöFRÔFW2&æöFRÆ'&÷w6W" Ğ¢w&—FRÔ–æfò%÷7BÖ–ç7FÆÂ6ö×ÆWFR Ğ§ĞĞ Ğ¦gVæ7F–öâÖ–â°Ğ¢w&—FRÔ&ææW Ğ¢–çfö¶RÔÆÅ7FvW0Ğ¢–b‚Öæ÷BD§6öâ’°Ğ¢w&—FRÔ6ö×ÆWF–öàĞ¢ÒVÇ6R°Ğ¢²ö²ÒGG'VS²&÷Fö6öÅ÷fW'6–öâÒD–ç7FÆÅ7FvU&÷Fö6öÅfW'6–öâÒÂ6öçfW'EFòÔ§6öâÔ6ö×&W72Âw&—FRÔ÷WGW@Ğ¢ĞĞ§ĞĞ Ğ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞĞ¢2VçG'’×ö–çBF—7F6€Ğ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞĞ¢0Ğ¢2ÆÂ'&æ6†W2gVææVÂF‡&÷Vv‚öæRG'’ö6F6‚6òW'&÷'2FöâwB¶–ÆÂâ—&ÒÀĞ¢2–W†÷vW%6†VÆÂ6W76–öâÂæB6òf–ÇW&W2–â7FvRÖG&—fW"ÖöFR&öGV6RĞ¢27G'V7GW&VB¥4ôâW'&÷"g&ÖR–ç7FVBöb&&RW†6WF–öâàĞ Ğ§G'’°Ğ¢–b‚DVç7W&RÖæR""’°Ğ¢–b‚E4&÷VæE&ÖWFW'2ä6öçF–ç4¶W’‚%7FvR"’’°Ğ¢w&—FRÔW'"$6ææ÷BW6RÔVç7W&RæBÕ7FvR6–×VÇFæV÷W6Ç’ Ğ¢W†—BĞ¢ĞĞ¢–çfö¶RÔVç7W&TÖöFRÔFW2DVç7W&PĞ¢W†—B Ğ¢ĞĞ¢–b‚E÷7D–ç7FÆÂ’°Ğ¢–çfö¶RÕ÷7D–ç7FÆÄÖöFPĞ¢W†—B Ğ¢ĞĞ Ğ¢–b‚E&÷Fö6öÅfW'6–öâ’°Ğ¢w&—FRÔ÷WGWBD–ç7FÆÅ7FvU&÷Fö6öÅfW'6–öàĞ¢W†—B Ğ¢ĞĞ Ğ¢–b‚E6†÷u&W6öÇfVEF‡2’°Ğ¢G67&—C¥&W6öÇfVEF…&W÷'BÂ6öçfW'EFòÔ§6öâÔFWF‚RÔ6ö×&W72Âw&—FRÔ÷WGW@Ğ¢W†—B Ğ¢ĞĞ Ğ¢–b‚DÖæ–fW7B’°Ğ¢G–ÆöBÒ°Ğ¢&÷Fö6öÅ÷fW'6–öâÒD–ç7FÆÅ7FvU&÷Fö6öÅfW'6–öàĞ¢7FvW2Ò‚D–ç7FÆÅ7FvW2Âf÷$V6‚Ôö&¦V7B°Ğ¢°Ğ¢æÖRÒEòäæÖPĞ¢F—FÆRÒEòåF—FÆPĞ¢6FVv÷'’ÒEòä6FVv÷'Ğ¢æVVG5÷W6W%ö–çWBÒEòäæVVG5W6W$–çW@Ğ¢ĞĞ¢ÒĞ¢ĞĞ¢G–ÆöBÂ6öçfW'EFòÔ§6öâÔFWF‚RÔ6ö×&W72Âw&—FRÔ÷WGW@Ğ¢W†—B Ğ¢ĞĞ Ğ¢2W6R4&÷VæE&ÖWFW'2&F†W"F†âE7FvRG'WF†–æW726òF†BàĞ¢2W‡Æ–6—BÕ7FvR"&g&öÒÖ—6&V†f–ærG&—fW"FöW6âwBfÆÂF‡&÷Vv€Ğ¢2FòF†RgVÆÂÖ–ç7FÆÂÖ–âF‚æB6–ÆVçFÇ’¶–6²öfbFW7G'V7F—fPĞ¢2÷W&F–öââV×G’7G&–ær—26öçG&7Bf–öÆF–öã²7W&f6R—B0Ğ¢2Væ¶æ÷vâ×7FvRW†—B"v—F‚7G'V7GW&VB¥4ôâg&ÖRàĞ¢–b‚E4&÷VæE&ÖWFW'2ä6öçF–ç4¶W’‚%7FvR"’’°Ğ¢FFVbÒvWBÔ–ç7FÆÅ7FvRÔæÖRE7FvPĞ¢–b‚Öæ÷BFFVb’°Ğ¢FW'"Ò°Ğ¢ö²ÒFfÇ6PĞ¢7FvRÒE7FvPĞ¢&V6öâÒ'Væ¶æ÷vâ7FvS¢E7FvRâ'Vâ–ç7FÆÂç3ÔÖæ–fW7BFòÆ—7BfÆ–B7FvW2â Ğ¢ĞĞ¢FW'"Â6öçfW'EFòÔ§6öâÔ6ö×&W72Âw&—FRÔ÷WGW@Ğ¢W†—B Ğ¢ĞĞ¢7FWÔ÷WDöd–ç7FÆÄF— Ğ¢–çfö¶RÕ7FvRÕ7FvTFVbFFV`Ğ¢W†—B Ğ¢ĞĞ Ğ¢2FVfVÇC¢gVÆÂ–ç7FÆÂ‡FöF’w2&V†f–÷"ÂÇW2÷F–öæÂÔæöä–çFW&7F—fPĞ¢2æBÔ§6öâÆ–W&VBöâ'’F†R&×2&÷fR’àĞ¢Ö–àĞ§Ò6F6‚°Ğ¢–b‚D§6öâÖ÷"E7FvR’°Ğ¢27FvRÖG&—fW"ÖöFS¢6ÆÆW"vçG2¥4ôâF†W’6â'6RâVÖ—BĞ¢27G'V7GW&VBW'&÷"g&ÖRæBW†—Bæöâ×¦W&òÒÒ%UBöæÇ’–`Ğ¢2–çfö¶RÕ7FvRF–FâwBÇ&VG’VÖ—BöæRf÷"F†—26ÖRf–ÇW&RàĞ¢2F†R–ææW"f–æÆÇ’VÖ—G2F†RWF†÷&—FF—fRW"×7FvRg&ÖPĞ¢2‡v—F‚GW&F–öåö×2²6¶—VBf–VÆG2“²6V6öæBVÖ—B†W&PĞ¢2v÷VÆB&öGV6RGvò6öæ6FVæFVB¥4ôâö&¦V7G2öâ7FF÷WBæ@Ğ¢2'&V²G&—fW'2F†B'6RöæRÖÆ–æR×W"Ö–çfö6F–öâàĞ¢–b‚Öæ÷BG67&—C¥õ7FvTVÖ—GFVDW'&÷$g&ÖR’°Ğ¢FW'"Ò°Ğ¢ö²ÒFfÇ6PĞ¢7FvRÒ–b‚E7FvR’²E7FvRÒVÇ6R²FçVÆÂĞĞ¢&V6öâÒ"Eò Ğ¢ĞĞ¢FW'"Â6öçfW'EFòÔ§6öâÔ6ö×&W72Âw&—FRÔ÷WGW@Ğ¢ĞĞ¢W†—BĞ¢ĞĞ Ğ¢2–çFW&7F—fRÖöFS¢¶VWFöF’w2g&–VæFÇ’&V6÷fW'’†–çBàĞ¢w&—FRÔ†÷7B" Ğ¢w&—FRÔW'"$–ç7FÆÆF–öâf–ÆVC¢Eò Ğ¢w&—FRÔ†÷7B" Ğ¢w&—FRÔ–æfò$–bF†RW'&÷"—2Væ6ÆV"ÂG'’F÷væÆöF–æræB'Vææ–ærF†R67&—BF—&V7FÇ“¢ Ğ¢w&—FRÔ†÷7B"–çfö¶RÕvV%&WVW7BÕW&’v‡GG3¢òö†W&ÖW2ÖvVçBææ÷W7&W6V&6‚æ6öÒö–ç7FÆÂç3rÔ÷WDf–ÆR–ç7FÆÂç3"Ôf÷&Vw&÷VæD6öÆ÷"–VÆÆ÷pĞ¢w&—FRÔ†÷7B"åÆ–ç7FÆÂç3"Ôf÷&Vw&÷VæD6öÆ÷"–VÆÆ÷pĞ¢w&—FRÔ†÷7B" Ğ§ĞĞ 
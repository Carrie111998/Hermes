<#>
.SYNOPSIS
    Register minimal boot/logon autostart tasks for Tailscale-centric Hermes.
    Replaces the legacy restart-hermes-autostart-admin.ps1 with a clean, minimal set.

.DESCRIPTION
    Registers only the essential tasks for a Tailscale-centric architecture:
    - Boot tasks: Tailscale Serve config, WebUI, Memory Graph
    - Logon tasks: LINE Personal Bridge recovery, Hermes Desktop
    All legacy ngrok, hypura, full-stack, dashboard tasks are disabled.

.PARAMETER Unregister
    Remove all Hermes boot/logon tasks (cleanup).

.PARAMETER CleanupOnly
    Only disable/remove legacy tasks, don't register new ones.

.EXAMPLE
    # Register new minimal tasks
    powershell -NoProfile -ExecutionPolicy Bypass -File Register-TailscaleCentricHermesAutostart.ps1

.EXAMPLE
    # Unregister all
    powershell ... -File Register-TailscaleCentricHermesAutostart.ps1 -Unregister

.EXAMPLE
    # Cleanup legacy only
    powershell ... -File Register-TailscaleCentricHermesAutostart.ps1 -CleanupOnly
#>

[CmdletBinding()]
param(
    [switch]$Unregister,
    [switch]$CleanupOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "[$stamp] $Message"
}

function New-TaskSettings {
    New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
}

function Unregister-Task {
    param([Parameter(Mandatory = $true)][string]$Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Step "Unregistered task: $Name"
        return $true
    }
    return $false
}

function Register-BootTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [int]$DelaySeconds = 30
    )
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command $Command" -WorkingDirectory $WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -AtStartup
    if ($DelaySeconds -gt 0) { $trigger.Delay = "PT${DelaySeconds}S" }
    $principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType S4U -RunLevel Highest
    $settings = New-TaskSettings
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
    Write-Step "Registered BOOT task: $TaskName (delay ${DelaySeconds}s)"
}

function Register-LogonTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [int]$DelaySeconds = 30
    )
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command $Command" -WorkingDirectory $WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    if ($DelaySeconds -gt 0) { $trigger.Delay = "PT${DelaySeconds}S" }
    $principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
    $settings = New-TaskSettings
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
    Write-Step "Registered LOGON task: $TaskName (delay ${DelaySeconds}s)"
}

# Resolve paths
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$HermesHome = Join-Path $env:USERPROFILE '.hermes'
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Script paths
$ManageTailscaleScript = Join-Path $ScriptDir 'Manage-HermesTailscaleServe.ps1'
$TestHealthScript = Join-Path $ScriptDir 'Test-HermesTailscaleHealth.ps1'
$WebUiScript = 'C:\Users\downl\AppData\Local\HermesWebUI\Start-HermesWebUI.ps1'
$MemoryGraphScript = Join-Path $ScriptDir 'start-obsidian-memory-graph-server.ps1'
$LineRecoveryCmd = 'C:\Users\downl\Desktop\line-bot-sdk-go-workspace\line-bot-sdk-go\logs\personal_line_bridge\run_personal_line_bridge_recovery.cmd'
$DesktopExe = 'C:\Users\downl\Documents\New project\hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe'

# Legacy tasks to disable/remove
$legacyBootTasks = @(
    'HermesGatewayBootAutoStart',
    'HermesHypuraHarnessBootAutoStart',
    'HermesLineNgrokBootAutoStart',
    'HermesMemoryGraphBootAutoStart',
    'HermesWebUIBootAutoStart',
    'HermesDashboardBootAutoStart',
    'HermesTailscaleServeBootUpdate'
)

$legacyLogonTasks = @(
    'HermesGatewayAutoStart',
    'HermesHypuraHarnessAutoStart',
    'HermesLineNgrokAutoStart',
    'HermesDashboardAutoStart',
    'HermesObsidianMemoryGraphServer',
    'HermesWebUIBootAutoStart',
    'HermesWebUINativeAutoStart'
)

$legacyStartupFiles = @(
    'HermesAgentGatewayAutoStart.cmd',
    'HypuraAutoStart.cmd',
    'OpenClaw Gateway (desktop-stack).cmd',
    'SuperGemma4-LlamaServer.vbs',
    'bpNEAT_PipelineRecovery.vbs',
    'Hermes_Gateway_secretary.vbs',
    'HermesBootStack.lnk'
)

if ($Unregister) {
    Write-Step "=== UNREGISTERING ALL HERMES TASKS ==="
    foreach ($name in $legacyBootTasks + $legacyLogonTasks + @(
        'HermesTailscaleServeBootConfigure',
        'HermesWebUIBootAutoStart',
        'HermesMemoryGraphBootAutoStart',
        'HermesLinePersonalBridgeAutoRecovery',
        'HermesDesktopAutoStart',
        'Hermes_Gateway'
    )) {
        Unregister-Task -Name $name
    }
    $startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    foreach ($file in $legacyStartupFiles) {
        $path = Join-Path $startupDir $file
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
            Write-Step "Removed startup file: $file"
        }
    }
    Write-Step "Unregister complete."
    exit 0
}

if ($CleanupOnly) {
    Write-Step "=== CLEANING UP LEGACY TASKS ONLY ==="
    foreach ($name in $legacyBootTasks + $legacyLogonTasks) {
        Unregister-Task -Name $name
    }
    $startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    foreach ($file in $legacyStartupFiles) {
        $path = Join-Path $startupDir $file
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
            Write-Step "Removed startup file: $file"
        }
    }
    Write-Step "Cleanup complete. Run without -CleanupOnly to register new tasks."
    exit 0
}

Write-Step "=== REGISTERING TAILSCALE-CENTRIC HERMES AUTOSTART ==="

# Cleanup legacy first
foreach ($name in $legacyBootTasks + $legacyLogonTasks) {
    Unregister-Task -Name $name
}
$startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
foreach ($file in $legacyStartupFiles) {
    $path = Join-Path $startupDir $file
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
        Write-Step "Removed legacy startup file: $file"
    }
}

# Verify required scripts exist
$required = @($ManageTailscaleScript, $WebUiScript, $MemoryGraphScript, $LineRecoveryCmd, $DesktopExe)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required script not found: $path"
    }
}

$envPrefix = "`$env:HERMES_HOME='$HermesHome'; "

# --- BOOT TASKS (run at system startup, before login) ---

# 1. Tailscale Serve Configuration (first, so routes exist when services start)
Register-BootTask `
    -TaskName 'HermesTailscaleServeBootConfigure' `
    -Description 'Configure all Tailscale Serve routes for Hermes services at boot' `
    -Command "$envPrefix& '$ManageTailscaleScript' -Action Configure -WaitSeconds 10" `
    -WorkingDirectory $RepoRoot `
    -DelaySeconds 20

# 2. Hermes WebUI (depends on Tailscale routes)
Register-BootTask `
    -TaskName 'HermesWebUIBootAutoStart' `
    -Description 'Boot auto-start Hermes WebUI from canonical checkout' `
    -Command "$envPrefix& '$WebUiScript'" `
    -WorkingDirectory 'C:\Users\downl\Documents\New project\hermes-WebUI' `
    -DelaySeconds 40

# 3. Memory Graph (optional but useful)
Register-BootTask `
    -TaskName 'HermesMemoryGraphBootAutoStart' `
    -Description 'Boot auto-start Obsidian memory-graph Go HTTP server (:8765)' `
    -Command "$envPrefix& '$MemoryGraphScript'" `
    -WorkingDirectory $RepoRoot `
    -DelaySeconds 50

# --- LOGON TASKS (run at user logon) ---

# 4. LINE Personal Bridge Recovery (existing proven task, keep as-is)
# This is already registered as HermesLinePersonalBridgeAutoRecovery
# Just ensure it's enabled
$lineTask = Get-ScheduledTask -TaskName 'HermesLinePersonalBridgeAutoRecovery' -ErrorAction SilentlyContinue
if ($lineTask -and $lineTask.State -eq 'Disabled') {
    Enable-ScheduledTask -TaskName 'HermesLinePersonalBridgeAutoRecovery' | Out-Null
    Write-Step "Enabled existing logon task: HermesLinePersonalBridgeAutoRecovery"
}

# 5. Hermes Desktop (existing, keep as-is)
$desktopTask = Get-ScheduledTask -TaskName 'HermesDesktopAutoStart' -ErrorAction SilentlyContinue
if ($desktopTask -and $desktopTask.State -eq 'Disabled') {
    Enable-ScheduledTask -TaskName 'HermesDesktopAutoStart' | Out-Null
    Write-Step "Enabled existing logon task: HermesDesktopAutoStart"
}

# 6. Health Check Verification (boot, after services start)
Register-BootTask `
    -TaskName 'HermesTailscaleHealthBootVerify' `
    -Description 'Verify Tailscale Serve routes and service health after boot' `
    -Command "$envPrefix& '$TestHealthScript' -CriticalOnly" `
    -WorkingDirectory $RepoRoot `
    -DelaySeconds 80

Write-Step ""
Write-Step "=== REGISTRATION COMPLETE ==="
Write-Step "Boot tasks:"
Write-Step "  HermesTailscaleServeBootConfigure (delay 20s)"
Write-Step "  HermesWebUIBootAutoStart (delay 40s)"
Write-Step "  HermesMemoryGraphBootAutoStart (delay 50s)"
Write-Step "  HermesTailscaleHealthBootVerify (delay 80s)"
Write-Step "Logon tasks (existing, ensured enabled):"
Write-Step "  HermesLinePersonalBridgeAutoRecovery"
Write-Step "  HermesDesktopAutoStart"
Write-Step ""
Write-Step "To verify: Get-ScheduledTask -TaskName 'Hermes*' | Format-Table TaskName,State,TaskPath"
Write-Step "To test boot: Restart-Computer (or run tasks manually with Start-ScheduledTask)"
Write-Step "To health check: powershell -File '$TestHealthScript' -Verbose"
Write-Step ""
Write-Step "Rollback: powershell -File '$($MyInvocation.MyCommand.Path)' -Unregister"
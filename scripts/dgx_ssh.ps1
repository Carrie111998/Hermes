[CmdletBinding()]
param(
    [ValidateSet("probe", "auth", "bootstrap", "exec")]
    [string]$Mode = "probe",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemoteCommand
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "dgx_ssh.sh"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Missing WSL wrapper: $scriptPath"
}

$wslInputPath = $scriptPath.Replace('\', '/')
$wslScriptPath = $null
try {
    $resolvedPath = & wsl.exe -d Ubuntu -- wslpath -a $wslInputPath 2>$null
    if ($LASTEXITCODE -eq 0 -and $null -ne $resolvedPath) {
        $candidatePath = $resolvedPath | Select-Object -First 1
        if ($null -ne $candidatePath) {
            $candidatePath = $candidatePath.ToString().Trim()
            if (-not [string]::IsNullOrWhiteSpace($candidatePath)) {
                $wslScriptPath = $candidatePath
            }
        }
    }
} catch {
    $wslScriptPath = $null
}

$dgxTarget = "cwliao@140.96.58.171"
$sshExe = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"

function Get-NativeSshOptions {
    $options = @(
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "AddKeysToAgent=yes"
    )
    $identity = $env:HERMES_DGX_IDENTITY
    if ([string]::IsNullOrWhiteSpace($identity)) {
        $identity = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
    } else {
        $identity = [Environment]::ExpandEnvironmentVariables($identity)
    }
    if (Test-Path -LiteralPath $identity -PathType Leaf) {
        $options += @("-i", $identity)
    }
    return $options
}

function Get-SshFailureStatus {
    param(
        [object[]]$Output,
        [int]$Status
    )
    $text = ($Output -join "`n")
    if ($text -match '(?i)host key verification failed|remote host identification has changed|offending .* key|no .* host key is known') {
        return $Status
    }
    if ($text -match '(?i)permission denied|no supported authentication methods|too many authentication failures|sign_and_send_pubkey.*failed') {
        return 75
    }
    return $Status
}

function Invoke-NativeProbe {
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
        return [pscustomobject]@{ Output = @(); Status = 127 }
    }
    $output = & $sshExe @(Get-NativeSshOptions) "-o" "BatchMode=yes" $dgxTarget "printf 'SSH_OK\n'; hostname; id -un" 2>&1
    $status = $LASTEXITCODE
    $classifiedStatus = [int](Get-SshFailureStatus -Output $output -Status $status)
    return [pscustomobject]@{
        Output = @($output)
        Status = $classifiedStatus
    }
}

function Invoke-NativeExec {
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
        return 127
    }
    if ($RemoteCommand.Count -eq 0) {
        Write-Error "USAGE: dgx_ssh.ps1 exec <remote-command>"
        return 64
    }
    & $sshExe @(Get-NativeSshOptions) "-o" "BatchMode=yes" $dgxTarget @RemoteCommand
    return $LASTEXITCODE
}

function ConvertTo-StartProcessArgumentString {
    param([string[]]$Arguments)
    return (($Arguments | ForEach-Object {
        $argument = [string]$_
        if ($argument -match '[\s"]') {
            '"' + $argument.Replace('"', '\"') + '"'
        } else {
            $argument
        }
    }) -join " ")
}

function Invoke-NativeAuth {
    if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
        return [pscustomobject]@{ Output = @(); Status = 127 }
    }
    $options = @(Get-NativeSshOptions) + @("-o", "BatchMode=no")
    $argumentList = @($options) + @($dgxTarget)
    if ($RemoteCommand.Count -gt 0) {
        $argumentList += @($RemoteCommand)
    }
    $argumentString = ConvertTo-StartProcessArgumentString -Arguments $argumentList
    $process = Start-Process -FilePath $sshExe -ArgumentList $argumentString -Wait -NoNewWindow -PassThru
    $status = [int]$process.ExitCode
    $output = @()
    if ($status -eq 0) {
        $output += "AUTH_OK: native Windows SSH route"
    }
    return [pscustomobject]@{
        Output = @($output)
        Status = $status
    }
}

switch ($Mode) {
    "probe" {
        if (-not [string]::IsNullOrWhiteSpace($wslScriptPath)) {
            $wslOutput = & wsl.exe -d Ubuntu -- bash $wslScriptPath probe 2>&1
            $wslStatus = $LASTEXITCODE
            if ($wslStatus -eq 0) {
                $wslOutput
                exit 0
            }
            if ($wslStatus -ne 75) {
                $wslOutput
                exit $wslStatus
            }
        }
        $nativeProbe = Invoke-NativeProbe
        $nativeProbe.Output
        if ($nativeProbe.Status -eq 0) {
            exit 0
        }
        if ($nativeProbe.Status -ne 75) {
            exit $nativeProbe.Status
        }
        Write-Error "REAUTH_REQUIRED: both WSL and native Windows SSH routes failed"
        exit 75
    }
    "exec" {
        if (-not [string]::IsNullOrWhiteSpace($wslScriptPath)) {
            $wslOutput = & wsl.exe -d Ubuntu -- env HERMES_DGX_EXEC_PROTOCOL=1 bash $wslScriptPath exec @RemoteCommand 2>&1
            $wslStatus = $LASTEXITCODE
            if ($wslStatus -eq 0) {
                $wslOutput
                exit 0
            }
            $wslText = $wslOutput -join "`n"
            if ($wslStatus -eq 75 -and $wslText -match 'HERMES_DGX_REMOTE_EXIT_STATUS=(\d+)') {
                $wslOutput | Where-Object {
                    $_.ToString() -notmatch '^HERMES_DGX_REMOTE_EXIT_STATUS=\d+$'
                }
                exit ([int]$matches[1])
            }
            if ($wslStatus -ne 75) {
                $wslOutput
                exit $wslStatus
            }
        }
        $nativeProbe = Invoke-NativeProbe
        if ($nativeProbe.Status -ne 0) {
            if ($nativeProbe.Status -ne 75) {
                exit $nativeProbe.Status
            }
            Write-Error "REAUTH_REQUIRED: both WSL and native Windows SSH routes failed"
            exit 75
        }
        $nativeStatus = Invoke-NativeExec
        exit $nativeStatus
    }
    "auth" {
        if (-not [string]::IsNullOrWhiteSpace($wslScriptPath)) {
            & wsl.exe -d Ubuntu -- bash $wslScriptPath auth @RemoteCommand
            $wslStatus = $LASTEXITCODE
            if ($wslStatus -eq 0) {
                exit 0
            }
            if ($wslStatus -ne 75) {
                exit $wslStatus
            }
        }
        $nativeAuth = Invoke-NativeAuth
        $nativeAuth.Output
        if ($nativeAuth.Status -eq 0) {
            exit 0
        }
        Write-Error "REAUTH_REQUIRED: interactive auth failed on both WSL and native Windows SSH"
        exit 75
    }
    "bootstrap" {
        # Key bootstrap remains canonical in WSL; native Windows auth is still
        # available through `auth` and `exec` fallback without duplicating the
        # public-key installation policy here.
        if ([string]::IsNullOrWhiteSpace($wslScriptPath)) {
            Write-Error "BLOCKED_WSL_UNAVAILABLE: bootstrap is restricted to the canonical WSL path."
            exit 69
        }
        & wsl.exe -d Ubuntu -- bash $wslScriptPath bootstrap @RemoteCommand
        exit $LASTEXITCODE
    }
}

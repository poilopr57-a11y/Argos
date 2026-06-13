# ------------------------------------------------------------
# ARGOS durable launcher for Windows Task Scheduler
# ------------------------------------------------------------
# Keep main.py in the foreground. This makes the scheduled task stay
# Running while Telegram/MCP are alive, instead of spawning a short
# npm process that exits immediately.

$ErrorActionPreference = "Continue"

$root = "F:\debug\argoss"
Set-Location -LiteralPath $root

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$outLog = Join-Path $logDir "argos_task_$stamp.out.log"
$errLog = Join-Path $logDir "argos_task_$stamp.err.log"

# ФИКС: приоритет .venv — там рабочие torch/пакеты. Системный python311 имеет
# СЛОМАННЫЕ torch+onnxruntime (zombie pip install) → main.py падал code -1 в цикле.
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "C:\Users\AvA\AppData\Local\Programs\Python\Python311\python.exe"
}
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python.exe"
}

$pwsh = "C:\Program Files\PowerShell\7\pwsh.exe"
if (-not (Test-Path -LiteralPath $pwsh)) {
    $pwsh = "powershell.exe"
}

function Write-LauncherLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $line | Out-File -FilePath $outLog -Append -Encoding utf8
}

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    try {
        Get-Content -LiteralPath $Path -Encoding utf8 | ForEach-Object {
            $line = [string]$_
            if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
                return
            }
            $idx = $line.IndexOf("=")
            if ($idx -le 0) {
                return
            }
            $name = $line.Substring(0, $idx).Trim()
            $value = $line.Substring($idx + 1).Trim()
            if ($value.Length -ge 2) {
                $first = $value.Substring(0, 1)
                $last = $value.Substring($value.Length - 1, 1)
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            if ($name -match '^[A-Za-z_][A-Za-z0-9_]*$') {
                [Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
        Write-LauncherLog "Loaded runtime env from .env"
    } catch {
        Write-LauncherLog "Failed to load .env: $($_.Exception.Message)"
    }
}

function Get-EnvInt {
    param(
        [string]$Name,
        [int]$Default
    )
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = [Environment]::GetEnvironmentVariable($Name, "User")
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = [Environment]::GetEnvironmentVariable($Name, "Machine")
    }
    $parsed = 0
    if ([int]::TryParse([string]$value, [ref]$parsed)) {
        return $parsed
    }
    return $Default
}

function Stop-StaleArgosProcesses {
    try {
        Write-LauncherLog "Stopping stale ARGOS python processes"
        $targets = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq "python.exe" -and (
                $_.CommandLine -match "main.py" -or
                $_.CommandLine -match "web_server.py" -or
                $_.CommandLine -match "argos_brain_api.py" -or
                $_.CommandLine -match "telegram_bot.py"
            )
        }
        foreach ($target in $targets) {
            if ($target.ProcessId -ne $PID) {
                Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
                Write-LauncherLog "Stopped stale PID $($target.ProcessId): $($target.CommandLine)"
            }
        }
    } catch {
        Write-LauncherLog "Stale process cleanup failed: $($_.Exception.Message)"
    }
}

function Stop-StaleArgosPortListeners {
    $ports = @(
        (Get-EnvInt "ARGOS_MCP_PORT" 8000),
        (Get-EnvInt "BRAIN_API_PORT" 5001),
        5001,
        (Get-EnvInt "ARGOS_WEB_PORT" 8080),
        (Get-EnvInt "ARGOS_DASHBOARD_PORT" 8090),
        47291
    ) | Sort-Object -Unique

    foreach ($port in $ports) {
        try {
            $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
            foreach ($listener in $listeners) {
                $owner = $listener.OwningProcess
                if (-not $owner -or $owner -eq $PID) {
                    continue
                }
                $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
                if ($null -eq $proc) {
                    continue
                }
                if ($proc.Name -match "python|pythonw") {
                    Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
                    Write-LauncherLog "Stopped stale listener PID $owner on port ${port}: $($proc.CommandLine)"
                }
            }
        } catch {
            Write-LauncherLog "Port cleanup failed for ${port}: $($_.Exception.Message)"
        }
    }
}

function Test-TcpPort {
    param([int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(500, $false)
        if ($ok) {
            $client.EndConnect($iar)
        }
        $client.Close()
        return [bool]$ok
    } catch {
        return $false
    }
}

function Get-AliveGpuPortCount {
    $gpuPorts = @(8082, 8083, 8084, 8085)
    $count = 0
    foreach ($port in $gpuPorts) {
        if (Test-TcpPort $port) {
            $count += 1
        }
    }
    return $count
}

Write-LauncherLog "ARGOS task launcher start"
Write-LauncherLog "Root: $root"

# Runtime defaults for the stable Telegram path.
$env:PYTHONUTF8 = "1"
Import-DotEnv (Join-Path $root ".env")

Stop-StaleArgosProcesses
Stop-StaleArgosPortListeners
Start-Sleep -Seconds 2

try {
    $v100Script = Join-Path $root "scripts\start_v100_nemo.ps1"
    if (Test-Path -LiteralPath $v100Script) {
        if ($env:ARGOS_FORCE_GPU_RESTART -eq "1" -or -not (Test-TcpPort 8085)) {
            Write-LauncherLog "Starting V100 Mistral Nemo via scripts\start_v100_nemo.ps1"
            & $pwsh -NoProfile -ExecutionPolicy Bypass -File $v100Script >> $outLog 2>> $errLog
            Write-LauncherLog "V100 launcher finished with code $LASTEXITCODE"
        } else {
            Write-LauncherLog "V100 launcher skipped: port 8085 already alive"
        }
    } else {
        Write-LauncherLog "V100 launcher not found: $v100Script"
    }

    $rx580Script = Join-Path $root "scripts\start_rx580_argos.ps1"
    if (Test-Path -LiteralPath $rx580Script) {
        if ($env:ARGOS_FORCE_GPU_RESTART -eq "1" -or -not (Test-TcpPort 8082)) {
            Write-LauncherLog "Starting RX580 ArgosV1 via scripts\start_rx580_argos.ps1"
            & $pwsh -NoProfile -ExecutionPolicy Bypass -File $rx580Script >> $outLog 2>> $errLog
            Write-LauncherLog "RX580 launcher finished with code $LASTEXITCODE"
        } else {
            Write-LauncherLog "RX580 launcher skipped: port 8082 already alive"
        }
    } else {
        Write-LauncherLog "RX580 launcher not found: $rx580Script"
    }

    $gpuScript = Join-Path $root "scripts\three_gpu_start.ps1"
    $aliveGpuPorts = Get-AliveGpuPortCount
    if ($env:ARGOS_USE_LEGACY_THREE_GPU -ne "1" -and (Test-Path -LiteralPath $v100Script)) {
        Write-LauncherLog "Legacy three_gpu_start skipped: dedicated V100/RX580 launchers manage active GPU_SERVER ports"
    } elseif ($env:ARGOS_FORCE_GPU_RESTART -ne "1" -and $aliveGpuPorts -gt 0) {
        Write-LauncherLog "GPU launcher skipped: $aliveGpuPorts GPU port(s) already alive"
    } elseif (Test-Path -LiteralPath $gpuScript) {
        Write-LauncherLog "Starting GPU servers via scripts\three_gpu_start.ps1"
        & $pwsh -NoProfile -ExecutionPolicy Bypass -File $gpuScript >> $outLog 2>> $errLog
        Write-LauncherLog "GPU launcher finished with code $LASTEXITCODE"
    } else {
        Write-LauncherLog "GPU launcher not found: $gpuScript"
    }
} catch {
    Write-LauncherLog "GPU launcher failed: $($_.Exception.Message)"
}

$restartDelaySec = Get-EnvInt "ARGOS_LAUNCHER_RESTART_DELAY_SEC" 10
$maxRestarts = Get-EnvInt "ARGOS_LAUNCHER_MAX_RESTARTS" 0
$restartCount = 0

while ($true) {
    Write-LauncherLog "Starting main.py in foreground (includes Telegram + MCP)"
    & $python (Join-Path $root "main.py") >> $outLog 2>> $errLog
    $exitCode = $LASTEXITCODE

    Write-LauncherLog "ARGOS exited with code $exitCode"
    if ($env:ARGOS_LAUNCHER_ONESHOT -eq "1") {
        exit $exitCode
    }

    $restartCount += 1
    if ($maxRestarts -gt 0 -and $restartCount -ge $maxRestarts) {
        Write-LauncherLog "Restart limit reached ($restartCount/$maxRestarts), launcher exits"
        exit $exitCode
    }

    Write-LauncherLog "Restarting ARGOS after ${restartDelaySec}s (restart #$restartCount)"
    Start-Sleep -Seconds $restartDelaySec
    Stop-StaleArgosProcesses
    Stop-StaleArgosPortListeners
    Start-Sleep -Seconds 2
}

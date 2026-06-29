param(
    [int]$Port = 8082,
    [int]$CheckIntervalSeconds = 30
)

$ErrorActionPreference = "Stop"
$server = "C:\Users\AvA\.docker\bin\inference\llama-server.exe"
$model = "D:\HFModels\AvaSiG__argos-v1-gguf\qwen2.5-1.5b-instruct.Q4_K_M.gguf"
$logDir = Join-Path $PSScriptRoot "..\logs"
$logPath = Join-Path $logDir "rx580_llama_watchdog.log"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-WatchdogLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-LlamaServer {
    try {
        $response = Invoke-WebRequest `
            -Uri "http://127.0.0.1:$Port/v1/models" `
            -UseBasicParsing `
            -TimeoutSec 5
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Start-LlamaServer {
    if (-not (Test-Path -LiteralPath $server)) {
        Write-WatchdogLog "ERROR executable not found: $server"
        return
    }
    if (-not (Test-Path -LiteralPath $model)) {
        Write-WatchdogLog "ERROR model not found: $model"
        return
    }

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Write-WatchdogLog "Port $Port is occupied but health check failed; waiting"
        return
    }

    $arguments = @(
        "-m", $model,
        "--port", "$Port",
        "--host", "0.0.0.0",
        "-ngl", "99",
        "-c", "4096",
        "--jinja",
        "--embeddings"
    )
    $process = Start-Process `
        -FilePath $server `
        -ArgumentList $arguments `
        -WorkingDirectory (Split-Path -Parent $server) `
        -WindowStyle Hidden `
        -PassThru
    Write-WatchdogLog "Started llama-server PID=$($process.Id) port=$Port"
}

Write-WatchdogLog "Watchdog started for port $Port"
while ($true) {
    if (-not (Test-LlamaServer)) {
        Start-LlamaServer
    }
    Start-Sleep -Seconds $CheckIntervalSeconds
}

# ARGOS -- autostart local GPU models (llama-server) + ESP voice server
# V100 mistral-nemo (CUDA, :8085) + RX580 argos-v1 (Vulkan, :8082) + xiaozhi-server (ESP voice, :8006)
#
# IMPORTANT: uses Invoke-CimMethod Win32_Process.Create instead of Start-Process,
# so launched processes survive the launching session ending (SSH disconnect / logoff).
# This was the root cause of GPU+ESP servers not staying up after startup.

$ErrorActionPreference = "SilentlyContinue"

# Clean old instances
Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'xiaozhi-server\\\\server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

# 1) V100 -- mistral-nemo 12B via CUDA b9412, port 8085
$cmd85 = '"F:\models\llama-b9412-bin-win-cuda-12.4-x64\llama-server.exe" -m "F:\models\mistral-nemo-instruct-2407.Q4_K_M.gguf" --port 8085 --host 0.0.0.0 -ngl 99 -c 4096 --jinja --log-disable'
$r85 = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd85; CurrentDirectory = "F:\models\llama-b9412-bin-win-cuda-12.4-x64" }
Write-Host "V100 (:8085) PID: $($r85.ProcessId)"

# 2) RX580 -- argos-v1 (qwen2.5-1.5b fine-tuned) via Vulkan, port 8082
$cmd82 = '"C:\Users\AvA\.docker\bin\inference\llama-server.exe" -m "D:\HFModels\AvaSiG__argos-v1-gguf\qwen2.5-1.5b-instruct.Q4_K_M.gguf" --port 8082 --host 0.0.0.0 --device Vulkan0 -ngl 99 -c 4096 --jinja'
$r82 = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd82; CurrentDirectory = "C:\Users\AvA\.docker\bin\inference" }
Write-Host "RX580 (:8082) PID: $($r82.ProcessId)"

Start-Sleep 25

# 3) Zigbee2MQTT on PC (CC2531 COM14)
$z2mDir = "C:\Users\AvA\zigbee2mqtt"
if (Test-Path "$z2mDir\index.js") {
    $nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
    if ($nodeExe) {
        $cmdz2m = "`"$nodeExe`" index.js"
        $rz2m = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdz2m; CurrentDirectory = $z2mDir }
        Write-Host "Zigbee2MQTT (:8100) PID: $($rz2m.ProcessId)"
    }
}

# 4) xiaozhi-server (ESP voice assistant) -- OTA :8006, depends on V100 :8085
# FIX: venv lives at F:\debug\argoss\.venv, not inside xiaozhi-server\.venv
$xzDir = "F:\debug\argoss\xiaozhi-server"
$venvPython = "F:\debug\argoss\.venv\Scripts\python.exe"
if (Test-Path "$xzDir\server.py") {
    $cmdxz = "`"$venvPython`" server.py"
    $rxz = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdxz; CurrentDirectory = $xzDir }
    Write-Host "xiaozhi-server (:8006) PID: $($rxz.ProcessId)"
}

Start-Sleep 15

# Health check
$v100 = (curl -s http://localhost:8085/health) -match "ok"
$rx580 = (curl -s http://localhost:8082/health) -match "ok"
$xz = (curl -s -o /dev/null -w "%{http_code}" http://localhost:8006/) -match "200"
Write-Host "V100 mistral (:8085): $(if($v100){'OK'}else{'FAIL'})"
Write-Host "RX580 argos-v1 (:8082): $(if($rx580){'OK'}else{'FAIL'})"
Write-Host "xiaozhi voice (:8006): $(if($xz){'OK'}else{'FAIL'})"

#requires -Version 7
<#
.SYNOPSIS
    Deploy Argos VPN server to GCP VM with Cloudflare tunnel.

.EXAMPLE
    pwsh deploy/gcp/vpn/deploy.ps1
#>
param(
    [string]$VmName = "argos-vpn-eu",
    [string]$Zone = "europe-west4-a",
    [string]$MachineType = "e2-small",
    [string]$Domain = "vpn.argosssss.win",
    [string]$RepoBranch = "main"
)

$ErrorActionPreference = "Stop"

function Load-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)\s*=\s*(.*)\s*$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim().Trim("'").Trim('"')
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

Load-DotEnv ".env"

$token = $env:CLOUDFLARE_API_TOKEN
$accountId = $env:CLOUDFLARE_ACCOUNT_ID
$botToken = $env:ARGOS_VPN_BOT_TOKEN

if (-not $token) { throw "CLOUDFLARE_API_TOKEN not set" }
if (-not $accountId) { throw "CLOUDFLARE_ACCOUNT_ID not set" }
if (-not $botToken) { throw "ARGOS_VPN_BOT_TOKEN not set" }

Write-Host "Creating VM $VmName in $Zone..."
gcloud compute instances create $VmName `
    --zone=$Zone `
    --machine-type=$MachineType `
    --image-family=debian-12 `
    --image-project=debian-cloud `
    --boot-disk-size=20GB `
    --boot-disk-type=pd-balanced `
    --tags=vpn-server `
    --metadata=enable-oslogin=TRUE

Write-Host "Creating firewall rule for WireGuard UDP 51820..."
gcloud compute firewall-rules create allow-vpn-wireguard `
    --direction=INGRESS `
    --network=default `
    --action=ALLOW `
    --rules=udp:51820 `
    --source-ranges=0.0.0.0/0 `
    --target-tags=vpn-server `
    --quiet 2>$null

Write-Host "Waiting for external IP..."
$externalIp = ""
for ($i = 0; $i -lt 30; $i++) {
    $externalIp = (gcloud compute instances describe $VmName --zone=$Zone --format="get(networkInterfaces[0].accessConfigs[0].natIP)")
    if ($externalIp) { break }
    Start-Sleep -Seconds 5
}
if (-not $externalIp) { throw "No external IP assigned" }
Write-Host "VM external IP: $externalIp"

Write-Host "Waiting for VM SSH..."
gcloud compute ssh $VmName --zone=$Zone --command="echo ready" --quiet 2>$null

Write-Host "Creating Cloudflare tunnel for $Domain..."
$headers = @{ "Authorization" = "Bearer $token"; "Content-Type" = "application/json" }
$body = @{ name = $VmName; config_src = "cloudflare" } | ConvertTo-Json -Depth 3
$tunnelResp = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/accounts/$accountId/tunnels" -Method Post -Headers $headers -Body $body
if (-not $tunnelResp.success) { throw "Tunnel creation failed: $($tunnelResp.errors)" }
$tunnelId = $tunnelResp.result.id
$tunnelToken = $tunnelResp.result.token
Write-Host "Tunnel ID: $tunnelId"

Write-Host "Finding Cloudflare zone for $Domain..."
$zones = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones" -Headers $headers
$zone = $zones.result | Where-Object { $Domain.EndsWith($_.name) } | Select-Object -First 1
if (-not $zone) { throw "Zone not found for $Domain" }
$zoneId = $zone.id
$recordName = $Domain -replace "\.\$([regex]::Escape($zone.name))\$", ""

Write-Host "Creating DNS record $Domain -> $tunnelId.cfargotunnel.com ..."
$dnsBody = @{
    type = "CNAME"
    name = $recordName
    content = "$tunnelId.cfargotunnel.com"
    ttl = 1
    proxied = $true
} | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones/$zoneId/dns_records" -Method Post -Headers $headers -Body $dnsBody | Out-Null

Write-Host "Writing setup script..."
$setupScript = Get-Content -Raw "deploy/gcp/vpn/startup.sh"
$setupScript = $setupScript -replace '\$\{ARGOS_VPN_BOT_TOKEN\}', $botToken
$setupScript = $setupScript -replace '\$\{ARGOS_VPN_SERVER_IP\}', $externalIp
$setupScript = $setupScript -replace '\$\{ARGOS_VPN_WEBAPP_URL\}', "https://$Domain/vpn/webapp"
$setupScript = $setupScript -replace '\$\{CLOUDFLARE_TUNNEL_ID\}', $tunnelId
$setupScript = $setupScript -replace '\$\{CLOUDFLARE_TUNNEL_HOSTNAME\}', $Domain

$tmpPath = "$env:TEMP\argos-vpn-setup.sh"
$setupScript | Set-Content -Path $tmpPath -Encoding UTF8 -NoNewline

Write-Host "Copying setup script to VM..."
gcloud compute scp "$tmpPath" "${VmName}:/tmp/argos-vpn-setup.sh" --zone=$Zone --quiet

Write-Host "Running setup on VM..."
gcloud compute ssh $VmName --zone=$Zone --command="chmod +x /tmp/argos-vpn-setup.sh && sudo bash /tmp/argos-vpn-setup.sh" --quiet

Write-Host "Updating local .env..."
(Get-Content .env) `
    -replace 'ARGOS_VPN_SERVER_IP=.*', "ARGOS_VPN_SERVER_IP=$externalIp" `
    -replace 'ARGOS_VPN_WEBAPP_URL=.*', "ARGOS_VPN_WEBAPP_URL=https://$Domain/vpn/webapp" |
    Set-Content .env

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "WebApp: https://$Domain/vpn/webapp"
Write-Host "Server IP: $externalIp:51820"
Write-Host "Next: update Telegram Menu Button to https://$Domain/vpn/webapp"

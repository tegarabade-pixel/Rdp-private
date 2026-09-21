$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PSNativeCommandUseErrorActionPreference = $true

function Split-IceUrls([string]$raw) {
  if ([string]::IsNullOrWhiteSpace($raw)) { return @() }
  return @($raw -split '[,;\s]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$iceServers = @()
$provider = 'none'

# 1) Exact RTCIceServer JSON. Good for any TURN provider.
$rawCustom = ($env:RDP_ICE_SERVERS_JSON_SECRET | Out-String).Trim()
if ($rawCustom) {
  try {
    $parsed = @($rawCustom | ConvertFrom-Json)
    if ($parsed.Count -lt 1) { throw 'empty array' }
    $iceServers = $parsed
    $provider = 'custom-json'
  } catch { throw "RDP_ICE_SERVERS_JSON invalid: $($_.Exception.Message)" }
}

# 2) Cloudflare Realtime TURN: exchange long-lived GitHub secrets for temporary credentials.
if ($iceServers.Count -eq 0) {
  $keyId = ($env:RDP_CF_TURN_KEY_ID_SECRET | Out-String).Trim()
  $token = ($env:RDP_CF_TURN_API_TOKEN_SECRET | Out-String).Trim()
  if ($keyId -or $token) {
    if (-not $keyId -or -not $token) { throw 'Cloudflare TURN requires BOTH RDP_CF_TURN_KEY_ID and RDP_CF_TURN_API_TOKEN' }
    $keep=120; $tmp=0
    if ([int]::TryParse($env:RDP_KEEP_MINUTES,[ref]$tmp)) { $keep=[Math]::Max(1,$tmp) }
    $ttl=[Math]::Min(172800,[Math]::Max(3600,($keep*60)+1800))
    $endpoint="https://rtc.live.cloudflare.com/v1/turn/keys/$keyId/credentials/generate-ice-servers"
    try {
      $resp=Invoke-RestMethod -Method Post -Uri $endpoint -Headers @{Authorization="Bearer $token";'Content-Type'='application/json'} -Body (@{ttl=$ttl}|ConvertTo-Json -Compress) -TimeoutSec 30
      $normalized=@()
      foreach($srv in @($resp.iceServers)) {
        $urls=@($srv.urls | ForEach-Object {[string]$_} | Where-Object {$_ -and $_ -notmatch ':(53)(\?|$)'})
        if($urls.Count -eq 0){continue}
        $item=[ordered]@{urls=$urls}
        if($srv.username){$item.username=[string]$srv.username}
        if($srv.credential){$item.credential=[string]$srv.credential}
        $normalized += $item
      }
      if($normalized.Count -lt 1){throw 'Cloudflare returned no usable ICE servers'}
      $iceServers=$normalized; $provider='cloudflare-realtime'
    } finally { $token=$null; $env:RDP_CF_TURN_API_TOKEN_SECRET='' }
  }
}

# 3) Metered credential API.
if ($iceServers.Count -eq 0) {
  $app=($env:RDP_METERED_APP_SECRET|Out-String).Trim()
  $api=($env:RDP_METERED_API_KEY_SECRET|Out-String).Trim()
  $region=($env:RDP_METERED_REGION_SECRET|Out-String).Trim()
  if($app -or $api) {
    if(-not $app -or -not $api){throw 'Metered requires BOTH RDP_METERED_APP and RDP_METERED_API_KEY'}
    $app=$app -replace '^https?://',''; $app=$app -replace '\.metered\.live/?$',''
    $endpoint="https://$app.metered.live/api/v1/turn/credentials?apiKey=$([uri]::EscapeDataString($api))"
    if($region){$endpoint += "&region=$([uri]::EscapeDataString($region))"}
    try {
      $raw=Invoke-RestMethod -Method Get -Uri $endpoint -TimeoutSec 30
      $normalized=@()
      foreach($srv in @($raw)) {
        $urls=@($srv.urls | ForEach-Object {[string]$_} | Where-Object {$_})
        if($urls.Count -eq 0 -and $srv.url){$urls=@([string]$srv.url)}
        if($urls.Count -eq 0){continue}
        $item=[ordered]@{urls=$urls}
        if($srv.username){$item.username=[string]$srv.username}
        if($srv.credential){$item.credential=[string]$srv.credential}
        $normalized += $item
      }
      if($normalized.Count -lt 1){throw 'Metered returned no ICE servers'}
      $iceServers=$normalized; $provider='metered'
    } finally { $api=$null; $env:RDP_METERED_API_KEY_SECRET='' }
  }
}

# 4) Static TURN credentials from any provider/coturn.
if ($iceServers.Count -eq 0) {
  $urls=Split-IceUrls (($env:RDP_TURN_URLS_SECRET|Out-String).Trim())
  $user=($env:RDP_TURN_USERNAME_SECRET|Out-String).Trim()
  $pass=($env:RDP_TURN_CREDENTIAL_SECRET|Out-String).Trim()
  if($urls.Count -gt 0 -or $user -or $pass) {
    if($urls.Count -lt 1 -or -not $user -or -not $pass){throw 'Static TURN requires RDP_TURN_URLS, RDP_TURN_USERNAME and RDP_TURN_CREDENTIAL'}
    $iceServers=@([ordered]@{urls=$urls;username=$user;credential=$pass}); $provider='static-turn'
  }
}

if($iceServers.Count -eq 0){
  throw @'
No TURN provider configured. PrivateRDP v0.2.0 is RELAY-ONLY by design.
Configure ONE option in GitHub Repository Secrets:
  RDP_ICE_SERVERS_JSON
or Cloudflare: RDP_CF_TURN_KEY_ID + RDP_CF_TURN_API_TOKEN
or Metered: RDP_METERED_APP + RDP_METERED_API_KEY (+ optional RDP_METERED_REGION)
or static: RDP_TURN_URLS + RDP_TURN_USERNAME + RDP_TURN_CREDENTIAL
'@
}

# Reject STUN-only configs before spending time starting the desktop.
$turnCount=0
foreach($srv in @($iceServers)){
  foreach($u in @($srv.urls)){
    if(([string]$u).ToLowerInvariant().StartsWith('turn:') -or ([string]$u).ToLowerInvariant().StartsWith('turns:')){$turnCount++}
  }
}
if($turnCount -lt 1){throw 'Configured ICE data contains no TURN URL; relay-only cannot start'}

$input=Join-Path $env:RUNNER_TEMP 'privaterdp-turn-input.json'
$output=Join-Path $env:RUNNER_TEMP 'privaterdp-turn-result.json'
ConvertTo-Json -InputObject @($iceServers) -Compress -Depth 8 | Set-Content -Path $input -Encoding UTF8
$env:RDP_TURN_PROBE_INPUT=$input
$env:RDP_TURN_PROBE_OUTPUT=$output
python scripts/verify_turn.py
if($LASTEXITCODE -ne 0 -or -not (Test-Path $output)){
  if(Test-Path $output){Get-Content $output -Raw | Write-Host}
  throw 'TURN probe failed: Runner could not gather a relay candidate'
}
$result=Get-Content $output -Raw | ConvertFrom-Json
if($result.ready -ne $true -or @($result.verifiedIceServers).Count -lt 1){throw 'TURN probe produced no verified relay endpoint'}
$verified=ConvertTo-Json -InputObject @($result.verifiedIceServers) -Compress -Depth 8
$delim='PRDP_ICE_'+[guid]::NewGuid().ToString('N')
"RDP_ICE_SERVERS_JSON<<$delim" | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
$verified | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
$delim | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
"RDP_ICE_PROVIDER=$provider" | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
"RDP_ICE_MODE=relay-only-verified" | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
"RDP_TURN_RELAY_READY=1" | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
"RDP_TURN_PROBE_RESULT=relay-ok" | Out-File $env:GITHUB_ENV -Encoding utf8NoBOM -Append
Write-Host "TURN VERIFIED provider=$provider endpoints=$(@($result.verifiedIceServers).Count) mode=RELAY_ONLY"

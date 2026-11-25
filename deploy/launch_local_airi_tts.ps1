<#
.SYNOPSIS
    Cross-machine Windows launcher template for Shinobu Daemon + Optional Dual-Mode TTS Gateway + Project AIRI.

.DESCRIPTION
    Resolves `shinobu` dynamically via PATH (or SHINOBU_BIN) and launches:
      1. Shinobu daemon on 127.0.0.1:$Port
      2. Optional Dual-Mode TTS gateway on 127.0.0.1:$TtsPort
      3. Companion Studio UI & Project AIRI desktop shell (if installed)

    Environment overrides (all optional):
      SHINOBU_BIN              Explicit path to shinobu executable
      SHINOBU_PORT             Daemon port (default: 18790)
      SHINOBU_TTS_PORT         Dual-Mode TTS gateway port (default: 18791)
      SHINOBU_AIRI_EXE         Path to airi.exe (default: $env:LOCALAPPDATA\Programs\airi\airi.exe)
      SHINOBU_AIRI_BUBBLE_URL  Optional bubble webhook URL passed via --airi-bubble-url
      SHINOBU_SKIP_TTS         Set to "1" to skip launching the TTS gateway
      SHINOBU_SKIP_UI          Set to "1" to skip opening Companion Studio in browser
      SHINOBU_SKIP_AIRI        Set to "1" to skip launching Project AIRI
#>

param(
    [int]$Port = $(if ($env:SHINOBU_PORT) { [int]$env:SHINOBU_PORT } else { 18790 }),
    [int]$TtsPort = $(if ($env:SHINOBU_TTS_PORT) { [int]$env:SHINOBU_TTS_PORT } else { 18791 }),
    [string]$AiriExe = $(if ($env:SHINOBU_AIRI_EXE) { $env:SHINOBU_AIRI_EXE } else { Join-Path $env:LOCALAPPDATA "Programs\airi\airi.exe" }),
    [string]$AiriBubbleUrl = $(if ($env:SHINOBU_AIRI_BUBBLE_URL) { $env:SHINOBU_AIRI_BUBBLE_URL } else { "" }),
    [switch]$SkipTts,
    [switch]$SkipUi,
    [switch]$SkipAiri
)

$ErrorActionPreference = "SilentlyContinue"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"

if ($env:SHINOBU_SKIP_TTS -eq "1") { $SkipTts = $true }
if ($env:SHINOBU_SKIP_UI -eq "1") { $SkipUi = $true }
if ($env:SHINOBU_SKIP_AIRI -eq "1") { $SkipAiri = $true }

$shinobuExe = $null
if ($env:SHINOBU_BIN -and (Test-Path $env:SHINOBU_BIN)) {
    $shinobuExe = $env:SHINOBU_BIN
} else {
    $cmdInfo = Get-Command shinobu -ErrorAction SilentlyContinue
    if ($cmdInfo) {
        $shinobuExe = $cmdInfo.Source
    }
}

if (-not $shinobuExe) {
    Write-Host "[Shinobu + AIRI] Could not locate 'shinobu' on PATH." -ForegroundColor Red
    Write-Host "  Please activate your Python environment or install Shinobu (e.g. 'pip install -e .')," -ForegroundColor Yellow
    Write-Host "  or set SHINOBU_BIN to the full path of your shinobu executable." -ForegroundColor Yellow
    exit 1
}

function Test-ShinobuReady {
    try {
        $raw = & $shinobuExe ui --no-open --json --port $Port 2>$null
        if ($raw) {
            $info = $raw | ConvertFrom-Json
            if ($info.status -eq "ready") {
                return $true
            }
        }
    } catch {}
    return $false
}

function Get-TtsHealth {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$TtsPort/health" -TimeoutSec 2
    } catch {
        return $null
    }
}

Write-Host "[Shinobu + AIRI] 1/3 Checking Shinobu daemon on 127.0.0.1:$Port..."
$ready = Test-ShinobuReady
if (-not $ready) {
    Write-Host "[Shinobu + AIRI] Daemon not running. Launching Shinobu daemon window..."
    $serveArgs = @("serve", "--port", "$Port")
    if ($AiriBubbleUrl) {
        $serveArgs += @("--airi-bubble-url", $AiriBubbleUrl)
    }
    $quotedServeArgs = ($serveArgs | ForEach-Object { if ($_ -match "\s") { "`"$_`"" } else { $_ } }) -join " "
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "`"`"$shinobuExe`" $quotedServeArgs`""
    Write-Host "[Shinobu + AIRI] Waiting for daemon readiness (mounting MCP servers may take a few seconds)..."
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-ShinobuReady) {
            $ready = $true
            break
        }
    }
}

$ttsHealth = $null
if (-not $SkipTts) {
    Write-Host "[Shinobu + AIRI] 2/3 Checking Dual-Mode TTS gateway on 127.0.0.1:$TtsPort..."
    $ttsHealth = Get-TtsHealth
    $ttsNeedsLaunch = ($null -eq $ttsHealth) -or ($null -eq $ttsHealth.PSObject.Properties["upstream_enabled"])

    if ($ttsNeedsLaunch) {
        if ($null -ne $ttsHealth) {
            Write-Host "[Shinobu + AIRI] Restarting legacy TTS listener to enable Dual-Mode gateway..." -ForegroundColor Yellow
            $conns = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $TtsPort -State Listen
            if ($conns) {
                $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
                foreach ($procId in $pids) {
                    $proc = Get-Process -Id $procId
                    if ($proc -and ($proc.ProcessName -match "python|shinobu")) {
                        Stop-Process -Id $procId -Force
                    }
                }
                Start-Sleep -Milliseconds 500
            }
        }
        Write-Host "[Shinobu + AIRI] Launching Dual-Mode TTS gateway window..."
        Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "`"`"$shinobuExe`" tts-gateway --port $TtsPort`"" -WindowStyle Minimized
        for ($i = 0; $i -lt 25; $i++) {
            Start-Sleep -Milliseconds 300
            $ttsHealth = Get-TtsHealth
            if ($ttsHealth -and ($null -ne $ttsHealth.PSObject.Properties["upstream_enabled"])) {
                break
            }
        }
    }
} else {
    Write-Host "[Shinobu + AIRI] 2/3 Skipping Dual-Mode TTS gateway (SkipTts enabled)."
}

if ($ready) {
    Write-Host ""
    Write-Host "[Shinobu + AIRI] Brain (Daemon) is READY!" -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Cyan
    & $shinobuExe compat
    Write-Host "==========================================================" -ForegroundColor Cyan
} else {
    Write-Host "[Shinobu + AIRI] Timed out waiting for daemon on port $Port." -ForegroundColor Yellow
}

if (-not $SkipTts) {
    if ($ttsHealth -and $ttsHealth.status -eq "ok") {
        $ttsMode = if ($ttsHealth.upstream_enabled) {
            "Colab GPU ($($ttsHealth.upstream_base_url)) + Edge-TTS fallback"
        } else {
            "Local Edge-TTS (hot-switch via: shinobu tts-gateway --set-upstream <URL> --api-key <KEY>)"
        }
        Write-Host "[Shinobu + AIRI] Voice Gateway is READY! Mode: $ttsMode" -ForegroundColor Green
        Write-Host "----------------------------------------------------------" -ForegroundColor Magenta
        Write-Host "  AIRI Speech Provider : OpenAI Compatible (Speech)"
        Write-Host "  Speech Base URL      : http://127.0.0.1:$TtsPort/v1"
        Write-Host "  Speech API Key       : local-edge-tts"
        Write-Host "  Speech Model         : tts-1"
        Write-Host "  Speech Voice         : shinobu (or zh-CN-XiaoyiNeural / ja-JP-NanamiNeural)"
        Write-Host "----------------------------------------------------------" -ForegroundColor Magenta
    } else {
        Write-Host "[Shinobu + AIRI] TTS gateway did not respond on port $TtsPort." -ForegroundColor Yellow
    }
}

if ($ready -and (-not $SkipUi)) {
    Write-Host ""
    Write-Host "[Shinobu + AIRI] Opening Companion Studio..."
    & $shinobuExe ui --port $Port
}

if (-not $SkipAiri) {
    Write-Host "[Shinobu + AIRI] 3/3 Checking Project AIRI..."
    if (Test-Path $AiriExe) {
        $running = Get-Process -Name "airi" -ErrorAction SilentlyContinue
        if ($running) {
            Write-Host "[Shinobu + AIRI] AIRI is already running." -ForegroundColor Green
        } else {
            Write-Host "[Shinobu + AIRI] Launching Project AIRI desktop shell..." -ForegroundColor Green
            Start-Process -FilePath $AiriExe
        }
    } else {
        Write-Host "[Shinobu + AIRI] AIRI executable not found at: $AiriExe" -ForegroundColor Yellow
        Write-Host "  Install Project AIRI or set SHINOBU_AIRI_EXE to its path (optional)." -ForegroundColor Yellow
    }
} else {
    Write-Host "[Shinobu + AIRI] 3/3 Skipping Project AIRI launch (SkipAiri enabled)."
}

Write-Host ""
Write-Host "Press Enter to close this launcher window (Shinobu, TTS Gateway & AIRI will keep running)..."
Read-Host | Out-Null

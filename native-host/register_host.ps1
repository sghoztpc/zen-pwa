$ErrorActionPreference = "Stop"

$hostDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $hostDir "zen_pwa_host.bat"
$pyScript = Join-Path $hostDir "zen_pwa_host.py"
$manifestPath = Join-Path $hostDir "zen_pwa_host.json"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python executable not found on PATH."
}
$pythonExe = $pythonCmd.Source
& $pythonExe -c "import PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Pillow (PIL) is not installed in $pythonExe. Install with: pip install Pillow"
}

$batContent = "@echo off`r`n`"$pythonExe`" `"$pyScript`"`r`n"
[System.IO.File]::WriteAllText($batPath, $batContent, [System.Text.Encoding]::ASCII)

$manifest = @{
    name               = "zen_pwa_host"
    description        = "Zen PWA Native Messaging Host"
    path               = $batPath
    type               = "stdio"
    allowed_extensions = @("zen-pwa-ct2026@lazymoon.dev")
}

$json = $manifest | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($manifestPath, $json, (New-Object System.Text.UTF8Encoding($false)))

$regPath = "HKCU:\Software\Mozilla\NativeMessagingHosts\zen_pwa_host"
New-Item -Path $regPath -Force | Out-Null
Set-ItemProperty -Path $regPath -Name "(Default)" -Value $manifestPath

Write-Output "Registered Zen PWA native host:"
Write-Output "  Python:   $pythonExe"
Write-Output "  Manifest: $manifestPath"
Write-Output "  Registry: $regPath"

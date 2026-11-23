$ErrorActionPreference = "Stop"

$regPath = "HKCU:\Software\Mozilla\NativeMessagingHosts\zen_pwa_host"
if (Test-Path $regPath) {
    Remove-Item -Path $regPath -Recurse -Force
    Write-Output "Removed registry key: $regPath"
} else {
    Write-Output "Registry key not found: $regPath"
}

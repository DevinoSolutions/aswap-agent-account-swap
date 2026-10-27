$ErrorActionPreference = 'Stop'

# Run in the visible startup terminal so Ctrl+C reaches aswap directly.
$aswapExe = Join-Path $env:USERPROFILE '.local\bin\aswap.exe'
if (-not (Test-Path -LiteralPath $aswapExe -PathType Leaf)) {
    throw "aswap is not installed at $aswapExe"
}

$Host.UI.RawUI.WindowTitle = 'aswap auto - Ctrl+C to stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
Set-Location -LiteralPath $env:USERPROFILE
Write-Host 'aswap auto is running in this terminal. Press Ctrl+C to stop it.'
Write-Host 'After stopping, run aswap auto to start it again, or close this window.'
Write-Host ''
& $aswapExe auto

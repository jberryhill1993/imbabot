# Publish an Imbabot update to the OneDrive download (replace-in-place).
#
# Rebuilds Imbabot.exe, re-zips it, and OVERWRITES the existing
# Imbabot-Download.zip on your Desktop/OneDrive -- so the share link you already
# sent people stays the same. Recipients just re-download to get the new version.
#
# Usage (PowerShell, from the project folder):
#   ./publish_update.ps1                  # rebuild + repackage, keep current version
#   ./publish_update.ps1 -NewVersion 0.3.0  # bump the version first, then build
#
# After it finishes: wait for OneDrive to finish syncing the new zip (the file's
# icon turns from the blue sync arrows to a green check) before telling people.

param(
    [string]$NewVersion = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# The Python that builds the lean exe (same one that produced the shipped build).
$py = "C:\Users\berry\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-Not (Test-Path $py)) {
    throw "Python not found at $py. Edit the `$py path at the top of this script."
}

# OneDrive Desktop locations (derived from your profile, not hardcoded to a user).
$desktop = Join-Path $env:USERPROFILE "OneDrive\Desktop"
$stage   = Join-Path $desktop "Imbabot-Download"
$zip     = Join-Path $desktop "Imbabot-Download.zip"
$exe     = Join-Path $root "dist\Imbabot.exe"

# 1. Optional version bump (shows in the app's title bar so people can tell builds apart).
if ($NewVersion -ne "") {
    $initPath = Join-Path $root "imbabot\__init__.py"
    Write-Host "==> Bumping version to $NewVersion in imbabot\__init__.py..."
    $text = Get-Content $initPath -Raw
    $text = $text -replace '__version__\s*=\s*"[^"]*"', ('__version__ = "{0}"' -f $NewVersion)
    Set-Content $initPath $text -Encoding utf8 -NoNewline
}

# Native tools (PyInstaller) log to stderr, which a "Stop" preference would treat as a
# terminating error. Run native exes with the preference relaxed and check exit codes instead.
function Invoke-Native {
    param([Parameter(Mandatory)][scriptblock]$Block, [string]$What)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
}

# 2. Self-test (offline). Aborts the whole script if anything fails.
Write-Host "==> Running self-test..."
Invoke-Native { & $py -m imbabot.cli selftest } "Self-test"

# 3. Build the single-file exe.
Write-Host "==> Building Imbabot.exe..."
Invoke-Native { & $py -m PyInstaller imbabot.spec --noconfirm } "Build"
if (-Not (Test-Path $exe)) { throw "Build did not produce $exe" }

# 4. Repackage: refresh the exe in the staging folder, then overwrite the shared zip.
Write-Host "==> Repackaging the OneDrive download..."
if (-Not (Test-Path $stage)) { New-Item -ItemType Directory -Path $stage | Out-Null }
Copy-Item $exe $stage -Force
if (-Not (Test-Path (Join-Path $stage "READ ME FIRST.txt"))) {
    Write-Warning "READ ME FIRST.txt is missing from $stage -- the zip will not include it."
}
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal

# 5. Report.
$z = Get-Item $zip
$v = (Select-String -Path (Join-Path $root "imbabot\__init__.py") -Pattern '__version__\s*=\s*"([^"]*)"').Matches.Groups[1].Value
Write-Host ""
Write-Host ("Done. Published Imbabot v{0}" -f $v) -ForegroundColor Green
Write-Host ("  {0}  ({1:N1} MB)" -f $z.FullName, ($z.Length/1MB))
Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. Wait for OneDrive to finish syncing the new zip (green check icon)."
Write-Host "  2. The share link is UNCHANGED -- people just re-download from the same link."
Write-Host "  3. They re-accept the one-time 'More info -> Run anyway' prompt (unsigned app)."

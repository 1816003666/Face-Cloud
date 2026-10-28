#Requires -Version 7.0

$ErrorActionPreference = "Stop"
$repo = (& git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $repo) {
    throw "Run this script inside E:\cloud-phone."
}
$repo = (($repo | Select-Object -First 1) -as [string]).Trim()
Set-Location $repo

& git config core.hooksPath .githooks
& git config pull.ff only
& git config fetch.prune true
& git config push.default simple
& git config core.autocrlf false

if ($LASTEXITCODE -ne 0) {
    throw "Failed to configure Git."
}

Write-Host "Installed .githooks and safe Git sync settings in $repo"

foreach ($hook in @(".githooks/pre-commit", ".githooks/pre-push")) {
    & git ls-files --error-unmatch $hook 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        & git update-index --chmod=+x $hook
    }
}

exit 0

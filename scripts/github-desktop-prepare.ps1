#Requires -Version 7.0

[CmdletBinding()]
param(
    [int]$MaxFileMiB = 20,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "git-policy-common.ps1")

$repo = (& git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $repo) {
    throw "Run this script inside E:\cloud-phone."
}
$repo = (($repo | Select-Object -First 1) -as [string]).Trim()
Set-Location $repo

$existingStaged = @(
    & git -c core.quotepath=false diff --cached --name-only
)
if ($existingStaged.Count -gt 0) {
    $preview = ($existingStaged | Select-Object -First 15) -join "`n"
    throw "The index already contains staged changes. Commit or unstage them in GitHub Desktop first:`n$preview"
}

$tracked = @(& git ls-files)
$excluded = @($tracked | Where-Object { Test-GitHubExcludedPath -Path $_ })
$limit = Get-GitHubPolicyLimitBytes -MaxFileMiB $MaxFileMiB
$oversize = [System.Collections.Generic.List[string]]::new()

foreach ($path in $tracked) {
    if (Test-GitHubExcludedPath -Path $path) {
        continue
    }
    $size = [int64]((& git cat-file -s ":$path" 2>$null) -join "").Trim()
    if ($LASTEXITCODE -eq 0 -and $size -gt $limit) {
        $oversize.Add(("{0:N2} MiB  {1}" -f ($size / 1MB), $path))
    }
}

Write-Host "Tracked files       : $($tracked.Count)"
Write-Host "To untrack          : $($excluded.Count)"
Write-Host "Source files to keep: $($tracked.Count - $excluded.Count)"

if ($oversize.Count -gt 0) {
    Write-Host "Oversized source files are not automatic exclusions:"
    $oversize | ForEach-Object { Write-Host "  $_" }
    throw "Resolve oversized source files before preparing the GitHub Desktop commit."
}

if ($DryRun) {
    Write-Host "Dry run: no index changes were made."
    $excluded | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" }
    if ($excluded.Count -gt 30) {
        Write-Host "  ... and $($excluded.Count - 30) more path(s)"
    }
    exit 0
}

for ($offset = 0; $offset -lt $excluded.Count; $offset += 50) {
    $end = [Math]::Min($offset + 49, $excluded.Count - 1)
    $batch = @($excluded[$offset..$end])
    $args = @("rm", "--cached", "-f", "--ignore-unmatch", "--") + $batch
    & git @args
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to remove generated paths from the Git index."
    }
}

$policyFiles = @(
    ".gitignore",
    ".gitattributes",
    "AGENTS.md",
    "GITHUB-SYNC.md",
    "README-本地调试.md",
    "scripts/git-policy-common.ps1",
    "scripts/git-policy.ps1",
    "scripts/create-github-snapshot.ps1",
    "scripts/github-desktop-prepare.ps1",
    "scripts/install-git-hooks.ps1",
    ".githooks/pre-commit",
    ".githooks/pre-push"
)

foreach ($path in $policyFiles) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Policy file is missing: $path"
    }
    & git add -- $path
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stage policy file: $path"
    }
}

& git update-index --chmod=+x .githooks/pre-commit .githooks/pre-push
if ($LASTEXITCODE -ne 0) {
    throw "Failed to mark Git hooks executable."
}

$staged = @(& git -c core.quotepath=false diff --cached --name-status)
$deletions = @($staged | Where-Object { $_ -match '^D\s' })
Write-Host ""
Write-Host "GitHub Desktop is ready for review."
Write-Host "Staged changes: $($staged.Count)"
Write-Host "Index removals: $($deletions.Count) (local files were preserved)"
Write-Host "Suggested commit: chore(git): keep runtime and release assets out of GitHub"
Write-Host "Reload the repository in GitHub Desktop before committing."
exit 0

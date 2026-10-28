#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$SourceRef = "HEAD",
    [string]$Branch = "github-source",
    [int]$MaxFileMiB = 20,
    [switch]$Orphan,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "git-policy-common.ps1")

$repo = (& git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $repo) {
    throw "Not inside a Git repository."
}
$repo = (($repo | Select-Object -First 1) -as [string]).Trim()
Set-Location $repo

& git check-ref-format "refs/heads/$Branch" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Invalid target branch: $Branch"
}

$dirty = @(& git status --porcelain=v1 --untracked-files=no)
if ($dirty.Count -gt 0 -and -not $DryRun) {
    $preview = ($dirty | Select-Object -First 15) -join "`n"
    throw "Tracked files are dirty. Commit source changes before creating a snapshot:`n$preview"
}

$sourceSha = ((& git rev-parse "$SourceRef^{commit}") -join "").Trim()
if ($LASTEXITCODE -ne 0 -or -not $sourceSha) {
    throw "Cannot resolve source ref: $SourceRef"
}

$limit = Get-GitHubPolicyLimitBytes -MaxFileMiB $MaxFileMiB
$targetRef = "refs/heads/$Branch"
$parent = $null
if (-not $Orphan) {
    $parentOutput = (& git rev-parse --verify $targetRef 2>$null)
    if ($LASTEXITCODE -eq 0 -and $parentOutput) {
        $parent = (($parentOutput | Select-Object -First 1) -as [string]).Trim()
    }
}

$rawTree = (& git ls-tree -r -l -z $sourceSha) -join ""
if ($LASTEXITCODE -ne 0) {
    throw "Cannot read source tree: $SourceRef"
}

$entries = @($rawTree -split [char]0 | Where-Object { $_ })
$removePaths = [System.Collections.Generic.List[string]]::new()
$oversize = [System.Collections.Generic.List[string]]::new()

foreach ($entry in $entries) {
    if ($entry -notmatch '^\d+\s+\w+\s+[0-9a-f]+\s+(\d+)\t(.+)$') {
        continue
    }

    $bytes = [int64]$matches[1]
    $path = $matches[2]
    if (Test-GitHubExcludedPath -Path $path) {
        $removePaths.Add($path)
    }
    elseif ($bytes -gt $limit) {
        $oversize.Add(("{0:N2} MiB  {1}" -f ($bytes / 1MB), $path))
    }
}

if ($oversize.Count -gt 0) {
    Write-Host "Snapshot blocked by oversized non-excluded files:"
    $oversize | ForEach-Object { Write-Host "  $_" }
    exit 1
}

$oldIndex = $env:GIT_INDEX_FILE
$oldObjects = $env:GIT_OBJECT_DIRECTORY
$oldAlternates = $env:GIT_ALTERNATE_OBJECT_DIRECTORIES
$tempIndex = Join-Path ([IO.Path]::GetTempPath()) ("cloud-phone-github-index-" + [guid]::NewGuid().ToString("N"))
$tempObjects = Join-Path ([IO.Path]::GetTempPath()) ("cloud-phone-github-objects-" + [guid]::NewGuid().ToString("N"))
$env:GIT_INDEX_FILE = $tempIndex

if ($DryRun) {
    $mainObjects = ((& git rev-parse --path-format=absolute --git-path objects) -join "").Trim()
    if (-not $mainObjects -or -not (Test-Path -LiteralPath $mainObjects)) {
        throw "Cannot locate the main Git object directory."
    }
    New-Item -ItemType Directory -Path $tempObjects | Out-Null
    $env:GIT_OBJECT_DIRECTORY = $tempObjects
    $env:GIT_ALTERNATE_OBJECT_DIRECTORIES = $mainObjects
}

try {
    & git read-tree $sourceSha
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create temporary index."
    }

    for ($offset = 0; $offset -lt $removePaths.Count; $offset += 50) {
        $end = [Math]::Min($offset + 49, $removePaths.Count - 1)
        $batch = @($removePaths[$offset..$end])
        $args = @("update-index", "--force-remove", "--") + $batch
        & git @args
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to remove excluded paths from the temporary index."
        }
    }

    $tree = ((& git write-tree) -join "").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $tree) {
        throw "Failed to write snapshot tree."
    }
}
finally {
    if ($null -eq $oldIndex) {
        Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_INDEX_FILE = $oldIndex
    }
    if ($null -eq $oldObjects) {
        Remove-Item Env:GIT_OBJECT_DIRECTORY -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_OBJECT_DIRECTORY = $oldObjects
    }
    if ($null -eq $oldAlternates) {
        Remove-Item Env:GIT_ALTERNATE_OBJECT_DIRECTORIES -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_ALTERNATE_OBJECT_DIRECTORIES = $oldAlternates
    }
    Remove-Item -LiteralPath $tempIndex -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$tempIndex.lock" -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $tempObjects) {
        $resolvedObjects = (Resolve-Path -LiteralPath $tempObjects).Path
        $resolvedTemp = (Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path.TrimEnd("\") + "\"
        if (
            $resolvedObjects.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedObjects).StartsWith("cloud-phone-github-objects-")
        ) {
            Remove-Item -LiteralPath $resolvedObjects -Recurse -Force
        }
        else {
            throw "Refusing to remove unexpected object directory: $resolvedObjects"
        }
    }
}

$parentTree = $null
if ($parent) {
    $parentTree = ((& git rev-parse "$parent^{tree}") -join "").Trim()
}

if ($parentTree -eq $tree) {
    Write-Host "Snapshot is unchanged: $Branch"
    exit 0
}

if ($DryRun) {
    Write-Host "Dry run passed."
    Write-Host "Source      : $sourceSha"
    Write-Host "Tree        : $tree"
    Write-Host "Target      : $targetRef"
    Write-Host "Parent      : $(if ($parent) { $parent } else { '(none; clean root snapshot)' })"
    Write-Host "Excluded    : $($removePaths.Count) path(s)"
    exit 0
}

$message = @(
    "chore(github): source-only snapshot",
    "",
    "Source: $sourceSha",
    "Excluded generated/runtime paths: $($removePaths.Count)"
) -join "`n"

$commitArgs = @("commit-tree", $tree)
if ($parent) {
    $commitArgs += @("-p", $parent)
}
$commitArgs += @("-m", $message)
$commit = ((& git @commitArgs) -join "").Trim()
if ($LASTEXITCODE -ne 0 -or -not $commit) {
    throw "Failed to create snapshot commit."
}

if ($parent) {
    & git update-ref $targetRef $commit $parent
}
else {
    & git update-ref $targetRef $commit
}
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update $targetRef"
}

Write-Host "Created $targetRef at $commit"
Write-Host "Source: $sourceSha; excluded $($removePaths.Count) generated/runtime path(s)."
exit 0

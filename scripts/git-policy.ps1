#Requires -Version 7.0

[CmdletBinding()]
param(
    [ValidateSet("staged", "ref", "push")]
    [string]$Mode = "ref",

    [string]$Rev = "HEAD",

    [string]$RemoteUrl = "",

    [int]$MaxFileMiB = 20
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "git-policy-common.ps1")

$repo = (& git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $repo) {
    throw "Not inside a Git repository."
}
$repo = (($repo | Select-Object -First 1) -as [string]).Trim()
$limit = Get-GitHubPolicyLimitBytes -MaxFileMiB $MaxFileMiB
$warningLimit = [int64]($limit / 2)
$issues = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()
$seen = [System.Collections.Generic.HashSet[string]]::new()

function Add-PolicyFinding {
    param(
        [string]$Path,
        [int64]$Bytes
    )

    if (Test-GitHubExcludedPath -Path $Path) {
        $key = "path:$Path"
        if ($seen.Add($key)) {
            $issues.Add("EXCLUDED  $Path")
        }
        return
    }

    if ($Bytes -gt $limit) {
        $key = "size:$Path"
        if ($seen.Add($key)) {
            $issues.Add(("TOO LARGE {0:N2} MiB  {1}" -f ($Bytes / 1MB), $Path))
        }
        return
    }

    if ($Bytes -gt $warningLimit) {
        $key = "warn:$Path"
        if ($seen.Add($key)) {
            $warnings.Add(("LARGE     {0:N2} MiB  {1}" -f ($Bytes / 1MB), $Path))
        }
    }
}

function Get-ObjectSize {
    param([string]$Object)

    $size = (& git -C $repo cat-file -s $Object 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot read Git object: $Object"
    }
    return [int64](($size | Select-Object -First 1) -as [string]).Trim()
}

function Test-StagedFiles {
    $raw = (& git -C $repo -c core.quotepath=false diff --cached --name-only -z --diff-filter=ACMR) -join ""
    $paths = @($raw -split [char]0 | Where-Object { $_ })
    foreach ($path in $paths) {
        $size = Get-ObjectSize -Object ":$path"
        Add-PolicyFinding -Path $path -Bytes $size
    }
    "Checked $($paths.Count) staged file(s)."
}

function Test-Tree {
    $raw = (& git -C $repo ls-tree -r -l -z $Rev) -join ""
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot read tree: $Rev"
    }

    $entries = @($raw -split [char]0 | Where-Object { $_ })
    foreach ($entry in $entries) {
        if ($entry -notmatch '^\d+\s+\w+\s+[0-9a-f]+\s+(\d+)\t(.+)$') {
            continue
        }
        Add-PolicyFinding -Path $matches[2] -Bytes ([int64]$matches[1])
    }
    "Checked $($entries.Count) path(s) in $Rev."
}

function Test-OutgoingPush {
    $records = @([Console]::In.ReadToEnd() -split "`n" | Where-Object { $_.Trim() })
    if ($records.Count -eq 0) {
        throw "No pre-push records were received."
    }

    $zero = "0000000000000000000000000000000000000000"
    $objectHashes = [System.Collections.Generic.HashSet[string]]::new()
    $objectPaths = [System.Collections.Generic.Dictionary[string, string]]::new()

    foreach ($record in $records) {
        $parts = $record.Trim() -split '\s+'
        if ($parts.Count -lt 4) {
            continue
        }

        $localSha = $parts[1]
        $remoteSha = $parts[3]
        if ($localSha -eq $zero) {
            continue
        }

        $range = if ($remoteSha -eq $zero) { $localSha } else { "$remoteSha..$localSha" }
        $objects = @(& git -C $repo rev-list --objects $range)
        if ($LASTEXITCODE -ne 0) {
            throw "Cannot audit outgoing range: $range. Fetch the remote branch first."
        }
        foreach ($line in $objects) {
            $objectParts = @($line -split ' ', 2)
            $objectHash = $objectParts[0]
            if ($objectHash) {
                [void]$objectHashes.Add($objectHash)
                if ($objectParts.Count -gt 1 -and -not $objectPaths.ContainsKey($objectHash)) {
                    $objectPaths[$objectHash] = $objectParts[1]
                }
            }
        }
    }

    if ($objectHashes.Count -eq 0) {
        "No outgoing objects."
        return
    }

    $inputText = (($objectHashes | Sort-Object) -join "`n") + "`n"
    $batch = @($inputText | & git -C $repo cat-file --batch-check='%(objecttype) %(objectsize) %(objectname)')
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read outgoing object metadata."
    }
    foreach ($line in $batch) {
        if ($line -notmatch '^blob (\d+) ([0-9a-f]+)$') {
            continue
        }

        $bytes = [int64]$matches[1]
        $objectHash = $matches[2]
        $path = $null
        if (-not $objectPaths.TryGetValue($objectHash, [ref]$path)) {
            $path = ""
        }
        Add-PolicyFinding -Path $path -Bytes $bytes
    }

    "Checked $($objectHashes.Count) outgoing object(s) for $RemoteUrl."
}

switch ($Mode) {
    "staged" { Test-StagedFiles }
    "ref" { Test-Tree }
    "push" { Test-OutgoingPush }
}

foreach ($warning in $warnings) {
    Write-Warning $warning
}

if ($issues.Count -gt 0) {
    Write-Host "GitHub policy blocked this operation:"
    $issues | Select-Object -First 80 | ForEach-Object { Write-Host "  $_" }
    if ($issues.Count -gt 80) {
        Write-Host "  ... and $($issues.Count - 80) more issue(s)"
    }
    exit 1
}

Write-Host "GitHub policy check passed."
exit 0

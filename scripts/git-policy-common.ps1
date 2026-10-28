# Shared path policy for GitHub source mirrors and Git hooks.

function Test-GitHubExcludedPath {
    param([Parameter(Mandatory)][string]$Path)

    $normalized = $Path.Replace("\", "/")

    if ($normalized -match "(^|/)\.env\.example$") {
        return $false
    }

    $patterns = @(
        "^_source-history/",
        "^frontend-src-legacy-",
        "^\.workbuddy/",
        "^云手机客户端/",
        "^server/",
        "^extracted/",
        "(^|/)\.env($|\.)",
        "(^|/)(id_rsa|id_ed25519)$",
        "\.(pem|key|ppk|p12|pfx)$",
        "(^|/)(venv|\.venv)/",
        "(^|/)__pycache__/",
        "\.py[cod]$",
        "(^|/)(node_modules|\.npm-cache|\.pnpm-store|\.vite|coverage)/",
        "^frontend/dist(/|_|\.|$)",
        "^backend/instance/",
        "^backend/uploads/",
        "^backend/resources/",
        "^backend/app_bak_",
        "^state/",
        "(^|/)downloads/",
        "\.(db|sqlite|sqlite3)($|-)",
        "\.(log|tmp|part)$",
        "\.bak($|[_-])",
        "\.(apk|exe|msi|dll|pyd|so|node|jar|img|iso|dmg|7z|zip|rar|tar|tgz|gz|bz2|xz)$",
        "\.(mp4|mkv|mov|avi|webm|onnx|ckpt|pt|pth|safetensors|bin)$"
    )

    foreach ($pattern in $patterns) {
        if ($normalized -match $pattern) {
            return $true
        }
    }

    return $false
}

function Get-GitHubPolicyLimitBytes {
    param([int]$MaxFileMiB)

    if ($MaxFileMiB -le 0) {
        throw "MaxFileMiB must be greater than zero."
    }
    return [int64]$MaxFileMiB * 1MB
}

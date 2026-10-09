# This installer is launched elevated by the parent assistant process.
# It records results without forcing a restart or accepting Docker terms.
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskLog = Join-Path $taskProjectRoot 'run\wsl-install.log'
$taskResult = Join-Path $taskProjectRoot 'run\wsl-install-result.json'
try {
    $taskPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'WSL setup requires an elevated Windows administrator session.'
    }
    & "$env:WINDIR\System32\wsl.exe" --install -d Ubuntu --no-launch --web-download 2>&1 |
        Out-File -LiteralPath $taskLog -Encoding utf8
    $taskExitCode = $LASTEXITCODE
    [pscustomobject]@{exit_code=$taskExitCode; completed_at=[DateTime]::UtcNow.ToString('o'); log=$taskLog} |
        ConvertTo-Json | Set-Content -LiteralPath $taskResult -Encoding utf8
    exit $taskExitCode
} catch {
    $_ | Out-File -LiteralPath $taskLog -Encoding utf8 -Append
    [pscustomobject]@{exit_code=1; error=$_.Exception.Message; completed_at=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $taskResult -Encoding utf8
    exit 1
}

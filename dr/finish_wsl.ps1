# Run after Windows has restarted to activate the WSL 2 components.
# Installs Ubuntu if missing, then performs the whole graded Linux lab flow.
param([string]$Distribution = 'Ubuntu-24.04')
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskProjectRoot
$taskWsl = Join-Path $env:WINDIR 'System32\wsl.exe'
$taskInstallResultPath = Join-Path $taskProjectRoot 'run\wsl-install-result.json'
$taskInstallLogPath = Join-Path $taskProjectRoot 'run\wsl-install.log'
if ((Test-Path -LiteralPath $taskInstallResultPath) -and (Test-Path -LiteralPath $taskInstallLogPath)) {
    $taskInstallResult = Get-Content -LiteralPath $taskInstallResultPath -Raw -Encoding utf8 | ConvertFrom-Json
    $taskInstallLog = (Get-Content -LiteralPath $taskInstallLogPath -Raw -Encoding utf8) -replace "`0",''
    $taskLastBoot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
    $taskInstalledAt = [DateTimeOffset]::Parse($taskInstallResult.completed_at).UtcDateTime
    if ($taskInstallLog -match 'rebooted' -and $taskLastBoot -lt $taskInstalledAt) {
        throw 'Windows has not recorded a full restart since WSL setup. Save work, then select Start > Power > Restart.'
    }
}
$taskRepairResultPath = Join-Path $taskProjectRoot 'run\wsl-feature-repair.json'
if (Test-Path -LiteralPath $taskRepairResultPath) {
    $taskRepairResult = Get-Content -LiteralPath $taskRepairResultPath -Raw -Encoding utf8 | ConvertFrom-Json
    $taskRepairTime = [DateTimeOffset]::Parse($taskRepairResult.completed_at).UtcDateTime
    $taskBootTime = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
    if ($taskRepairResult.restart_needed -and $taskBootTime -lt $taskRepairTime) {
        throw 'Windows requires Restart after the newly enabled WSL component. Save work and select Start > Power > Restart.'
    }
}
$taskWslVersion = 2
$taskWslStatus = ((& $taskWsl --status) -join "`n") -replace "`0",''
if ($taskWslStatus -match 'WSL2 is unable to start' -and $taskWslStatus -match 'WSL1 is not supported' -and
    (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')) {
    throw 'Windows has pending component changes and WSL cannot start. Save work, then select Update and restart, or Settings > Windows Update > Restart now. A plain Restart can defer applying updates on current Windows versions.'
}
if ($taskWslStatus -match 'WSL2 is unable to start' -and $taskWslStatus -notmatch 'WSL1 is not supported') {
    # The graded bare Python drill also works on WSL1. Do not alter the global
    # default version; explicitly select the supported version for this distro.
    $taskWslVersion = 1
    Write-Output 'WSL2 is unavailable; using WSL1 Linux compatibility for the bare lab. Docker still requires WSL2.'
}
$taskDistributions = ((& $taskWsl --list --quiet 2>$null) -join "`n") -replace "`0",''
if ($taskDistributions -notmatch ('(?m)^' + [regex]::Escape($Distribution) + '\s*$')) {
    & $taskWsl --install -d $Distribution --version $taskWslVersion --no-launch --web-download
    if ($LASTEXITCODE -ne 0) {
        throw "Ubuntu install failed (exit $LASTEXITCODE). Check whether Windows needs a restart."
    }
}
$taskLinuxRoot = (& $taskWsl -d $Distribution -u root --exec wslpath -a $taskProjectRoot) -join ''
$taskLinuxRoot = $taskLinuxRoot.Trim()
if ($LASTEXITCODE -ne 0 -or -not $taskLinuxRoot.StartsWith('/mnt/')) {
    throw 'Could not access the project folder from Ubuntu. See the WSL error above.'
}
& $taskWsl -d $Distribution -u root --exec env "LAB_WSL_VERSION=$taskWslVersion" "LAB_LINUX_DISTRIBUTION=$Distribution" bash "$taskLinuxRoot/dr/finish_linux.sh"
if ($LASTEXITCODE -ne 0) {
    throw "Linux lab verification failed (exit $LASTEXITCODE). Inspect run/*.log."
}
$taskEnvironment = Get-Content -LiteralPath 'reports\environment.json' -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $taskEnvironment.graded_linux_path_verified) {
    throw 'Linux verification flag is still false; do not submit the earlier Windows evidence as Linux.'
}
Write-Output "Completed: Linux bare --mock drill, reports, tests, run\lab23-submission.zip"

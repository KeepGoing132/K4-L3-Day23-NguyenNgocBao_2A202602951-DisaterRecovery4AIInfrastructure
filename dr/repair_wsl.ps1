# Complete Windows feature dependencies for WSL, without automatically rebooting.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskTranscript = Join-Path $taskRoot 'run\wsl-feature-repair.log'
$taskResultPath = Join-Path $taskRoot 'run\wsl-feature-repair.json'
Start-Transcript -LiteralPath $taskTranscript -Force | Out-Null
try {
    $taskPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Windows feature repair requires an elevated administrator session.'
    }
    $taskBefore = @()
    $taskAfter = @()
    $taskRestartNeeded = $false
    foreach ($taskFeatureName in @('VirtualMachinePlatform','Microsoft-Windows-Subsystem-Linux')) {
        $taskFeature = Get-WindowsOptionalFeature -Online -FeatureName $taskFeatureName
        $taskBefore += [pscustomobject]@{name=$taskFeatureName; state=$taskFeature.State.ToString()}
        if ($taskFeature.State.ToString() -ne 'Enabled') {
            $taskEnabled = Enable-WindowsOptionalFeature -Online -FeatureName $taskFeatureName -All -NoRestart
            $taskRestartNeeded = $taskRestartNeeded -or $taskEnabled.RestartNeeded
        }
        $taskNewFeature = Get-WindowsOptionalFeature -Online -FeatureName $taskFeatureName
        $taskAfter += [pscustomobject]@{name=$taskFeatureName; state=$taskNewFeature.State.ToString()}
    }
    $taskBootInfo = (& "$env:WINDIR\System32\bcdedit.exe" /enum) -join "`n"
    $taskHypervisorConfig = ($taskBootInfo -split "`n" | Where-Object {$_ -match 'hypervisorlaunchtype'}) -join ''
    [pscustomobject]@{before=$taskBefore; after=$taskAfter; restart_needed=$taskRestartNeeded;
        reboot_pending=(Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending');
        hypervisor_config=$taskHypervisorConfig; vmcompute_present=(Test-Path -LiteralPath "$env:WINDIR\System32\vmcompute.exe");
        completed_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $taskResultPath -Encoding utf8
} catch {
    $_ | Out-String | Add-Content -LiteralPath $taskTranscript -Encoding utf8
    [pscustomobject]@{error=$_.Exception.Message; completed_at=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $taskResultPath -Encoding utf8
    exit 1
} finally {
    Stop-Transcript | Out-Null
}

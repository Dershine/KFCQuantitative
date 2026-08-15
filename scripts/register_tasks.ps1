$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_task.ps1"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$TaskPath = "\KFCQuant\"
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Virtual environment not found: $PythonExe"
}

$ScheduleJson = & $PythonExe -m kfcquant.cli schedule-plan --json | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "KFCQuant schedule configuration is invalid"
}
$Schedule = $ScheduleJson | ConvertFrom-Json

function Register-KFCQuantTask {
    param(
        [string]$Name,
        [string]$Command,
        [datetime]$At,
        [timespan]$RepeatInterval = [timespan]::Zero,
        [timespan]$RepeatDuration = [timespan]::Zero
    )

    $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" $Command"
    $Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $ProjectRoot
    if ($RepeatInterval -gt [timespan]::Zero) {
        $Trigger = New-ScheduledTaskTrigger -Daily -At $At -RepetitionInterval $RepeatInterval -RepetitionDuration $RepeatDuration
    } else {
        $Trigger = New-ScheduledTaskTrigger -Daily -At $At
    }
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable:$false -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Action $Action -Trigger $Trigger -Settings $Settings -Description "KFCQuant research-only scheduled task" -Force | Out-Null
}

foreach ($Task in $Schedule.tasks) {
    $At = [datetime]::ParseExact([string]$Task.at, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
    Register-KFCQuantTask -Name ([string]$Task.name) -Command ([string]$Task.command) -At $At
}

$MonitorAt = [datetime]::ParseExact(
    [string]$Schedule.monitor.start,
    "HH:mm",
    [Globalization.CultureInfo]::InvariantCulture
)
Register-KFCQuantTask -Name ([string]$Schedule.monitor.name) `
    -Command ([string]$Schedule.monitor.command) `
    -At $MonitorAt `
    -RepeatInterval (New-TimeSpan -Minutes ([int]$Schedule.monitor.interval_minutes)) `
    -RepeatDuration (New-TimeSpan -Minutes ([int]$Schedule.monitor.duration_minutes))

Write-Host "KFCQuant tasks registered under $TaskPath. Jobs safely no-op outside trading sessions."

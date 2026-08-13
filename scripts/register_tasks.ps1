$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_task.ps1"
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$TaskPath = "\KFCQuant\"

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

Register-KFCQuantTask -Name "SyncCalendar" -Command "sync-calendar" -At "08:00"
Register-KFCQuantTask -Name "Morning" -Command "run-morning" -At "08:30"
Register-KFCQuantTask -Name "EvaluateMorning" -Command "evaluate-morning" -At "14:35"
Register-KFCQuantTask -Name "Preclose" -Command "run-preclose" -At "14:40"
Register-KFCQuantTask -Name "CaptureFill" -Command "capture-fill" -At "14:45"
# BaoStock's free daily data is published later than commercial EOD feeds.
Register-KFCQuantTask -Name "SyncEod" -Command "sync-eod" -At "18:10"
Register-KFCQuantTask -Name "Postclose" -Command "run-postclose" -At "20:30"
Register-KFCQuantTask -Name "Monitor" -Command "monitor-paper" -At "09:30" -RepeatInterval (New-TimeSpan -Minutes 5) -RepeatDuration (New-TimeSpan -Hours 5 -Minutes 30)

Write-Host "KFCQuant tasks registered under $TaskPath. Jobs safely no-op outside trading sessions."

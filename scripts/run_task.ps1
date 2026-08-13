param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("sync-calendar", "run-morning", "evaluate-morning", "run-preclose", "capture-fill", "monitor-paper", "sync-eod", "run-postclose")]
    [string]$Command
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Virtual environment not found: $PythonExe"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location -LiteralPath $ProjectRoot
$LogPath = Join-Path $LogDir ("{0}-{1}.log" -f $Command, (Get-Date -Format "yyyyMMdd"))

& $PythonExe -m kfcquant.cli $Command *>> $LogPath
exit $LASTEXITCODE

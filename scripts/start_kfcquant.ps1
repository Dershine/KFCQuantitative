param(
    [switch]$RunTests,
    [switch]$SkipTests,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Get-ProjectFingerprint {
    param([switch]$IncludeTests)

    $files = @(
        (Join-Path $ProjectRoot "pyproject.toml"),
        (Join-Path $ProjectRoot "README.md")
    )
    $files += Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "src") -Recurse -File -Filter "*.py" |
        Select-Object -ExpandProperty FullName
    if ($IncludeTests) {
        $files += Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "tests") -Recurse -File -Filter "*.py" |
            Select-Object -ExpandProperty FullName
    }

    $lines = foreach ($file in ($files | Sort-Object)) {
        "${file}:$((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash)"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "")
    }
    finally {
        $sha.Dispose()
    }
}

function Find-Python313 {
    $candidates = @()
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        $candidates += [pscustomobject]@{ Path = $pyLauncher.Source; Prefix = @("-3.13") }
    }

    $projectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython) {
        $candidates += [pscustomobject]@{ Path = $projectPython; Prefix = @() }
    }

    $systemPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $systemPython) {
        $candidates += [pscustomobject]@{ Path = $systemPython.Source; Prefix = @() }
    }

    foreach ($candidate in $candidates) {
        $checkArgs = @($candidate.Prefix) + @(
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)"
        )
        & $candidate.Path @checkArgs 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    return $null
}

try {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    Set-Location -LiteralPath $ProjectRoot

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $userSid = $identity.User.Value
    $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        throw "LOCALAPPDATA is unavailable for the current Windows user."
    }

    # Every Windows account, including a sandbox account, gets a different SID
    # directory. This prevents pytest, pip, coverage and bytecode cache ACLs from
    # leaking between Codex and the interactive user.
    $UserRoot = Join-Path $localAppData ("KFCQuantitative\" + $userSid)
    $RuntimeRoot = Join-Path $UserRoot "runtime"
    $TempRoot = Join-Path $RuntimeRoot "temp"
    $PytestTemp = Join-Path $RuntimeRoot "pytest-temp"
    $PytestCache = Join-Path $RuntimeRoot "pytest-cache"
    $PythonCache = Join-Path $RuntimeRoot "pycache"
    $CoverageFile = Join-Path $RuntimeRoot ".coverage"
    $VenvRoot = Join-Path $UserRoot "venv-py313"
    $PythonExe = Join-Path $VenvRoot "Scripts\python.exe"

    foreach ($directory in @($UserRoot, $RuntimeRoot, $TempRoot, $PytestCache, $PythonCache)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }

    $env:TEMP = $TempRoot
    $env:TMP = $TempRoot
    $env:PYTHONPYCACHEPREFIX = $PythonCache
    $env:COVERAGE_FILE = $CoverageFile
    $env:STREAMLIT_SERVER_HEADLESS = "false"
    $env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"
    $env:PYTHONUTF8 = "1"

    Write-Host "KFCQuant project : $ProjectRoot"
    Write-Host "Private runtime  : $UserRoot"
    if ($identity.Owner -ne $null) {
        Write-Host "Windows identity : $($identity.Name)"
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Step "Creating a private Python 3.13 environment"
        $bootstrap = Find-Python313
        if ($null -eq $bootstrap) {
            throw "Python 3.13 was not found. Install Python 3.13, then run Start-KFCQuant.cmd again."
        }
        $venvArgs = @($bootstrap.Prefix) + @("-m", "venv", $VenvRoot)
        Invoke-Checked -FilePath $bootstrap.Path -ArgumentList $venvArgs `
            -FailureMessage "Could not create the private virtual environment"
    }

    $PackageFingerprint = Get-ProjectFingerprint
    $PackageStamp = Join-Path $RuntimeRoot "installed-package.sha256"
    $InstalledFingerprint = if (Test-Path -LiteralPath $PackageStamp) {
        (Get-Content -LiteralPath $PackageStamp -Raw).Trim()
    } else {
        ""
    }

    if ($InstalledFingerprint -ne $PackageFingerprint) {
        Write-Step "Installing or updating KFCQuant dependencies"
        # Build from a user-owned copy. Some Python build backends create
        # egg-info beside the source tree; copying first ensures that even those
        # temporary writes never touch a Codex-owned workspace path.
        $PackageSources = Join-Path $RuntimeRoot "package-sources"
        $PackageSource = Join-Path $PackageSources $PackageFingerprint
        if (-not (Test-Path -LiteralPath (Join-Path $PackageSource "pyproject.toml"))) {
            New-Item -ItemType Directory -Force -Path $PackageSource | Out-Null
            Copy-Item -LiteralPath (Join-Path $ProjectRoot "pyproject.toml") -Destination $PackageSource
            Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination $PackageSource
            $PackageSrc = Join-Path $PackageSource "src"
            New-Item -ItemType Directory -Force -Path $PackageSrc | Out-Null
            Copy-Item -LiteralPath (Join-Path $ProjectRoot "src\kfcquant") -Destination $PackageSrc -Recurse
        }
        Invoke-Checked -FilePath $PythonExe -ArgumentList @("-m", "pip", "install", "--upgrade", "pip") `
            -FailureMessage "pip could not update itself; check the network connection"
        Invoke-Checked -FilePath $PythonExe -ArgumentList @(
            "-m", "pip", "install", "--upgrade", "${PackageSource}[dev]"
        ) -FailureMessage "KFCQuant dependencies could not be installed; check the network connection"
        Set-Content -LiteralPath $PackageStamp -Value $PackageFingerprint -Encoding ASCII
    } else {
        Write-Step "Dependencies are already up to date"
    }

    $EnvFile = Join-Path $ProjectRoot ".env"
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        $EnvExample = Join-Path $ProjectRoot ".env.example"
        if (-not (Test-Path -LiteralPath $EnvExample)) {
            throw "Both .env and .env.example are missing."
        }
        Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
        Start-Process -FilePath "notepad.exe" -ArgumentList @($EnvFile)
        throw ".env was created and opened in Notepad. Add the DeepSeek API key, save it, then run Start-KFCQuant.cmd again."
    }

    $TestFingerprint = Get-ProjectFingerprint -IncludeTests
    $TestStamp = Join-Path $RuntimeRoot "verified-tests.sha256"
    $VerifiedFingerprint = if (Test-Path -LiteralPath $TestStamp) {
        (Get-Content -LiteralPath $TestStamp -Raw).Trim()
    } else {
        ""
    }
    $ShouldRunTests = $RunTests -or ((-not $SkipTests) -and ($VerifiedFingerprint -ne $TestFingerprint))

    if ($ShouldRunTests) {
        Write-Step "Running isolated code checks (first run or source change)"
        Invoke-Checked -FilePath $PythonExe -ArgumentList @(
            "-m", "ruff", "check", "src", "tests", "--no-cache"
        ) -FailureMessage "The code quality check failed"
        Invoke-Checked -FilePath $PythonExe -ArgumentList @(
            "-m", "pytest", "--cov=kfcquant", "--basetemp", $PytestTemp,
            "-o", "cache_dir=$PytestCache"
        ) -FailureMessage "The automated tests failed"
        Set-Content -LiteralPath $TestStamp -Value $TestFingerprint -Encoding ASCII
    } else {
        Write-Step "Code checks already passed for this version"
    }

    Write-Step "Checking local configuration"
    Invoke-Checked -FilePath $PythonExe -ArgumentList @("-m", "kfcquant.cli", "doctor") `
        -FailureMessage "The local configuration check failed"

    if ($CheckOnly) {
        Write-Host "`nAll checks passed. The dashboard was not started because -CheckOnly was used." -ForegroundColor Green
        exit 0
    }

    Write-Step "Starting KFCQuant at http://localhost:8501"
    Write-Host "Keep this window open. Press Ctrl+C here to stop the dashboard."
    & $PythonExe -m kfcquant.cli serve
    exit $LASTEXITCODE
}
catch {
    Write-Host "`n[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "No market database, raw snapshot, report, or .env secret was deleted." -ForegroundColor Yellow
    exit 1
}

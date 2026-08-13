@echo off
setlocal
chcp 65001 >nul
title KFCQuant Launcher

set "LAUNCHER=%~dp0scripts\start_kfcquant.ps1"
if not exist "%LAUNCHER%" (
    echo [ERROR] Launcher script not found: "%LAUNCHER%"
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] KFCQuant did not start. Read the message above, then press any key.
    pause >nul
)

exit /b %EXIT_CODE%

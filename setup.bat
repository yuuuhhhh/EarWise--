@echo off
setlocal
title EarWise - Environment Setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "EARWISE_SETUP_EXIT=%ERRORLEVEL%"
if not "%EARWISE_SETUP_EXIT%"=="0" echo EarWise setup failed. Read the error above and rerun this script.
if "%EARWISE_SETUP_EXIT%"=="0" echo EarWise is ready. Run start-simulation.bat or start.bat.
pause
exit /b %EARWISE_SETUP_EXIT%

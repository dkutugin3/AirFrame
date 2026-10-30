@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0verification\verify-windows.ps1" -BundleDirectory "%~dp0."
set "AIRFRAME_CHECK_RESULT=%ERRORLEVEL%"
echo.
if "%AIRFRAME_CHECK_RESULT%"=="0" (echo AirFrame Windows verification PASSED.) else (echo AirFrame Windows verification FAILED. See the report folder printed above.)
pause
exit /b %AIRFRAME_CHECK_RESULT%

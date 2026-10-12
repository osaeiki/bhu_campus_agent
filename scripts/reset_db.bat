@echo off
cd /d %~dp0..
echo This will DELETE data\campus.db. It will be recreated on next start.
choice /M "Continue"
if errorlevel 2 exit /b 0
del /q data\campus.db 2>nul
echo Done.
pause

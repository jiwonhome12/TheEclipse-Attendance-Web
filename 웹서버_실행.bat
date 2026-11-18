@echo off
rem Double-click to start the web attendance server (web\server.py) from this folder.
rem Works wherever the folder is copied: %~dp0 is the folder this file is in.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\start_web.ps1" %*
if errorlevel 1 pause

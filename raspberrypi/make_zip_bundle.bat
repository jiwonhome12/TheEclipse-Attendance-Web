@echo off
rem Double-click to make TheEclipse_RaspberryPi.zip in the dist folder (runs make_usb_bundle.ps1 -Zip)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_usb_bundle.ps1" -Zip
pause

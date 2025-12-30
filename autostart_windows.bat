@echo off
:: Ustawia folder roboczy na lokalizację tego pliku .bat (naprawia problem ze ścieżkami)
cd /d "%~dp0"

:: Uruchamia skrypt PowerShell z flagą Bypass (omija blokadę wykonywania skryptów)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\autostart_windows.ps1"

pause
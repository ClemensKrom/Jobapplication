@echo off
title JobRadar
cd /d "%~dp0"
echo Starting JobRadar... your browser will open in a moment.
echo Keep this window open while you use JobRadar. Close it to stop.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python server.py
  ) else (
    echo Python was not found on this computer.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
    echo Then double-click this file again.
  )
)
echo.
pause

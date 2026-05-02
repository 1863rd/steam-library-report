@echo off
setlocal
cd /d "%~dp0"
python generate_report.py
if errorlevel 1 (
  echo.
  echo Python command failed. Try installing Python from https://www.python.org/downloads/
  echo or run: py generate_report.py
  echo.
  pause
)

@echo off
cd /d "%~dp0"
py -3 MI_Bridge.py >> "%LOCALAPPDATA%\GeoMaker_MI_Builder_Bridge.log" 2>&1
if errorlevel 1 (
  echo [%date% %time%] Bridge failed. See %%LOCALAPPDATA%%\GeoMaker_MI_Builder_Bridge.log >> "%LOCALAPPDATA%\GeoMaker_MI_Builder_Bridge.log"
)

@echo off
cd /d "%~dp0"
py -3 MI_MBtiles_Create.py
if errorlevel 1 pause

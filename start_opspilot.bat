@echo off
cd /d "%~dp0backend"
set OPSPILOT_AUTO_SHUTDOWN=1
start "" http://localhost:8420
python -m uvicorn main:app --port 8420

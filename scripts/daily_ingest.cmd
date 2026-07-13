@echo off
rem Daily catch-up ingest + cluster alerts (Phase 4).
rem Wired for Windows Task Scheduler via scripts\schedule_daily.ps1.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.ingest --daily >> "data\daily_ingest.log" 2>&1

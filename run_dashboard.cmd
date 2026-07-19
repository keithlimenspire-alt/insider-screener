@echo off
rem Double-click to run the insider-buying dashboard.
rem Keeps running until you close this window; open http://localhost:8501
cd /d "%~dp0"
".venv\Scripts\streamlit.exe" run dashboard.py --server.headless true

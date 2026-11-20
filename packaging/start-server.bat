@echo off
REM ClipForge API server — double-click to start.
REM No Python needed. API: http://127.0.0.1:8000 (docs at /docs).
cd /d "%~dp0"
if not exist clipforge-server.exe (
  echo clipforge-server.exe is missing from this folder.
  pause
  exit /b 1
)
echo Starting ClipForge API on http://127.0.0.1:8000 ...
echo API docs: http://127.0.0.1:8000/docs
echo Press Ctrl+C to stop.
clipforge-server.exe
pause

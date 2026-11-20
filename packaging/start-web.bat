@echo off
REM ClipForge web UI (Next.js standalone build) — needs Node.js 20+.
REM The API server must be running too (use start-server.bat).
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. The web UI needs Node.js 20+:
  echo https://nodejs.org
  echo The API server itself does NOT need Node — use start-server.bat.
  pause
  exit /b 1
)
if not exist web\server.js (
  echo The web UI build is missing from this folder (web\server.js not found).
  pause
  exit /b 1
)
set PORT=3000
echo Starting ClipForge web UI on http://localhost:3000 ...
echo (Keep the API server running — start-server.bat)
node web\server.js
pause

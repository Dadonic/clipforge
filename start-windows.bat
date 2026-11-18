@echo off
rem ClipForge AI on a Windows laptop. Double-click this file.
rem First run installs what is missing (it asks before each install) and takes a few minutes.
setlocal
cd /d "%~dp0"
title ClipForge AI

rem Your recordings, clips and database live outside this folder, so replacing the folder with a
rem newer download never deletes them.
if not defined CLIPFORGE_DATA_DIR set "CLIPFORGE_DATA_DIR=%USERPROFILE%\ClipForgeData"
if not defined CLIPFORGE_MODELS_DIR set "CLIPFORGE_MODELS_DIR=%USERPROFILE%\ClipForgeData\models"
if not exist "%CLIPFORGE_DATA_DIR%" mkdir "%CLIPFORGE_DATA_DIR%"

echo.
echo   ClipForge AI
echo   Data folder: %CLIPFORGE_DATA_DIR%
echo.

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto need_python

if exist "%~dp0tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
ffmpeg -version >nul 2>nul
if errorlevel 1 goto need_ffmpeg

if exist ".venv\ready.txt" goto run
echo   First run: installing ClipForge's Python packages. This needs internet and takes a few minutes.
%PY% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
echo ok> ".venv\ready.txt"

:run
echo.
echo   Starting. Your browser will open http://localhost:8000 in a few seconds.
echo   Keep this window open while you use ClipForge. Close it to stop ClipForge.
echo.
start "" /min cmd /c "timeout /t 8 /nobreak >nul & start http://localhost:8000"
cd backend
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo   ClipForge stopped.
pause
exit /b 0

:need_python
echo   Python is not installed on this computer.
choice /c YN /m "  Install Python 3.12 now (free, from the official source via winget)"
if errorlevel 2 goto manual
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
echo.
echo   Finished. Close this window, then double-click start-windows.bat again.
pause
exit /b 0

:need_ffmpeg
echo   FFmpeg (the video tool ClipForge uses) is not installed on this computer.
choice /c YN /m "  Install FFmpeg now (free, via winget)"
if errorlevel 2 goto manual
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
echo.
echo   Finished. Close this window, then double-click start-windows.bat again.
pause
exit /b 0

:manual
echo.
echo   Install it yourself, then run this file again:
echo     Python:  https://www.python.org/downloads/   (tick "Add python.exe to PATH" in the installer)
echo     FFmpeg:  open PowerShell and run   winget install Gyan.FFmpeg
pause
exit /b 1

:fail
echo.
echo   Setup failed. Scroll up for the first red error line and send it to whoever helps you with ClipForge.
echo   To start setup again from scratch, delete the .venv folder next to this file.
pause
exit /b 1

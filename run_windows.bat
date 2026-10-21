@echo off
REM PUP Smart Room Reservation System - one-click local start for Windows.
REM Double-click this file. The site opens at http://127.0.0.1:5000
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 ( set "PY=py -3" ) else ( set "PY=python" )

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 ( echo Could not create a virtual environment. Install Python 3.10+ from python.org and tick "Add Python to PATH". & pause & exit /b 1 )
)

call ".venv\Scripts\activate.bat"
echo Installing dependencies...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
if errorlevel 1 ( echo Dependency installation failed. Check your internet connection. & pause & exit /b 1 )

if not exist ".env" copy ".env.example" ".env" >nul

start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:5000"
python app.py
pause

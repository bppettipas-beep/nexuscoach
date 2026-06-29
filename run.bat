@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt -q
echo.
echo ============================================
echo   NexusCoach is starting...
echo   http://localhost:5000
echo   Keep this window open!
echo   Ctrl+C to stop.
echo ============================================
echo.
python app.py
pause

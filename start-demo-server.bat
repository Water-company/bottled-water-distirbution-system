@echo off
cd /d "%~dp0"
echo Starting AquaFlow demo server at http://127.0.0.1:8000/
".\.venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000 --noreload
if errorlevel 1 (
    echo.
    echo The server stopped because of an error.
    pause
)

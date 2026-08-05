@echo off
REM Double-click to launch the ground station.
REM
REM Opens the browser and starts the local server. Closing this window stops
REM the server, which is why it stays open.

title drone-gcs ground station
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Could not find the Python environment at .venv
    echo.
    echo Create it once with:
    echo     py -3.14 -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Where reconstructions are looked for. Change this to point elsewhere.
if "%DRONE_DATA_DIR%"=="" set DRONE_DATA_DIR=C:\DroneData\samples

echo.
echo   drone-gcs ground station
echo   ------------------------
echo   Reconstructions: %DRONE_DATA_DIR%
echo   Opening http://127.0.0.1:8000
echo.
echo   Leave this window open. Close it to stop the server.
echo.

.venv\Scripts\python.exe -m gcs.server

REM Only reached if the server exits, usually because the port is in use.
echo.
echo Server stopped.
pause

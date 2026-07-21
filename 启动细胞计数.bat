@echo off
title Zeiss CZI Cell Counter
cd /d "%~dp0"

echo.
echo  ========================================
echo    Zeiss CZI Cell Counter
echo  ========================================
echo.
echo  Starting server, please wait...
echo.
echo  Close this window to stop the server
echo  ========================================
echo.

REM Stop stale CellCounter servers so requests cannot hit an old code version.
echo  Closing previous CellCounter server instances...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":5000"') do (
    taskkill /PID %%p /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

REM Python first -- always run the latest code
where python >nul 2>&1
if %errorlevel% equ 0 (
    echo  [OK] Launching Python version...
    cd /d "%~dp0"
    start "" python "app.py"
    goto :END
)

REM Try known Python install paths
for %%p in (
    "E:\python-3.13.7\python.exe"
    "C:\Python313\python.exe"
    "C:\Program Files\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
) do (
    if exist %%p (
        echo  [OK] Launching Python version...
        cd /d "%~dp0"
        start "" %%p "app.py"
        goto :END
    )
)

REM Fallback: try bundled CellCounter.exe
if exist "%~dp0CellCounter\CellCounter.exe" (
    echo  Launching bundled app (legacy)...
    start "" "%~dp0CellCounter\CellCounter.exe"
    goto :END
)

echo  ERROR: Python not found. Please install Python 3.13.
echo.
pause

:END

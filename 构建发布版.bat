@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   Building Cell Counter release
echo ========================================

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found.
    pause
    exit /b 1
)

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing build dependencies...
    python -m pip install -r requirements-build.txt
    if errorlevel 1 goto :error
)

python -m PyInstaller --noconfirm --clean --distpath release --workpath build CellCounter.spec
if errorlevel 1 goto :error

copy /y "使用说明.txt" "release\细胞计数工具\使用说明.txt" >nul
echo.
echo Build complete:
echo   release\细胞计数工具\细胞计数工具.exe
pause
exit /b 0

:error
echo.
echo Build failed. Review the messages above.
pause
exit /b 1

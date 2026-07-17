@echo off
echo This will stop the CellCounter server.
taskkill /f /im CellCounter.exe 2>nul
echo Server stopped.
pause

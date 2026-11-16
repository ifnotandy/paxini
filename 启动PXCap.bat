@echo off
setlocal
echo ============================================
echo   PXCap Studio launcher (WSL2)
echo ============================================
echo.
echo [1/3] waking up WSL ...
start /min "" wsl.exe -d Ubuntu -- bash -c "sleep 6"
timeout /t 2 /nobreak >nul

echo [2/3] attaching glove to WSL ...
"C:\Program Files\usbipd-win\usbipd.exe" attach --wsl --hardware-id 28e9:018a
if errorlevel 1 echo    (device not plugged in, or already attached - continuing)
timeout /t 3 /nobreak >nul

echo [3/3] launching PXCap Studio ...
start "PXCap Studio" /min C:\Windows\System32\wsl.exe -d Ubuntu --cd "/opt/PXCap Studio" -- /usr/bin/pxcap-studio
echo.
echo Done. The window should appear in a few seconds.
timeout /t 3 /nobreak >nul

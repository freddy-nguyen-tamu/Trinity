@echo off
setlocal
cd /d "%~dp0" || exit /b 1
py -m PyInstaller --clean --noconfirm trinity.spec
if errorlevel 1 (
    echo.
    echo Trinity build failed. Check the errors above.
    exit /b 1
)
echo.
echo Trinity built at: "%~dp0dist\Trinity\Trinity.exe"
explorer.exe "%~dp0dist\Trinity"

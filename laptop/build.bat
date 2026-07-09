@echo off
setlocal

echo Building trinity.exe...
pyinstaller trinity.spec --noconfirm
if errorlevel 1 (
    echo BUILD FAILED
    pause
    exit /b 1
)

set "SRC=%~dp0dist\trinity.exe"
set "DST=%~dp0trinity.exe"

if not exist "%SRC%" (
    echo ERROR: %SRC% not found after build
    pause
    exit /b 1
)

echo Copying trinity.exe to laptop folder...
copy /Y "%SRC%" "%DST%"
if errorlevel 1 (
    echo COPY FAILED
    pause
    exit /b 1
)

echo Pinning trinity.exe to taskbar...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe = '%DST%';" ^
  "$shortcutPath = [System.IO.Path]::Combine($env:APPDATA, 'Microsoft', 'Internet Explorer', 'Quick Launch', 'User Pinned', 'TaskBar', 'Trinity.lnk');" ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$shortcut = $ws.CreateShortcut($shortcutPath);" ^
  "$shortcut.TargetPath = $exe;" ^
  "$shortcut.WorkingDirectory = Split-Path $exe;" ^
  "$shortcut.Save();" ^
  "Write-Host 'Shortcut created at:' $shortcutPath"

echo.
echo Done. You may need to right-click trinity.exe on the taskbar and select
echo "Pin to taskbar" if it did not appear automatically.
echo.
pause

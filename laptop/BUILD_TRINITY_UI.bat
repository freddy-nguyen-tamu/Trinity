@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1

echo ==============================================================
echo  Building the FAST Trinity UI with MP3 TRIMMER and original icon
 echo ==============================================================
 echo.
for %%F in (trinity_ui.py trinity.py mp3trimmer.py trinity.spec trinity.ico) do (
    if not exist "%%F" (
        echo ERROR: Missing %%F in %CD%.
        exit /b 1
    )
)

rem Always use the ONE-FOLDER build. Building a single-file replacement for
rem laptop\trinity.exe would revive the slow PyInstaller extraction startup.
set "TRINITY_PYTHON=python"
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "TRINITY_PYTHON=py -3"
%TRINITY_PYTHON% -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3 is needed to rebuild Trinity.
    exit /b 1
)
%TRINITY_PYTHON% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller is missing. Install it with:
    echo   py -3 -m pip install pyinstaller
    exit /b 1
)

echo Building the fast one-folder executable...
%TRINITY_PYTHON% -m PyInstaller --clean --noconfirm "trinity.spec"
if errorlevel 1 (
    echo ERROR: Fast Trinity build failed. Existing app is unchanged.
    exit /b 1
)
if not exist "dist\Trinity\Trinity.exe" (
    echo ERROR: Built executable is missing.
    exit /b 1
)

rem Update only the previously named Trinity shortcuts, when present. Do not
rem silently replace laptop\trinity.exe with a slower single-file executable.
set "TRINITY_FAST_EXE=%CD%\dist\Trinity\Trinity.exe"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$target=$env:TRINITY_FAST_EXE; $folders=@((Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'), [Environment]::GetFolderPath('Desktop')); $shell=New-Object -ComObject WScript.Shell; foreach($dir in $folders){if(-not(Test-Path -LiteralPath $dir)){continue}; foreach($lnk in @(Get-ChildItem -LiteralPath $dir -Filter 'Trinity.lnk' -File -ErrorAction SilentlyContinue)){try{$shortcut=$shell.CreateShortcut($lnk.FullName); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory=(Split-Path -Parent $target); $shortcut.IconLocation=$target+',0'; $shortcut.Save(); Write-Host ('Updated '+$lnk.FullName)}catch{Write-Warning $_.Exception.Message}}}"

echo.
echo FAST Trinity build: "%TRINITY_FAST_EXE%"
if exist "trinity.exe" (
    echo NOTE: The old laptop\trinity.exe is a DIFFERENT, slower launcher.
    echo       The fast launcher is dist\Trinity\Trinity.exe.
)
echo Starting the updated UI...
start "" "%TRINITY_FAST_EXE%"
exit /b 0

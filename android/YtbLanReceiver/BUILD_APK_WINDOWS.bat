@echo off
setlocal
cd /d "%~dp0"
where gradle >nul 2>nul
if errorlevel 1 (
  echo Gradle was not found on PATH.
  echo Open this folder in Android Studio and use Build ^> Build Bundle(s) / APK(s) ^> Build APK(s).
  exit /b 1
)
gradle assembleDebug
if errorlevel 1 exit /b 1
echo APK built at app\build\outputs\apk\debug\app-debug.apk

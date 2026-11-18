@echo off
rem Keep the legacy build entry point working. The old script expected
rem dist\trinity.exe, but the project spec produces dist\Trinity\Trinity.exe.
call "%~dp0BUILD_TRINITY_UI.bat"
exit /b %errorlevel%

@echo off
REM ===========================================================================
REM Build the Store Kit ZIP to take to a Store PC.
REM
REM WINDOWS BUILD TOOLING, deliberately. The Store Receiver is Windows
REM software: PyInstaller executables, a Windows Scheduled Task for auto-start,
REM and a Windows audio endpoint. Unlike the HQ start/stop wrappers, this one
REM has no cross-platform equivalent to lose.
REM
REM A THIN WRAPPER. The orchestration lives in
REM tools\build_store_receiver_zip.py, which calls the existing supported
REM build scripts rather than reimplementing them:
REM
REM     scripts\Build-EchoCastReceiver.ps1
REM     scripts\Test-EchoCastReceiverPackage.ps1
REM     scripts\Build-EchoCastStoreSetupPackage.ps1
REM
REM Output: artifacts\EchoCast-Store-Kit-<version>-<commit>-<timestamp>.zip
REM ===========================================================================
setlocal

set "REPO=%~dp0"
pushd "%REPO%"

call :find_python
if errorlevel 1 goto :missing_python

"%PYTHON%" "%REPO%tools\build_store_receiver_zip.py" %*
set "RESULT=%ERRORLEVEL%"

popd
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b %RESULT%

:find_python
set "PYTHON=%REPO%backend\.venv\Scripts\python.exe"
if exist "%PYTHON%" exit /b 0
where py >nul 2>&1 && set "PYTHON=py -3" && exit /b 0
where python >nul 2>&1 && set "PYTHON=python" && exit /b 0
exit /b 1

:missing_python
echo.
echo   Python was not found on this computer.
echo.
echo   Building the Store Kit needs Python 3.11 or newer, plus PyInstaller
echo   from backend\requirements.txt and an ffmpeg.exe to ship.
echo.
popd
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b 2

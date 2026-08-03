@echo off
REM ===========================================================================
REM Restart the EchoCast HQ instance THIS folder started.
REM
REM Stops it, confirms it stopped, starts it again, waits for READY.
REM Never spawns a second copy, and changes no configuration or data.
REM 
REM
REM A THIN WRAPPER - the logic lives in tools\echocast_server.py.
REM ===========================================================================
setlocal

set "REPO=%~dp0"
pushd "%REPO%"

call :find_python
if errorlevel 1 goto :missing_python

"%PYTHON%" "%REPO%tools\echocast_server.py" restart
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
echo   EchoCast HQ needs Python 3.11 or newer. Install it from
echo   https://www.python.org/downloads/ and tick "Add python.exe to PATH".
echo.
popd
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b 2

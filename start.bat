@echo off
REM ===========================================================================
REM Start SpeakLink HQ from this folder.
REM
REM A THIN WRAPPER. Every decision - configuration, dependency bootstrap, PID
REM ownership, health checking - lives in tools\speaklink_server.py, so a Linux
REM or cloud host loses only the double-click, not the capability.
REM
REM Double-click this file, or run it from a terminal.
REM ===========================================================================
setlocal

REM The repository is wherever THIS FILE is, never the current directory. An
REM operator double-clicking from Explorer, and a shortcut with some other
REM working directory, must both find it.
set "REPO=%~dp0"
pushd "%REPO%"

call :find_python
if errorlevel 1 goto :missing_python

"%PYTHON%" "%REPO%tools\speaklink_server.py" start
set "RESULT=%ERRORLEVEL%"

popd
REM Only pause when launched by double-click, so a terminal or a script is not
REM left waiting for a keypress nobody is there to give.
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b %RESULT%

:find_python
REM The repo-local environment first: once it exists it is the one holding the
REM pinned requirements. Fall back to a system Python, which is what creates it.
set "PYTHON=%REPO%backend\.venv\Scripts\python.exe"
if exist "%PYTHON%" exit /b 0
where py >nul 2>&1 && set "PYTHON=py -3" && exit /b 0
where python >nul 2>&1 && set "PYTHON=python" && exit /b 0
exit /b 1

:missing_python
echo.
echo   Python was not found on this computer.
echo.
echo   SpeakLink HQ needs Python 3.11 or newer. Install it from
echo   https://www.python.org/downloads/ and tick "Add python.exe to PATH".
echo.
popd
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b 2

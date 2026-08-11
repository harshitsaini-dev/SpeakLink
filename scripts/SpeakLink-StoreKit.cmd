@echo off
setlocal

rem  SpeakLink Store Kit - the thing somebody at a till double-clicks.
rem
rem  WHY A .cmd AND NOT JUST THE .ps1
rem
rem  Double-clicking a .ps1 on a default Windows install opens it in Notepad.
rem  Right-click - Run with PowerShell exists, but it also runs under whatever
rem  execution policy the machine has, and a Store PC with a locked-down policy
rem  fails with a red wall of text that reads like the kit is broken. This
rem  launches PowerShell with the policy bypassed FOR THIS PROCESS ONLY, which
rem  changes nothing about the machine.
rem
rem  WHY A MENU
rem
rem  The script can decide install-or-upgrade by itself, and by default it does.
rem  Repair and Uninstall cannot be inferred - they are things a person decides
rem  after something has gone wrong - so they are offered here rather than
rem  hidden behind a parameter nobody at a till will ever type.

title SpeakLink Store Kit

set "SCRIPT=%~dp0SpeakLink-StoreKit.ps1"
if not exist "%SCRIPT%" (
  echo.
  echo   SpeakLink-StoreKit.ps1 was not found next to this file.
  echo   Unzip the whole Store Kit and run it from there, rather than copying
  echo   this one file out of it.
  echo.
  pause
  exit /b 1
)

:menu
cls
echo.
echo   SpeakLink Store Kit
echo   -------------------
echo.
echo   [1]  Install or upgrade    (keeps this Store enrolled)
echo   [2]  Repair                (rebuilds the runtime, keeps the credential)
echo   [3]  Uninstall             (keeps the credential, so it can come back)
echo   [4]  Uninstall and forget  (removes the credential too - re-enrolment needed)
echo   [5]  Check only            (changes nothing, shows what it would do)
echo   [Q]  Quit
echo.
set "choice="
set /p "choice=  Choose: "

if /i "%choice%"=="1" set ARGS=-Action Auto& goto run
if /i "%choice%"=="2" set ARGS=-Action Repair& goto run
if /i "%choice%"=="3" set ARGS=-Action Uninstall& goto run
if /i "%choice%"=="4" set ARGS=-Action Uninstall -RemoveCredential& goto confirmforget
if /i "%choice%"=="5" set ARGS=-Action Auto -DryRun& goto run
if /i "%choice%"=="Q" exit /b 0
goto menu

:confirmforget
echo.
echo   This removes the Device credential as well as the software.
echo   This Store would have to be enrolled again with a new code from HQ.
echo.
set "sure="
set /p "sure=  Type YES to continue: "
if /i not "%sure%"=="YES" goto menu

:run
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %ARGS%
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo   That did not finish cleanly ^(exit code %code%^).
  echo   Read the messages above, then send them to HQ if they do not explain it.
) else (
  echo   Done.
)
echo.
pause
goto menu

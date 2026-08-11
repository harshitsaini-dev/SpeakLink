@echo off
REM Project-local GitHub CLI.
REM
REM Windows has no directory-scoped environment variables, so "gh configured
REM per project" has to be a wrapper rather than a setting. Run ".\gh" from the
REM repository root and the CLI reads and writes .gh-config instead of
REM %APPDATA%\GitHub CLI, which keeps this project's token out of the account's
REM global login.
REM
REM GH_CONFIG_DIR alone is not enough: gh prefers the OS keyring, so a token
REM stored there would still be found and this wrapper would look like it had
REM worked while changing nothing. Log in with
REM     .\gh auth login --insecure-storage
REM so the token is written into .gh-config\hosts.yml, which is gitignored.
REM "insecure" here means a file with user-only permissions instead of the
REM Credential Manager - deliberate, because a per-project token is the point.

setlocal
set "GH_CONFIG_DIR=%~dp0.gh-config"
"C:\Program Files\GitHub CLI\gh.exe" %*
endlocal

<#
.SYNOPSIS
    Prepare the dedicated EchoCastService account and the EchoCast directories
    on the backend host. Run once, elevated.

.DESCRIPTION
    Implements the recorded architecture decision: one Windows server, one
    dedicated local account EchoCastService, DPAPI CURRENT_USER scope under that
    account, and the EchoCast tree under C:\ProgramData\EchoCast-AI.

    Least privilege, per directory:

      app    (RX)   the service runs the code and must not be able to rewrite
                    it. A service that can edit its own binaries turns any
                    code-execution bug into persistence.
      keys   (R,W)  read the key, rewrite it on rotation. Never Full Control,
                    which would let the running service edit its own ACL.
      data   (R,W)
      logs   (R,W)

    Inheritance is broken BEFORE any grant, in every directory. C:\ProgramData
    grants BUILTIN\Users by default, so granting first would leave a window in
    which the directory exists and every account on the host can read it.

    Every ACL is read back and judged by backend/key_custody_acl.py. Running
    icacls and printing "Done" proves nothing about the result.

.PARAMETER Credential
    The account password, supplied by you. Omit it and the script prompts.

    It is deliberately NOT generated: an account whose password nobody knows
    cannot be configured to log on, so the installer would leave something that
    looks finished and is not. The password is never printed, never written to
    disk, and an existing account's password is never reset - that would break a
    scheduled task already configured with the old one.

.PARAMETER WhatIfOnly
    Print the plan and change nothing. Works WITHOUT elevation: the one mode
    meant for looking must not require the rights meant for changing.

.NOTES
    This does NOT create the key container and does NOT configure a service or
    scheduled task.

    The container must be created by the backend running AS the service account,
    because DPAPI CURRENT_USER binds it to the identity that sealed it; one
    created by an administrator is one the service cannot open.

    Task/service configuration is deliberately left out until Log On As A
    Service rights and password custody are settled. For the initial pilot,
    Windows Task Scheduler is the simpler and safer of the two.

.EXAMPLE
    .\Install-EchoCastServiceIdentity.ps1 -WhatIfOnly

.EXAMPLE
    .\Install-EchoCastServiceIdentity.ps1
#>
[CmdletBinding()]
param(
    [string] $AccountName = 'EchoCastService',
    [string] $Root = 'C:\ProgramData\EchoCast-AI',
    [System.Management.Automation.PSCredential] $Credential,
    [switch] $WhatIfOnly
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

$qualified = "$env:COMPUTERNAME\$AccountName"

# An explicit array of objects, not an ordered hashtable. PowerShell's member
# enumeration turns $table.Keys on a dictionary of dictionaries into the inner
# keys, so $name became a hashtable and every path interpolated as empty - which
# would have run `icacls ""` under elevation. Caught by running -WhatIfOnly and
# reading the output.
$directories = @(
    [pscustomobject]@{ Name = 'app';  Path = (Join-Path $Root 'app');  Rights = 'RX';  Role = 'application' }
    [pscustomobject]@{ Name = 'keys'; Path = (Join-Path $Root 'keys'); Rights = 'R,W'; Role = 'keys' }
    [pscustomobject]@{ Name = 'data'; Path = (Join-Path $Root 'data'); Rights = 'R,W'; Role = 'data' }
    [pscustomobject]@{ Name = 'logs'; Path = (Join-Path $Root 'logs'); Rights = 'R,W'; Role = 'logs' }
)

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Elevated {
    if (-not (Test-Elevated)) {
        throw 'This script must run elevated. Creating a local account and setting an ACL both require administrator rights. Use -WhatIfOnly to see the plan without elevation.'
    }
}

function Test-EchoCastDirectoryAcl {
    <# Read the ACL back and judge it with the tested Python policy. #>
    param([string] $Path, [string] $Role)

    if (-not (Test-Path $venvPython)) {
        Write-Host "  (cannot verify $Role : no venv Python at $venvPython)"
        return $false
    }
    $check = @"
import sys, json
sys.path.insert(0, r'$repositoryRoot\backend')
from key_custody_acl import DirectoryRole, read_acl, verify_directory_acl
verdict = verify_directory_acl(
    read_acl(r'$Path'), role=DirectoryRole('$Role'), service_account=r'$qualified')
for problem in verdict.problems:
    print('    - ' + problem)
sys.exit(0 if verdict.acceptable else 3)
"@
    $check | & $venvPython -
    return ($LASTEXITCODE -eq 0)
}

# --- The plan, printable by anyone -----------------------------------------
Write-Output '=== EchoCast service identity and directories ==='
Write-Output ''
Write-Output "Account   : $qualified"
Write-Output "Root      : $Root"
Write-Output ''
Write-Output 'Planned steps:'
Write-Output "  - create local account $AccountName if it does not exist (password supplied by you)"
foreach ($entry in $directories) {
    Write-Output "  - create $($entry.Path)"
    Write-Output "    icacls `"$($entry.Path)`" /inheritance:r"
    Write-Output "    icacls `"$($entry.Path)`" /grant `"NT AUTHORITY\SYSTEM:(OI)(CI)(F)`""
    Write-Output "    icacls `"$($entry.Path)`" /grant `"BUILTIN\Administrators:(OI)(CI)(F)`""
    Write-Output "    icacls `"$($entry.Path)`" /grant `"${qualified}:(OI)(CI)($($entry.Rights))`""
    Write-Output "    verify the resulting ACL as role '$($entry.Role)'"
}
Write-Output ''
Write-Output 'Not done by this script:'
Write-Output '  - the key container (must be created by the backend AS the service account)'
Write-Output '  - any service or scheduled task (see .NOTES)'

if ($WhatIfOnly) {
    Write-Output ''
    Write-Output "WhatIfOnly: nothing was changed. Elevated: $(Test-Elevated)"
    exit 0
}

Assert-Elevated

# --- Account ---------------------------------------------------------------
Write-Output ''
$existing = Get-LocalUser -Name $AccountName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "  Account $AccountName already exists; its password is left untouched."
    Write-Output '  To change it deliberately:'
    Write-Output "    Set-LocalUser -Name $AccountName -Password (Read-Host 'New password' -AsSecureString)"
} else {
    if ($Credential) {
        $secret = $Credential.Password
    } else {
        Write-Output "  Creating $AccountName. Choose a password; it is never displayed or saved."
        $secret = Read-Host "  Password for $AccountName" -AsSecureString
    }
    if (-not $secret -or $secret.Length -eq 0) {
        throw 'No password was supplied. The account is not created, because one whose password nobody knows cannot be configured to log on.'
    }
    New-LocalUser -Name $AccountName -Password $secret `
        -Description 'EchoCast backend service identity. No interactive logon.' `
        -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    $secret.Dispose()
    Remove-Variable secret
    Write-Output "  Created $AccountName."
}

# --- Directories and ACLs --------------------------------------------------
$allVerified = $true
foreach ($entry in $directories) {
    $path = $entry.Path
    Write-Output ''
    Write-Output "  $($entry.Name) -> $path"
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Write-Output '    created'
    }

    & icacls "$path" /inheritance:r | Out-Null
    & icacls "$path" /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" | Out-Null
    & icacls "$path" /grant "BUILTIN\Administrators:(OI)(CI)(F)" | Out-Null
    & icacls "$path" /grant "${qualified}:(OI)(CI)($($entry.Rights))" | Out-Null

    if (Test-EchoCastDirectoryAcl -Path $path -Role $entry.Role) {
        Write-Output "    ACL verified as '$($entry.Role)'"
    } else {
        Write-Output "    ACL REJECTED for role '$($entry.Role)'"
        $allVerified = $false
    }
}

Write-Output ''
Write-Output 'Uninstall / disable:'
Write-Output "  Disable-LocalUser -Name $AccountName          # stop it logging on, keep the data"
Write-Output "  Remove-LocalUser  -Name $AccountName          # remove the account"
Write-Output "  # The directories under $Root are NOT removed by either command."
Write-Output "  # Back up $Root\keys before deleting anything: a lost key makes every"
Write-Output '  # stored Receiver credential unverifiable.'

Write-Output ''
if ($allVerified) {
    Write-Output 'RESULT: SERVICE_IDENTITY_PREPARED'
    Write-Output 'Next, as the service account: .\scripts\Test-EchoCastKeyCustody.ps1'
    exit 0
}
Write-Output 'RESULT: ACL_VERIFICATION_FAILED - inspect the problems listed above before continuing.'
exit 1

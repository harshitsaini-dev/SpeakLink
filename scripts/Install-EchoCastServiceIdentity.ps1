<#
.SYNOPSIS
    Create the dedicated EchoCastService account and lock down the Receiver HMAC
    key directory. Run once, elevated, on the backend host.

.DESCRIPTION
    Implements the recorded architecture decision:

      - one Windows server
      - one dedicated local account, EchoCastService
      - DPAPI CURRENT_USER scope under that account
      - key container at C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin
      - ACL: EchoCastService read/write, Administrators recovery, nobody else

    Inheritance is broken BEFORE any grant. C:\ProgramData grants BUILTIN\Users
    by default, so granting first would leave a window in which the key
    directory exists and every account on the host can read it.

    This script does NOT create the key container. It prepares the identity and
    the directory; the key itself is minted by the backend under the service
    account, so that CURRENT_USER scope binds to the right identity. Creating it
    as an administrator would produce a container the service cannot open.

.NOTES
    Requires elevation. Prints no secret. The account password is generated,
    never displayed, and never written to disk by this script.
#>
[CmdletBinding()]
param(
    [string] $AccountName = 'EchoCastService',
    [string] $KeyDirectory = 'C:\ProgramData\EchoCast-AI\keys',
    [switch] $WhatIfOnly
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must run elevated. Creating a local account and setting an ACL both require administrator rights.'
    }
}

Write-Output '=== EchoCast service identity and key directory ==='
Assert-Elevated

$keyFile = Join-Path $KeyDirectory 'receiver-hmac-keys.bin'
$qualified = "$env:COMPUTERNAME\$AccountName"

$steps = @(
    "Create local account $AccountName (no interactive logon, password never displayed)",
    "Create $KeyDirectory",
    "icacls `"$KeyDirectory`" /inheritance:r",
    "icacls `"$KeyDirectory`" /grant `"NT AUTHORITY\SYSTEM:(OI)(CI)(F)`"",
    "icacls `"$KeyDirectory`" /grant `"BUILTIN\Administrators:(OI)(CI)(F)`"",
    "icacls `"$KeyDirectory`" /grant `"${qualified}:(OI)(CI)(R,W)`""
)

Write-Output ''
Write-Output 'Planned steps:'
$steps | ForEach-Object { Write-Output "  - $_" }

if ($WhatIfOnly) {
    Write-Output ''
    Write-Output 'WhatIfOnly was supplied; nothing was changed.'
    exit 0
}

# --- Account ---------------------------------------------------------------
if (Get-LocalUser -Name $AccountName -ErrorAction SilentlyContinue) {
    Write-Output ''
    Write-Output "  Account $AccountName already exists; leaving it alone."
} else {
    Write-Output ''
    Write-Output "  Creating $AccountName..."
    # Generated here and never shown. The service is configured to run as this
    # account by the host's service/task configuration, which stores the
    # credential in the Windows credential store, not in a script or a file.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RNGCryptoServiceProvider]::new().GetBytes($bytes)
    $secret = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + '!aA1') -AsPlainText -Force
    New-LocalUser -Name $AccountName -Password $secret `
        -Description 'EchoCast backend service identity. No interactive logon.' `
        -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    Remove-Variable secret, bytes
    Write-Output "  Created. The password was generated and never displayed."
    Write-Output "  Set it deliberately with: Set-LocalUser -Name $AccountName -Password (Read-Host -AsSecureString)"
}

# --- Directory and ACL -----------------------------------------------------
if (-not (Test-Path $KeyDirectory)) {
    New-Item -ItemType Directory -Path $KeyDirectory -Force | Out-Null
    Write-Output "  Created $KeyDirectory"
}

Write-Output '  Breaking inheritance before granting anything...'
& icacls $KeyDirectory /inheritance:r | Out-Null
& icacls $KeyDirectory /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" | Out-Null
& icacls $KeyDirectory /grant "BUILTIN\Administrators:(OI)(CI)(F)" | Out-Null
& icacls $KeyDirectory /grant "${qualified}:(OI)(CI)(R,W)" | Out-Null

Write-Output ''
Write-Output 'Resulting ACL:'
& icacls $KeyDirectory

Write-Output ''
Write-Output 'Done. What has NOT happened yet:'
Write-Output "  - no key container exists at $keyFile"
Write-Output '  - the key must be created by the backend running AS this account,'
Write-Output '    because DPAPI CURRENT_USER binds the container to the identity'
Write-Output '    that sealed it. Creating it as an administrator would produce a'
Write-Output '    container the service cannot open.'
Write-Output ''
Write-Output 'Next, as EchoCastService:'
Write-Output '  .\scripts\Test-EchoCastKeyCustody.ps1'

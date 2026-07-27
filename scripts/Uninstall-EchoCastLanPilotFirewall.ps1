<#
.SYNOPSIS
    Remove the EchoCast LAN pilot firewall rules, and nothing else.

.DESCRIPTION
    Matches on the exact display names this pilot creates. It never removes a
    rule by port, because "any rule that mentions 8000" could be somebody else's
    and a firewall is not a thing to be approximate about.

    -WhatIf and -DryRun list what would go, without elevation.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RULE_PREFIX = 'EchoCast LAN Pilot'
$EXACT_NAMES = @(
    "$RULE_PREFIX - HQ dashboard (TCP 3000)",
    "$RULE_PREFIX - API (TCP 8000)"
)

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Output '=== removing EchoCast LAN pilot firewall rules ==='

$present = @()
foreach ($name in $EXACT_NAMES) {
    $rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    if ($rule) { $present += $rule; Write-Output "  found : $name" }
    else { Write-Output "  absent: $name" }
}

# Anything else wearing the prefix is reported, not removed. If a rule was
# renamed into this namespace by hand, deleting it silently would be worse than
# leaving it and saying so.
$strays = Get-NetFirewallRule -ErrorAction SilentlyContinue |
          Where-Object { $_.DisplayName -like "$RULE_PREFIX*" -and $_.DisplayName -notin $EXACT_NAMES }
foreach ($stray in $strays) {
    Write-Output "  NOT REMOVING (name is not one this script creates): $($stray.DisplayName)"
}

if (-not $present) {
    Write-Output ''
    Write-Output 'Nothing to remove.'
    exit 0
}

if ($DryRun -or $WhatIfPreference) {
    Write-Output ''
    Write-Output "Dry run: $($present.Count) rule(s) would be removed. Nothing was changed."
    exit 0
}

if (-not (Test-Elevated)) {
    throw 'Removing firewall rules needs an elevated PowerShell. Re-run as Administrator, or use -DryRun.'
}

foreach ($rule in $present) {
    if ($PSCmdlet.ShouldProcess($rule.DisplayName, 'Remove firewall rule')) {
        Remove-NetFirewallRule -DisplayName $rule.DisplayName
        Write-Output "  removed: $($rule.DisplayName)"
    }
}

$remaining = Get-NetFirewallRule -ErrorAction SilentlyContinue |
             Where-Object { $_.DisplayName -in $EXACT_NAMES }
Write-Output ''
Write-Output ("Pilot rules remaining: {0}" -f $remaining.Count)
exit $(if ($remaining.Count -eq 0) { 0 } else { 1 })

<#
.SYNOPSIS
    Open TCP 3000 and 8000 to the local subnet, on the Private profile only.

.DESCRIPTION
    Without these rules the second desktop cannot reach the pilot at all -
    Windows blocks inbound connections by default, which is the correct default
    and the reason this script has to be deliberate about undoing it.

    Two ports, Private profile only, remote scope LocalSubnet. Never the Public
    profile: a Public profile means Windows has been told this network is
    untrusted, and this pilot serves plain HTTP. Never "Any" remote address,
    because that is the difference between "the desktop in the next room" and
    "anything that can route to this machine".

    Nothing else is opened. No SMB, no RDP, no database port, no debugger.

    -WhatIf works without elevation and changes nothing, so the whole plan can
    be reviewed before anything is granted.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# One prefix, so Test and Uninstall can find exactly these and nothing else.
$RULE_PREFIX = 'SpeakLink LAN Pilot'
$RULES = @(
    @{ Name = "$RULE_PREFIX - HQ dashboard (TCP 3000)"; Port = 3000
       Description = 'SpeakLink private LAN pilot. HQ dashboard. Private profile, local subnet only. Remove with Uninstall-SpeakLinkLanPilotFirewall.ps1.' }
    @{ Name = "$RULE_PREFIX - API (TCP 8000)";          Port = 8000
       Description = 'SpeakLink private LAN pilot. FastAPI backend. Private profile, local subnet only. Remove with Uninstall-SpeakLinkLanPilotFirewall.ps1.' }
)

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Output '=== SpeakLink LAN pilot firewall rules ==='
Write-Output ''
Write-Output '  profile      : Private only'
Write-Output '  direction    : Inbound'
Write-Output '  protocol     : TCP'
Write-Output '  ports        : 3000, 8000'
Write-Output '  remote scope : LocalSubnet'
Write-Output '  action       : Allow'
Write-Output ''
foreach ($rule in $RULES) { Write-Output "  would ensure : $($rule.Name)" }
Write-Output ''

# Report what the profile actually is, because a Public profile makes this a
# refusal rather than a warning.
$profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
foreach ($item in $profiles) {
    Write-Output ("  network '{0}' is {1}" -f $item.Name, $item.NetworkCategory)
}
if ($profiles | Where-Object { $_.NetworkCategory -eq 'Public' -and $_.IPv4Connectivity -ne 'Disconnected' }) {
    Write-Output ''
    Write-Output '  NOTE: at least one connected network is Public. These rules apply to the'
    Write-Output '        Private profile only, so they will have no effect there - which is'
    Write-Output '        deliberate. Set the pilot network to Private before relying on this.'
}

if ($DryRun -or $WhatIfPreference) {
    Write-Output ''
    Write-Output 'Dry run: nothing was changed. No elevation required for this.'
    exit 0
}

if (-not (Test-Elevated)) {
    throw 'Creating firewall rules needs an elevated PowerShell. Re-run as Administrator, or use -DryRun to review the plan first.'
}

foreach ($rule in $RULES) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        # Idempotent: bring an existing rule back to the intended shape rather
        # than adding a second one beside it.
        if ($PSCmdlet.ShouldProcess($rule.Name, 'Update existing rule')) {
            Set-NetFirewallRule -DisplayName $rule.Name -Enabled True -Action Allow `
                -Profile Private -Direction Inbound -Description $rule.Description
            Set-NetFirewallRule -DisplayName $rule.Name -RemoteAddress LocalSubnet
            Get-NetFirewallRule -DisplayName $rule.Name | Get-NetFirewallPortFilter |
                Set-NetFirewallPortFilter -Protocol TCP -LocalPort $rule.Port
            Write-Output "  updated : $($rule.Name)"
        }
    } else {
        if ($PSCmdlet.ShouldProcess($rule.Name, 'Create rule')) {
            New-NetFirewallRule -DisplayName $rule.Name -Description $rule.Description `
                -Direction Inbound -Action Allow -Protocol TCP -LocalPort $rule.Port `
                -Profile Private -RemoteAddress LocalSubnet -Enabled True | Out-Null
            Write-Output "  created : $($rule.Name)"
        }
    }
}

Write-Output ''
Write-Output 'Done. Verify with .\scripts\Test-SpeakLinkLanPilotFirewall.ps1'
Write-Output 'Remove with  .\scripts\Uninstall-SpeakLinkLanPilotFirewall.ps1'

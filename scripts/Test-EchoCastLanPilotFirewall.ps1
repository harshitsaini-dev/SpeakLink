<#
.SYNOPSIS
    Verify the EchoCast LAN pilot firewall rules, and only those.

.DESCRIPTION
    Checks each rule is present exactly once, on the Private profile only, TCP,
    the right port, and scoped to LocalSubnet rather than Any.

    The last one is the check worth having. "Allow TCP 8000 inbound" and "allow
    TCP 8000 inbound from anywhere that can route here" look identical in a rule
    list and are not the same rule.

    Needs no elevation - reading firewall rules is not a privileged operation.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RULE_PREFIX = 'EchoCast LAN Pilot'
$EXPECTED = @{
    "$RULE_PREFIX - HQ dashboard (TCP 3000)" = 3000
    "$RULE_PREFIX - API (TCP 8000)"          = 8000
}

$results = [ordered]@{}
function Check { param([string]$Name, [scriptblock]$Test)
    try { $value = & $Test } catch { $value = $false }
    $results[$Name] = $value
    $mark = if ($value -eq $true) { 'PASS' } elseif ($value -eq $false) { 'FAIL' } else { "$value" }
    Write-Output ("  {0,-52} {1}" -f $Name, $mark)
}

Write-Output '=== EchoCast LAN pilot firewall verification ==='

# Get-NetFirewallRule needs elevation and answers "Access is denied" without it.
# The first version of this script treated that as "no rules installed" and told
# the operator to install rules that were already there - a wrong answer that
# sends somebody to change a firewall for no reason. netsh reads the same rules
# unelevated, so "cannot read" and "not installed" are now different outcomes.
$all = $null
$readable = $true
try {
    $all = Get-NetFirewallRule -ErrorAction Stop
} catch {
    $readable = $false
}

if (-not $readable) {
    Write-Output '  Get-NetFirewallRule is denied without elevation; reading through netsh instead.'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $names = @()
        foreach ($name in $EXPECTED.Keys) {
            $detail = netsh advfirewall firewall show rule name="$name" verbose 2>&1
            if (($detail -join ' ') -match 'No rules match') { continue }
            $names += $name
            # A plain function, not a scriptblock captured in a loop variable.
            # A Check block runs in the Check function's scope, so a value read
            # into a loop-local variable out here was not what the block saw -
            # which is how "correct local port" failed against a rule whose port
            # was demonstrably correct.
            function Get-RuleField {
                param([string[]]$Lines, [string]$Label)
                $line = $Lines | Where-Object { $_ -match "^$Label\s*:" } | Select-Object -First 1
                if (-not $line) { return '' }
                return ($line -replace "^$Label\s*:\s*", '').Trim()
            }
            $expectedPort = "$($EXPECTED[$name])"
            Write-Output ("  {0}" -f $name)
            Check "$expectedPort : enabled"                  { (Get-RuleField $detail 'Enabled') -eq 'Yes' }
            Check "$expectedPort : inbound allow"            { (Get-RuleField $detail 'Direction') -eq 'In' -and (Get-RuleField $detail 'Action') -eq 'Allow' }
            Check "$expectedPort : Private profile only"     { (Get-RuleField $detail 'Profiles') -eq 'Private' }
            Check "$expectedPort : TCP"                      { (Get-RuleField $detail 'Protocol') -eq 'TCP' }
            Check "$expectedPort : correct local port"       { (Get-RuleField $detail 'LocalPort') -eq $expectedPort }
            Check "$expectedPort : remote scope LocalSubnet" { (Get-RuleField $detail 'RemoteIP') -eq 'LocalSubnet' }
            Check "$expectedPort : remote scope is not Any"  { (Get-RuleField $detail 'RemoteIP') -ne 'Any' }
            Check "$expectedPort : description carries no secret" {
                -not ((Get-RuleField $detail 'Description') -match 'password|token|credential|secret|key|phc_')
            }
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    Write-Output ''
    $failed = @($results.GetEnumerator() | Where-Object { $_.Value -eq $false })
    if ($names.Count -eq 0) {
        Write-Output 'No EchoCast pilot firewall rules are installed.'
        Write-Output 'Install them with .\scripts\Install-EchoCastLanPilotFirewall.ps1 (elevated).'
        exit 2
    }
    if ($failed.Count -eq 0) {
        Write-Output 'Result: ECHOCAST_LAN_PILOT_FIREWALL_VERIFIED'
        Write-Output '(read through netsh; run elevated for the full rule-set comparison)'
        exit 0
    }
    Write-Output "Result: FAILED ($($failed.Count) check(s))"
    $failed | ForEach-Object { Write-Output "  - $($_.Key)" }
    exit 1
}

$ours = $all | Where-Object { $_.DisplayName -like "$RULE_PREFIX*" }
Write-Output ("  pilot rules found: {0}" -f $ours.Count)

foreach ($name in $EXPECTED.Keys) {
    $port = $EXPECTED[$name]
    $matching = @($ours | Where-Object { $_.DisplayName -eq $name })

    Check "$port : exactly one rule"        { $matching.Count -eq 1 }
    if ($matching.Count -ne 1) { continue }
    $rule = $matching[0]

    Check "$port : enabled"                 { $rule.Enabled -eq 'True' }
    Check "$port : inbound allow"           { $rule.Direction -eq 'Inbound' -and $rule.Action -eq 'Allow' }
    Check "$port : Private profile only"    { "$($rule.Profile)" -eq 'Private' }
    Check "$port : not on the Public profile" { "$($rule.Profile)" -notmatch 'Public' }

    $filter = $rule | Get-NetFirewallPortFilter
    Check "$port : TCP"                     { $filter.Protocol -eq 'TCP' }
    Check "$port : correct local port"      { "$($filter.LocalPort)" -eq "$port" }

    $scope = $rule | Get-NetFirewallAddressFilter
    Check "$port : remote scope LocalSubnet" { "$($scope.RemoteAddress)" -eq 'LocalSubnet' }
    Check "$port : remote scope is not Any"  { "$($scope.RemoteAddress)" -ne 'Any' }

    Check "$port : description carries no secret" {
        -not ($rule.Description -match 'password|token|credential|secret|key|phc_')
    }
}

Check 'no pilot rule opens a port we did not intend' {
    $ports = @()
    foreach ($rule in $ours) {
        $ports += ($rule | Get-NetFirewallPortFilter).LocalPort
    }
    $unexpected = @($ports | Where-Object { $_ -notin @('3000', '8000') })
    $unexpected.Count -eq 0
}

Check 'nothing else was renamed into the pilot prefix' {
    $ours.Count -le $EXPECTED.Count
}

Write-Output ''
$failed = @($results.GetEnumerator() | Where-Object { $_.Value -eq $false })
if ($ours.Count -eq 0) {
    Write-Output 'No EchoCast pilot firewall rules are installed.'
    Write-Output 'Install them with .\scripts\Install-EchoCastLanPilotFirewall.ps1 (elevated).'
    exit 2
}
if ($failed.Count -eq 0) {
    Write-Output 'Result: ECHOCAST_LAN_PILOT_FIREWALL_VERIFIED'
    exit 0
}
Write-Output "Result: FAILED ($($failed.Count) check(s))"
$failed | ForEach-Object { Write-Output "  - $($_.Key)" }
exit 1

<#
.SYNOPSIS
    Shared helper: stop the complete process tree a pilot owns, and nothing else.

.DESCRIPTION
    Dot-source this from a Stop-* script. It reads a scoped PID file, asks
    tools\process_tree.py which PIDs the recorded root actually owns, stops them
    deepest-first, waits, force-terminates only verified owned PIDs, and reports
    success only after every one of them is gone.

    The ownership decision lives in Python so it can be tested against a fixed
    process table (backend\tests\test_process_tree.py). The termination stays
    here so the destructive step is visible in the script the operator reads.

    It never stops a process by name, never touches a process whose command line
    does not prove ownership, and never removes a PID file until the tree is
    verified gone.

.NOTES
    Functions here report progress with Write-Host, NOT Write-Output, and that
    is deliberate. In PowerShell every uncaptured value a function emits joins
    its return value, so a Write-Output inside Stop-SpeakLinkProcessTree turns
    "return $false" into @("message", $false) - a non-empty array, which is
    always truthy. That silently defeats the survivor check this helper exists
    to enforce. It was caught by running the real stop path and seeing
    Test-SpeakLinkPortReleased report success for a port that was still bound.
    backend/tests/test_pilot_scripts.py pins it.
#>

function Read-SpeakLinkPidFile {
    <#
        Returns @{ ProcessId = <int>; CreatedAt = <string|$null> } or $null.

        Line 1 is the PID. Line 2, when present, is the process creation time
        recorded at launch; it is what lets us refuse a recycled PID. Files
        written before that was recorded hold only the number, and still work.
    #>
    param([Parameter(Mandatory)][string]$PidFile)

    if (-not (Test-Path $PidFile)) { return $null }

    $lines = @(Get-Content $PidFile -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) { return $null }

    $processId = 0
    if (-not [int]::TryParse($lines[0].Trim(), [ref]$processId) -or $processId -le 0) {
        return @{ ProcessId = 0; CreatedAt = $null }
    }

    $createdAt = $null
    if ($lines.Count -ge 2 -and -not [string]::IsNullOrWhiteSpace($lines[1])) {
        $createdAt = $lines[1].Trim()
    }
    return @{ ProcessId = $processId; CreatedAt = $createdAt }
}

function Write-SpeakLinkPidFile {
    <# Record the PID and its creation time so a recycled PID can be refused. #>
    param(
        [Parameter(Mandatory)][string]$PidFile,
        [Parameter(Mandatory)][int]$ProcessId
    )

    $created = ''
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -ne $process -and $null -ne $process.CreationDate) {
        $created = $process.CreationDate.ToUniversalTime().ToString('o')
    }
    Set-Content -Path $PidFile -Value @("$ProcessId", $created) -Encoding utf8
}

function Get-SpeakLinkProcessTable {
    <# One snapshot, shaped for tools\process_tree.py. #>
    Get-CimInstance Win32_Process | ForEach-Object {
        [pscustomobject]@{
            ProcessId       = $_.ProcessId
            ParentProcessId = $_.ParentProcessId
            Name            = $_.Name
            CommandLine     = $_.CommandLine
            CreationDate    = $(if ($null -ne $_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { $null })
        }
    } | ConvertTo-Json -Depth 3 -Compress
}

function Stop-SpeakLinkProcessTree {
    <#
        Stop the whole owned tree. Returns $true only when every owned PID is
        confirmed gone, so a caller must never print "stopped" on a $false.
    #>
    param(
        [Parameter(Mandatory)][string]$PidFile,
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string[]]$ExpectedCommandFragments,
        [Parameter(Mandatory)][string]$VenvPython,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [int]$GracefulTimeoutSeconds = 15
    )

    $record = Read-SpeakLinkPidFile -PidFile $PidFile
    if ($null -eq $record) {
        Write-Host "  $Label : no PID file, nothing to stop."
        return $true
    }
    if ($record.ProcessId -le 0) {
        Write-Host "  $Label : PID file is unreadable; removing the stale file."
        Remove-Item $PidFile -Force
        return $true
    }

    $processId = $record.ProcessId
    if ($null -eq (Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue)) {
        Write-Host "  $Label : process $processId is not running; removing the stale PID file."
        Remove-Item $PidFile -Force
        return $true
    }

    # --- Ask the tested planner what we actually own -----------------------
    $tool = Join-Path $RepositoryRoot 'tools\process_tree.py'
    $arguments = @("`"$tool`"", '--root', "$processId")
    foreach ($fragment in $ExpectedCommandFragments) { $arguments += @('--marker', "`"$fragment`"") }
    if ($record.CreatedAt) { $arguments += @('--created-at', "`"$($record.CreatedAt)`"") }

    $table = Get-SpeakLinkProcessTable
    $planOutput = $table | & $VenvPython @arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  $Label : ownership could NOT be proven for process $processId."
        Write-Host "         Refusing to stop it. Inspect it yourself, then remove $PidFile if it is stale."
        return $false
    }

    $ordered = @($planOutput | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
    if ($ordered.Count -eq 0) {
        Write-Host "  $Label : planner returned no PIDs; refusing to guess."
        return $false
    }

    Write-Host "  $Label : owns $($ordered.Count) process(es): $($ordered -join ', ')"
    Write-Host "  $Label : stopping deepest first, gracefully..."

    # --- Graceful first, children before parents ---------------------------
    foreach ($ownedPid in $ordered) {
        try { Stop-Process -Id $ownedPid -ErrorAction Stop } catch { }
    }

    $deadline = (Get-Date).AddSeconds($GracefulTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $alive = @($ordered | Where-Object { $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
        if ($alive.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }

    # --- Force only PIDs we already proved we own --------------------------
    $stubborn = @($ordered | Where-Object { $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($stubborn.Count -gt 0) {
        Write-Host "  $Label : still running after $GracefulTimeoutSeconds s: $($stubborn -join ', '). Force-terminating these verified owned PIDs."
        foreach ($ownedPid in $stubborn) {
            Stop-Process -Id $ownedPid -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }

    # --- Verify before claiming anything -----------------------------------
    $survivors = @($ordered | Where-Object { $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($survivors.Count -gt 0) {
        Write-Host "  $Label : NOT fully stopped. Still alive: $($survivors -join ', ')."
        Write-Host "         Keeping $PidFile so the survivors stay traceable."
        return $false
    }

    Remove-Item $PidFile -Force
    Write-Host "  $Label : stopped (whole tree verified gone, PID file removed)."
    return $true
}

function Test-SpeakLinkPortReleased {
    <# A port still bound after a stop means something survived. #>
    param([Parameter(Mandatory)][int]$Port)

    $listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq $Port }
    if ($null -eq $listener) {
        Write-Host "  port $Port : released."
        return $true
    }
    Write-Host "  port $Port : STILL BOUND by process $($listener.OwningProcess)."
    return $false
}

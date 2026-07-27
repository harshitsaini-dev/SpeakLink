<#
.SYNOPSIS
    Stop a private LAN pilot, and only the processes it started.

.DESCRIPTION
    Reads the manifest the Start script wrote and stops exactly those process
    trees. It never stops a process because it happens to be Python, Node or
    FFmpeg - a developer's editor, language server or unrelated dev server are
    all of those, and killing one of them to tidy up a pilot is the kind of help
    nobody asks for twice.

    Logs, the manifest and the temporary database are left in place. A pilot that
    deleted its own evidence when it stopped would be useless the one time
    somebody needed to know what happened.

    The protected database is never touched.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)]
    [string]$PilotRoot,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$manifestPath = Join-Path $PilotRoot 'pilot-processes.json'
if (-not (Test-Path $manifestPath)) { throw "No pilot manifest at $manifestPath." }
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json

Write-Output '=== stopping the EchoCast private LAN pilot ==='
Write-Output "  pilot root : $PilotRoot"
Write-Output "  owned PIDs : $($manifest.backend_pid), $($manifest.frontend_pid)"

# Everything descended from an owned PID: yarn spawns node, uvicorn may spawn
# ffmpeg. Walking the tree from a PID we recorded is what keeps this precise.
function Get-OwnedTree {
    param([int]$RootPid)
    $collected = New-Object System.Collections.Generic.List[int]
    $pending = New-Object System.Collections.Generic.Queue[int]
    $pending.Enqueue($RootPid)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        if ($collected -contains $current) { continue }
        if (-not (Get-Process -Id $current -ErrorAction SilentlyContinue)) { continue }
        $collected.Add($current) | Out-Null
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$current" -ErrorAction SilentlyContinue |
            ForEach-Object { $pending.Enqueue([int]$_.ProcessId) }
    }
    return $collected
}

$owned = @()
foreach ($rootPid in @($manifest.backend_pid, $manifest.frontend_pid)) {
    if ($rootPid) { $owned += Get-OwnedTree -RootPid ([int]$rootPid) }
}
$owned = $owned | Select-Object -Unique

if (-not $owned) {
    Write-Output '  nothing from this pilot is still running'
} else {
    foreach ($processId in $owned) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        $name = if ($proc) { $proc.Name } else { 'gone' }
        if ($DryRun -or -not $PSCmdlet.ShouldProcess("PID $processId ($name)", 'Stop')) {
            Write-Output "  would stop PID $processId ($name)"
        } else {
            Write-Output "  stopping PID $processId ($name)"
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($DryRun) {
    Write-Output ''
    Write-Output 'Dry run: nothing was stopped.'
    exit 0
}

Start-Sleep -Seconds 2
Write-Output '--- ports ---'
$allFree = $true
foreach ($port in @(3000, 8000)) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $free = -not $listener
    if (-not $free) { $allFree = $false }
    Write-Output ("  port {0}: {1}" -f $port, $(if ($free) { 'free' } else { "STILL IN USE by PID $($listener[0].OwningProcess)" }))
}

# The credentials this session exported. Cleared from THIS shell only; the child
# processes that inherited them are gone.
foreach ($name in 'ADMIN_PASSWORD','JWT_SECRET','ADMIN_USERNAME','ECHOCAST_DB_PATH',
                  'ECHOCAST_KEY_CONTAINER','ECHOCAST_KEY_PROTECTOR','CORS_ORIGINS',
                  'REACT_APP_BACKEND_URL','HOST','PORT') {
    if (Test-Path "env:$name") { Set-Item "env:$name" '' }
}
Write-Output '  session secrets cleared from this shell'

$protected = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend\echocast_live.db'
if (Test-Path $protected) {
    $item = Get-Item $protected
    Write-Output ("  protected DB: {0} bytes, wal={1}, shm={2}" -f $item.Length,
        (Test-Path "$protected-wal"), (Test-Path "$protected-shm"))
}

Write-Output ''
Write-Output "Logs, manifest and the temporary database are kept at:"
Write-Output "  $PilotRoot"
exit $(if ($allFree) { 0 } else { 1 })

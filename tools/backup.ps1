<#
.SYNOPSIS
    Copy the irreplaceable data to the Nikhef cluster. See docs/operating/backup.md.

.DESCRIPTION
    Run nightly from Task Scheduler, as localadmin:

        .\tools\backup.ps1              # copy what changed since the last run
        .\tools\backup.ps1 -Full        # copy everything, ignoring the stamp
        .\tools\backup.ps1 -WhatIf      # list what would be copied, send nothing

    WHAT IS COPIED, AND WHY ONLY THIS.

    Only what cannot be reconstructed:

      data/raw/         the measurement archive. THE truth (DESIGN.md section 9).
      data/events/      flight-recorder dumps: the ten minutes before an alarm.
      data/quarantine/  data deliberately set aside; small, and not reproducible.
      data/fm101_total.json   the integrator's running total.

    Deliberately NOT copied:

      data/imported/    556 MB reconstructed from Desktop\SC_DATA, which still
                        exists. Re-importable, so it is bloat here.
      PostgreSQL        accepted as expendable on 18 Sep 2026: `meas` replays
                        from the archive, and the alarm history is not worth a
                        schedule.
      config/secrets.yaml   credentials do not go on shared storage.
      logs/             useful for a week, worthless after.
      The NI driver     a one-off upload, not this job's business.

    WHY TAR OVER SSH RATHER THAN scp PER FILE.

    `data/events/` is a hundred small files and grows by a few per alarm. A
    hundred scp invocations means a hundred SSH handshakes through a jump host;
    one tar stream means one. It also preserves the directory layout without
    creating it by hand at the far end.

    INCREMENTAL, BY MODIFICATION TIME.

    `data/raw/` is append-only and date-partitioned: today's file grows,
    yesterday's never changes again. So "everything modified since the last
    successful run" is exactly right, and today's file is re-sent each night
    until the date rolls over - a few MB, not worth being clever about.

    The stamp is written ONLY after a verified success, so a failed run leaves
    the next one to pick up the same work rather than skipping it.
#>

[CmdletBinding()]
param(
    [switch]$Full,
    [switch]$WhatIf,
    [string]$Target = "nikhef-backup",
    [string]$RemoteDir = "/data/xenon/xams_slow_control/archive",
    # Warn below this: the volume is SHARED xenon-group storage and was 95%
    # full when this was set up. A backup that stops because somebody else
    # filled the disk is the classic version of this going wrong.
    [int]$MinFreeGB = 100
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Stamp    = Join-Path $RepoRoot "data\.backup-stamp"
$LogFile  = Join-Path $RepoRoot "logs\backup.log"
$Mosquitto = "$env:ProgramFiles\mosquitto\mosquitto_pub.exe"

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

function Log ($msg, $level = "INFO") {
    $line = "{0} {1,-7} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

# Published retained, so anything that wants to know can ask the broker rather
# than read a file on this machine (DESIGN.md section 2.1). The web UI and xams-ctl
# both show it.
function Publish-Status ($ok, $detail, $bytes = 0, $files = 0) {
    if (-not (Test-Path $Mosquitto)) { return }
    $payload = @{
        t       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        ok      = [bool]$ok
        detail  = [string]$detail
        bytes   = [int64]$bytes
        files   = [int]$files
        target  = "$Target`:$RemoteDir"
    } | ConvertTo-Json -Compress

    # SENT FROM A FILE, NOT ON THE COMMAND LINE.
    #
    # `-m "$payload"` looks right and silently corrupts the message: Windows
    # argument parsing strips the double quotes out of the JSON, so the broker
    # received {detail:2 file(s), 14,5 MB,bytes:...} and every reader of it
    # failed to parse. -f bypasses argument parsing entirely.
    #
    # Written UTF-8 with NO BOM: a BOM at the start of the payload makes it
    # invalid JSON just as surely as the missing quotes did.
    $payloadFile = Join-Path $env:TEMP "xams-backup-status.json"
    try {
        [System.IO.File]::WriteAllText($payloadFile, $payload,
                                       (New-Object System.Text.UTF8Encoding($false)))
        & $Mosquitto -h 127.0.0.1 -p 1883 -t "xams/backup/status" -r -f $payloadFile
        if ($LASTEXITCODE -ne 0) {
            Log "mosquitto_pub returned $LASTEXITCODE; status not published" "WARN"
        }
    } catch {
        Log "could not publish backup status to MQTT: $($_.Exception.Message)" "WARN"
    } finally {
        Remove-Item $payloadFile -Force -ErrorAction SilentlyContinue
    }
}

function Fail ($msg, $files = 0) {
    Log $msg "FAIL"
    Publish-Status $false $msg 0 $files
    exit 1
}

Log "backup starting (target $Target`:$RemoteDir)"

# ---------------------------------------------------------------- what to send

$since = [DateTime]::MinValue
if (-not $Full -and (Test-Path $Stamp)) {
    try   { $since = [DateTime]::Parse((Get-Content $Stamp -Raw).Trim()) }
    catch { Log "unreadable stamp; copying everything" "WARN" }
}
Log ("copying files modified since {0}" -f $(if ($since -eq [DateTime]::MinValue) { "the beginning" } else { $since.ToString("u") }))

$sources = @("data\raw", "data\events", "data\quarantine")
$files = foreach ($s in $sources) {
    $p = Join-Path $RepoRoot $s
    if (Test-Path $p) { Get-ChildItem $p -File -Recurse | Where-Object { $_.LastWriteTime -gt $since } }
}
# Tiny, and it is the integrator's entire memory. Always sent.
$total = Join-Path $RepoRoot "data\fm101_total.json"
if (Test-Path $total) { $files = @($files) + @(Get-Item $total) }

$files = @($files | Where-Object { $_ })
if ($files.Count -eq 0) {
    Log "nothing has changed since the last run"
    Publish-Status $true "nothing to copy" 0 0
    exit 0
}

$bytes = ($files | Measure-Object Length -Sum).Sum
Log ("{0} file(s), {1:N1} MB" -f $files.Count, ($bytes / 1MB))

# Paths relative to the repo root, so the layout is reproduced at the far end.
$relative = $files | ForEach-Object { $_.FullName.Substring($RepoRoot.Length + 1).Replace("\", "/") }

if ($WhatIf) {
    Log "-WhatIf: nothing will be sent. Files:"
    $relative | ForEach-Object { Log "    $_" }
    exit 0
}

# ---------------------------------------------------------------- room at the far end

$freeGB = $null
try {
    $df = & ssh -o BatchMode=yes -o ConnectTimeout=20 $Target "df -PBG $RemoteDir | tail -1 | awk '{print `$4}' | tr -d 'G'" 2>&1
    if ($LASTEXITCODE -eq 0) { $freeGB = [int]($df | Select-Object -Last 1) }
} catch { }

if ($null -eq $freeGB) {
    Log "could not read free space at the destination; continuing anyway" "WARN"
} elseif ($freeGB -lt $MinFreeGB) {
    Fail "only ${freeGB} GB free at $RemoteDir (want at least ${MinFreeGB} GB). Nothing was copied." $files.Count
} else {
    Log "${freeGB} GB free at the destination"
}

# ---------------------------------------------------------------- send

$listFile = [System.IO.Path]::GetTempFileName()
# tar reads the file list as UTF-8 with no BOM; Set-Content's default encoding
# would put one in and tar would look for a file whose name begins with it.
[System.IO.File]::WriteAllLines($listFile, $relative, (New-Object System.Text.UTF8Encoding($false)))

# STAGED THROUGH A FILE, NOT PIPED.
#
# `tar -cf - ... | ssh ...` is the obvious way to write this and it does NOT
# work in Windows PowerShell 5.1: a pipeline between two NATIVE commands is
# re-encoded as text, which corrupts the byte stream. The far end reports
# "This does not look like a tar archive" and the cause is invisible from
# either end. Staging to a file and sending it with scp keeps the bytes
# intact, at the cost of temporary space equal to the changed data - a few MB
# on an ordinary night.

$tarPath = Join-Path $env:TEMP ("xams-backup-{0}.tar" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

$ErrorActionPreference = "Continue"     # native commands report by exit code
try {
    Log "packing $($files.Count) file(s) ..."
    & tar.exe -cf $tarPath -C $RepoRoot -T $listFile
    if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = "Stop"; Fail "tar failed (exit $LASTEXITCODE). Nothing was sent." $files.Count }

    $tarMB = (Get-Item $tarPath).Length / 1MB
    Log ("sending {0:N1} MB to $Target ..." -f $tarMB)
    & scp -o BatchMode=yes -o ConnectTimeout=30 $tarPath "${Target}:${RemoteDir}/.incoming.tar"
    $sendExit = $LASTEXITCODE
    if ($sendExit -ne 0) { $ErrorActionPreference = "Stop"; Fail "scp failed (exit $sendExit). The stamp was NOT advanced." $files.Count }

    # Unpacked and removed in one connection. The staging file goes whether or
    # not the extract succeeded, so a failed run leaves no part-file behind to
    # be mistaken for data later.
    & ssh -o BatchMode=yes -o ConnectTimeout=30 $Target "cd '$RemoteDir' && tar -xf .incoming.tar; rc=`$?; rm -f .incoming.tar; exit `$rc"
    $sshExit = $LASTEXITCODE
} finally {
    Remove-Item $listFile -Force -ErrorAction SilentlyContinue
    $ErrorActionPreference = "Stop"
}

if ($sshExit -ne 0) { Fail "unpacking failed at the destination (exit $sshExit). The stamp was NOT advanced." $files.Count }

# ---------------------------------------------------------------- prove it arrived
#
# An exit code of 0 says the pipe closed cleanly, not that the bytes are on the
# far disk. Comparing the total size of what was sent against what is now there
# is cheap and catches a truncated stream, which is the failure that would
# otherwise be discovered during a restore.

# Compared against the sizes RECORDED IN THE TAR, not against the sizes
# measured before packing.
#
# `data/raw/<today>.jsonl` is being appended to while this runs, so the file
# tar read is already bigger than the file Get-ChildItem measured a moment
# earlier. Comparing the destination against that earlier number reports a
# mismatch on every single night, for a backup that is perfectly correct - a
# check that cries wolf nightly is worse than no check, because it is the one
# people learn to ignore.
#
# The tar's own listing is what was actually sent, so that is what the
# destination has to match.

$sentBytes = $null
try {
    $listing = & tar.exe -tvf $tarPath 2>$null
    if ($LASTEXITCODE -eq 0) {
        $sentBytes = ($listing | ForEach-Object {
            # -rw-rw-rw-  0 0  0  149 sep 18 09:46 data/fm101_total.json
            if ($_ -match '^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s') { [int64]$Matches[1] }
        } | Measure-Object -Sum).Sum
    }
} catch { }

$remoteBytes = $null
try {
    $quoted = ($relative | ForEach-Object { "'$_'" }) -join " "
    $out = & ssh -o BatchMode=yes -o ConnectTimeout=30 $Target "cd '$RemoteDir' && du -cb $quoted 2>/dev/null | tail -1 | cut -f1" 2>&1
    if ($LASTEXITCODE -eq 0) { $remoteBytes = [int64]($out | Select-Object -Last 1) }
} catch { }

if ($null -eq $remoteBytes -or $null -eq $sentBytes) {
    Log "could not verify the copy at the destination" "WARN"
} elseif ($remoteBytes -ne $sentBytes) {
    Fail "size mismatch: packed $sentBytes bytes, found $remoteBytes at the destination. The stamp was NOT advanced." $files.Count
} else {
    Log ("verified: {0:N0} bytes at the destination" -f $remoteBytes)
}

# ---------------------------------------------------------------- record success

# Written only now. A run that failed anywhere above leaves the stamp where it
# was, so the next run repeats the same work instead of stepping over it.
Remove-Item $tarPath -Force -ErrorAction SilentlyContinue
(Get-Date).ToString("o") | Set-Content -Path $Stamp -Encoding utf8

$msg = "{0} file(s), {1:N1} MB" -f $files.Count, ($bytes / 1MB)
Log "backup complete - $msg"
Publish-Status $true $msg $bytes $files.Count
exit 0

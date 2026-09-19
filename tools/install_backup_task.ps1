<#
.SYNOPSIS
    Register the nightly backup as a Windows Scheduled Task.

.DESCRIPTION
        .\tools\install_backup_task.ps1              # register (or update) it
        .\tools\install_backup_task.ps1 -RunNow      # register, then run it once
        .\tools\install_backup_task.ps1 -Uninstall   # remove it

    A SCHEDULED TASK, NOT AN NSSM SERVICE. This matters and it is not a
    preference.

    The XAMS services run as LocalSystem. LocalSystem has its own profile at
    C:\Windows\System32\config\systemprofile, so it would look for the SSH key
    in ...\systemprofile\.ssh, not find it, and fail with "Permission denied
    (publickey)" - an error identical to a genuinely broken key, from a command
    that works perfectly when you run it by hand. A task running as localadmin
    reads C:\Users\localadmin\.ssh, which is where the key actually is.

    LOGON TYPE. Registered S4U: it runs whether or not anyone is logged in, and
    stores no password. S4U tasks get the user profile but no Windows network
    credentials - which is fine here, because SSH public-key authentication
    needs the profile (for ~/.ssh) and not a Windows credential.

    Verify that claim rather than trusting it: -RunNow runs the task and shows
    what logs\backup.log says afterwards. A backup nobody has watched run is a
    hypothesis.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$RunNow,
    [string]$TaskName = "XAMS nightly backup",
    [string]$At = "03:30"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Script   = Join-Path $PSScriptRoot "backup.ps1"
$LogFile  = Join-Path $RepoRoot "logs\backup.log"

function Say  ($m) { Write-Host "  $m" }
function Good ($m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Fail ($m) { Write-Host "  FAIL $m" -ForegroundColor Red }

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Good "removed '$TaskName'"
    } else {
        Say "'$TaskName' is not registered"
    }
    exit 0
}

if (-not (Test-Path $Script)) { Fail "no backup.ps1 beside this script"; exit 1 }

# Registering an S4U task - one that runs with nobody logged in - needs admin.
# Checked here so the failure is one clear sentence rather than a CIM
# "Access is denied" from three cmdlets in a row.
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal $identity
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "This script must run elevated."
    Say  "A task that runs with nobody logged in is registered with the S4U"
    Say  "logon type, and Windows requires administrator rights for that."
    Say  ""
    Say  "Open PowerShell as Administrator, then:"
    Say  "    cd $RepoRoot"
    Say  "    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force"
    Say  "    .\tools\install_backup_task.ps1 -RunNow"
    exit 1
}

# -ExecutionPolicy Bypass for THIS PROCESS only. The machine policy is
# Restricted and stays that way: relaxing it permanently to run one scheduled
# script would be a change to the machine's security posture, made for
# convenience, that nobody would remember having made.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# StartWhenAvailable: if the PC was off at 03:30, run at the next opportunity
# rather than skipping the night entirely.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

# The account comes from the token, not from $env:USERDOMAIN. On this
# domain-joined PC that variable reads "ad" while localadmin is a *local*
# account, so "$env:USERDOMAIN\$env:USERNAME" is "ad\localadmin", which does
# not resolve and makes Register-ScheduledTask fail. GetCurrent().Name is
# whatever Windows will actually accept.
$RunAs = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$principal = New-ScheduledTaskPrincipal -UserId $RunAs `
    -LogonType S4U -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
} catch {
    Fail "could not register the task: $($_.Exception.Message)"
    exit 1
}

# ASKED FOR, NOT ASSUMED. Register-ScheduledTask can report a CIM error that
# does not terminate the script, and an earlier version of this printed "OK
# registered" immediately after Windows had refused with "Access is denied".
# Reporting success for work that did not happen is the failure mode this whole
# system is written to avoid; it should not start here.
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Fail "Windows did not register '$TaskName', despite reporting no error."
    exit 1
}
Good "registered '$TaskName', daily at $At, as $RunAs"

Say ""
Say "It runs:  powershell -File $Script"
Say "Logs to:  $LogFile"
Say "Status:   published retained on MQTT as xams/backup/status,"
Say "          and shown by xams-ctl status and the web UI overview."

if ($RunNow) {
    Say ""
    Say "running it once now ..."
    $before = if (Test-Path $LogFile) { (Get-Item $LogFile).Length } else { 0 }
    Start-ScheduledTask -TaskName $TaskName
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
        $running = (Get-ScheduledTask -TaskName $TaskName).State -eq "Running"
    } while ($running -and (Get-Date) -lt $deadline)

    Say ""
    Say ("last run result: 0x{0:X} (0 means success)" -f $info.LastTaskResult)
    if (Test-Path $LogFile) {
        Say "what it wrote:"
        Get-Content $LogFile | Select-Object -Skip ([int]($before / 80)) |
            Select-Object -Last 12 | ForEach-Object { Say "    $_" }
    }
    if ($info.LastTaskResult -ne 0) {
        Fail "the task did not succeed. The likeliest cause is the SSH key:"
        Say  "check that `ssh nikhef-backup hostname` works as THIS user."
        exit 1
    }
    Good "the scheduled task ran and succeeded"
}
exit 0

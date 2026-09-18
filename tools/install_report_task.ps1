<#
.SYNOPSIS
    Register the daily report email as a Windows Scheduled Task.

.DESCRIPTION
        .\tools\install_report_task.ps1              # register (or update) it
        .\tools\install_report_task.ps1 -RunNow      # register, then send one now
        .\tools\install_report_task.ps1 -Uninstall   # remove it

    Sibling of install_backup_task.ps1, and the same reasoning applies: a
    Scheduled Task running as localadmin, not an NSSM service running as
    LocalSystem.

    NOT PART OF THE ALARM SERVICE, deliberately. A daily summary is a
    convenience; the alarm engine is not. Scheduling inside the engine would
    put a timer, an SMTP connection and a template renderer in the one process
    that must keep evaluating readings - and an SMTP server that hangs would
    then hang the alarms. Separate process, separate failure.

    The default time is 07:30: before the working day, after the night's
    backup at 03:30, so the report can tell you whether the backup ran.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$RunNow,
    [string]$TaskName = "XAMS daily report",
    [string]$At = "07:30"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"

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

if (-not (Test-Path $Python)) { Fail "no virtual environment at $Python"; exit 1 }

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal $identity
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "This script must run elevated."
    Say  "A task that runs with nobody logged in uses the S4U logon type, and"
    Say  "Windows requires administrator rights to register one."
    Say  ""
    Say  "Open PowerShell as Administrator, then:"
    Say  "    cd $RepoRoot"
    Say  "    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force"
    Say  "    .\tools\install_report_task.ps1 -RunNow"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m xams_sc.alarms.daily" -WorkingDirectory $RepoRoot

$trigger  = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
} catch {
    Fail "could not register the task: $($_.Exception.Message)"
    exit 1
}

# Asked for, not assumed: Register-ScheduledTask can report a CIM error that
# does not stop the script, and announcing success after Windows refused is
# exactly the failure this system is written to avoid.
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Fail "Windows did not register '$TaskName', despite reporting no error."
    exit 1
}
Good "registered '$TaskName', daily at $At, as $env:USERDOMAIN\$env:USERNAME"

Say ""
Say "It runs:      $Python -m xams_sc.alarms.daily"
Say "Recipients:   config\recipients.yaml (those with enabled: true)"
Say "Preview it:   .\.venv\Scripts\python.exe -m xams_sc.alarms.daily --preview report.html"
Say "Send to one:  .\.venv\Scripts\python.exe -m xams_sc.alarms.daily --to you@nikhef.nl"

if ($RunNow) {
    Say ""
    Say "sending one now, to everyone in recipients.yaml ..."
    Start-ScheduledTask -TaskName $TaskName
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Seconds 4
        $state = (Get-ScheduledTask -TaskName $TaskName).State
    } while ($state -eq "Running" -and (Get-Date) -lt $deadline)

    $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
    Say ("last run result: 0x{0:X} (0 means every recipient got it)" -f $info.LastTaskResult)
    if ($info.LastTaskResult -ne 0) {
        Fail "the report did not go to everyone. Run it by hand to see why:"
        Say  "    .\.venv\Scripts\python.exe -m xams_sc.alarms.daily"
        exit 1
    }
    Good "the scheduled task ran and the report was delivered"
}
exit 0

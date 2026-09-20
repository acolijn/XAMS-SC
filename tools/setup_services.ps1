<#
.SYNOPSIS
    Installs and configures Mosquitto, PostgreSQL and Grafana for XAMS slow control.

.DESCRIPTION
    Run this ONCE, in an elevated PowerShell, from the repository root:

        .\tools\setup_services.ps1

    It is idempotent: anything already installed or already configured is left
    alone, so it is safe to re-run after fixing one step.

    What it does:
      1. Downloads the three installers into tools/installers/ (skipped if present)
      2. Installs each one silently (skipped if already installed)
      3. Binds ALL THREE TO LOOPBACK — see the note below
      4. Creates the xams database and role, applies sql/schema.sql
      5. Writes config/secrets.yaml (gitignored) with the database password
      6. Verifies every port is listening on 127.0.0.1 and nowhere else

    WHY THE LOOPBACK BINDING MATTERS. Commands travel over MQTT (xams/cmd/#),
    so anything that can reach the broker can set a high voltage. A broker
    listening on 0.0.0.0 — which is what most tutorials show — is an
    unauthenticated HV control interface on the building network. See
    docs/DESIGN.md §8.

    This script does NOT install the NSSM services for the XAMS Python
    services themselves. That belongs to the trial phase (§12), and auto-start
    stays off while LabVIEW is still the fallback: every device admits only
    one process, so a service that starts at boot would lock LabVIEW out.

.PARAMETER SkipDownload
    Use installers already staged in tools/installers/ and never reach the network.

.PARAMETER PostgresVersion
    EDB installer version. If the download 404s, fetch the installer by hand
    from https://www.enterprisedb.com/software-downloads-postgres into
    tools/installers/ and re-run with -SkipDownload.
#>

[CmdletBinding()]
param(
    [switch]$SkipDownload,
    [string]$MosquittoVersion = "2.1.2",
    [string]$PostgresVersion  = "18.6-1",
    [string]$GrafanaVersion   = "13.2.2",
    [string]$GrafanaMsi       = "grafana_13.2.2_34846740809_windows_amd64.msi"
)

$ErrorActionPreference = "Stop"

$RepoRoot    = Split-Path -Parent $PSScriptRoot
$StagingDir  = Join-Path $PSScriptRoot "installers"
$Changed     = New-Object System.Collections.ArrayList
$Skipped     = New-Object System.Collections.ArrayList

# PowerShell 5.1's -Encoding utf8 writes a BOM, and a BOM on the first line of
# mosquitto.conf or postgresql.conf breaks the parser. Always write config
# files through this.
function Write-ConfigFile($path, $text) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($path, $text, $utf8NoBom)
}

function Say    ($m) { Write-Host "  $m" }
function Step   ($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Good   ($m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn   ($m) { Write-Host "  WARN $m" -ForegroundColor Yellow }
function Fail   ($m) { Write-Host "  FAIL $m" -ForegroundColor Red }
function Note   ($m) { [void]$Changed.Add($m) }
function Kept   ($m) { [void]$Skipped.Add($m) }

# ---------------------------------------------------------------- preconditions

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal $identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "This script must run elevated."
    Say  "Open PowerShell as Administrator, cd to $RepoRoot, and run it again."
    exit 1
}

if (-not (Test-Path (Join-Path $RepoRoot "sql\schema.sql"))) {
    Fail "sql\schema.sql not found. Run this from the repository root."
    exit 1
}

New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null

Write-Host "XAMS slow control - service setup" -ForegroundColor White
Say "repository: $RepoRoot"
Say "staging:    $StagingDir"

# ---------------------------------------------------------------- passwords

Step "Credentials"

Say "PostgreSQL needs two passwords: one for the 'postgres' superuser and one"
Say "for the 'xams' role this system connects as. If PostgreSQL is already"
Say "installed, give its existing superuser password."
Write-Host ""

$pgSuperSecure = Read-Host "  postgres superuser password" -AsSecureString
$xamsPwSecure  = Read-Host "  password for the new 'xams' role" -AsSecureString

function Plain($secure) {
    [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$PgSuperPassword = Plain $pgSuperSecure
$XamsPassword    = Plain $xamsPwSecure

if ([string]::IsNullOrWhiteSpace($PgSuperPassword) -or
    [string]::IsNullOrWhiteSpace($XamsPassword)) {
    Fail "Both passwords are required."
    exit 1
}

# ---------------------------------------------------------------- download

function Get-Installer($name, $url, $file) {
    $path = Join-Path $StagingDir $file
    if (Test-Path $path) {
        Good "$name installer already staged"
        return $path
    }
    if ($SkipDownload) {
        Fail "$name installer missing and -SkipDownload was given: $path"
        Say  "Download it from: $url"
        exit 1
    }
    Say "downloading $name ..."
    try {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $url -OutFile $path -UseBasicParsing -TimeoutSec 600
    } catch {
        Fail "could not download $name"
        Say  "URL: $url"
        Say  "Fetch it by hand into $StagingDir, then re-run with -SkipDownload."
        exit 1
    }
    # A 404 page saved as an .exe is the classic silent failure here.
    if ((Get-Item $path).Length -lt 1MB) {
        Remove-Item $path -Force
        Fail "$name download was too small to be an installer (probably a 404 page)."
        Say  "URL: $url"
        Say  "Fetch it by hand into $StagingDir, then re-run with -SkipDownload."
        exit 1
    }
    Good "$name downloaded"
    return $path
}

Step "Installers"

$mosqUrl  = "https://mosquitto.org/files/binary/win64/mosquitto-$MosquittoVersion-install-windows-x64.exe"
$mosqFile = "mosquitto-$MosquittoVersion-install-windows-x64.exe"
$pgUrl    = "https://get.enterprisedb.com/postgresql/postgresql-$PostgresVersion-windows-x64.exe"
$pgFile   = "postgresql-$PostgresVersion-windows-x64.exe"
$grafUrl  = "https://dl.grafana.com/grafana/release/$GrafanaVersion/$GrafanaMsi"

$mosqPath = $null; $pgPath = $null; $grafPath = $null

$mosqInstalled = Test-Path "$env:ProgramFiles\mosquitto\mosquitto.exe"
$pgInstalled   = (Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue) -ne $null
$grafInstalled = (Get-Service -Name "Grafana" -ErrorAction SilentlyContinue) -ne $null

if (-not $mosqInstalled) { $mosqPath = Get-Installer "Mosquitto"  $mosqUrl $mosqFile }  else { Good "Mosquitto already installed" }
if (-not $pgInstalled)   { $pgPath   = Get-Installer "PostgreSQL" $pgUrl   $pgFile }    else { Good "PostgreSQL already installed" }
if (-not $grafInstalled) { $grafPath = Get-Installer "Grafana"    $grafUrl $GrafanaMsi } else { Good "Grafana already installed" }

# ---------------------------------------------------------------- 1. Mosquitto

Step "1. Mosquitto (MQTT broker)"

if (-not $mosqInstalled) {
    Say "installing silently ..."
    Start-Process -FilePath $mosqPath -ArgumentList "/S" -Wait -NoNewWindow
    Start-Sleep -Seconds 3
    if (-not (Test-Path "$env:ProgramFiles\mosquitto\mosquitto.exe")) {
        Fail "Mosquitto did not install. Run $mosqPath by hand and re-run this script."
        exit 1
    }
    Note "installed Mosquitto $MosquittoVersion"
} else {
    Kept "Mosquitto (already installed)"
}

$mosqConf = "$env:ProgramFiles\mosquitto\mosquitto.conf"
$mosqDesired = @"
# XAMS slow control. Managed by tools/setup_services.ps1 - see docs/DESIGN.md section 8.
#
# LOOPBACK ONLY, DELIBERATELY. Control commands travel over this broker
# (xams/cmd/#), so a listener on 0.0.0.0 is an unauthenticated high-voltage
# control interface on the building network. Do not change this without
# recording the decision: the answer to wanting remote access is an SSH
# tunnel or a read-only mirror, never opening this port.

listener 1883 127.0.0.1
allow_anonymous true

persistence true
persistence_location C:\ProgramData\mosquitto\
log_dest file C:\ProgramData\mosquitto\mosquitto.log
log_type warning
log_type error
"@

$needsWrite = $true
if (Test-Path -LiteralPath $mosqConf -PathType Leaf) {
    $existing = [System.IO.File]::ReadAllText($mosqConf)
    if ($existing -eq $mosqDesired) { $needsWrite = $false }
    else {
        $backup = "$mosqConf.before-xams"
        if (-not (Test-Path $backup)) {
            Copy-Item $mosqConf $backup
            Say "existing config backed up to $backup"
        }
    }
}

if ($needsWrite) {
    New-Item -ItemType Directory -Force -Path "C:\ProgramData\mosquitto" | Out-Null
    Write-ConfigFile $mosqConf $mosqDesired
    Note "wrote $mosqConf (listener 1883 on 127.0.0.1)"
    Good "bound to 127.0.0.1:1883"
} else {
    Kept "mosquitto.conf (already correct)"
    Good "bound to 127.0.0.1:1883"
}

if (-not (Get-Service -Name mosquitto -ErrorAction SilentlyContinue)) {
    & "$env:ProgramFiles\mosquitto\mosquitto.exe" install | Out-Null
    Note "registered the mosquitto Windows service"
}
Restart-Service -Name mosquitto -ErrorAction SilentlyContinue
Set-Service -Name mosquitto -StartupType Automatic
Good "mosquitto service running"

# ---------------------------------------------------------------- 2. PostgreSQL

Step "2. PostgreSQL"

if (-not $pgInstalled) {
    Say "installing silently (this takes a few minutes) ..."
    $pgArgs = @(
        "--mode", "unattended",
        "--unattendedmodeui", "minimal",
        "--superpassword", $PgSuperPassword,
        "--serverport", "5432",
        "--disable-components", "stackbuilder,pgAdmin"
    )
    Start-Process -FilePath $pgPath -ArgumentList $pgArgs -Wait -NoNewWindow
    Start-Sleep -Seconds 5
    Note "installed PostgreSQL $PostgresVersion"
} else {
    Kept "PostgreSQL (already installed)"
}

$psql = Get-ChildItem "$env:ProgramFiles\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
if (-not $psql) {
    Fail "psql.exe not found under $env:ProgramFiles\PostgreSQL"
    exit 1
}
Good "using $($psql.FullName)"

$pgDataDir = Join-Path (Split-Path (Split-Path $psql.FullName)) "data"
$pgConf    = Join-Path $pgDataDir "postgresql.conf"
if (Test-Path $pgConf) {
    $conf = Get-Content $pgConf -Raw
    if ($conf -notmatch "(?m)^\s*listen_addresses\s*=\s*'localhost'") {
        Copy-Item $pgConf "$pgConf.before-xams" -ErrorAction SilentlyContinue
        if ($conf -match "(?m)^\s*#?\s*listen_addresses\s*=.*$") {
            $conf = $conf -replace "(?m)^\s*#?\s*listen_addresses\s*=.*$", "listen_addresses = 'localhost'"
        } else {
            $conf += "`nlisten_addresses = 'localhost'`n"
        }
        Write-ConfigFile $pgConf $conf
        Note "set listen_addresses = 'localhost' in postgresql.conf"
        Restart-Service -Name "postgresql*" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 4
    } else {
        Kept "postgresql.conf listen_addresses (already localhost)"
    }
    Good "bound to localhost:5432"
}

$env:PGPASSWORD = $PgSuperPassword

# Run psql and judge it by its EXIT CODE ONLY.
#
# Two PowerShell 5.1 traps live here, and both bit this script once:
#
#  * `2>&1` on a native executable wraps each stderr line in an ErrorRecord.
#    With $ErrorActionPreference = "Stop" that becomes a TERMINATING error
#    even when the program succeeded.
#  * psql writes NOTICE to stderr. "relation meas already exists, skipping"
#    is the schema behaving exactly as intended - schema.sql is written with
#    CREATE TABLE IF NOT EXISTS so that re-running is safe - and it must not
#    be mistaken for a failure.
#
# So: stderr goes to a file, the preference is relaxed around the call, and
# only $LASTEXITCODE decides.
function Invoke-Psql {
    param(
        [string]$Database,
        [string[]]$PsqlArgs,
        [string]$AsUser = "postgres"
    )
    $errFile = [System.IO.Path]::GetTempFileName()
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $stdout = & $psql.FullName -U $AsUser -h 127.0.0.1 -d $Database @PsqlArgs 2>$errFile
        $code   = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    $stderr = ""
    if (Test-Path -LiteralPath $errFile) {
        $stderr = (Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue)
        Remove-Item -LiteralPath $errFile -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{
        Output   = ($stdout -join "`n").Trim()
        Stderr   = $stderr
        ExitCode = $code
    }
}

function Psql($database, $sql) {
    $r = Invoke-Psql -Database $database -PsqlArgs @("-t", "-A", "-c", $sql)
    if ($r.ExitCode -ne 0) { throw "psql failed: $($r.Stderr)$($r.Output)" }
    return $r.Output
}

try {
    $dbExists = (Psql "postgres" "SELECT 1 FROM pg_database WHERE datname='xams'") -match "1"
    if (-not $dbExists) {
        Psql "postgres" "CREATE DATABASE xams" | Out-Null
        Note "created database 'xams'"
    } else { Kept "database 'xams' (already exists)" }

    $roleExists = (Psql "postgres" "SELECT 1 FROM pg_roles WHERE rolname='xams'") -match "1"
    $escaped = $XamsPassword.Replace("'", "''")
    if (-not $roleExists) {
        Psql "postgres" "CREATE USER xams WITH PASSWORD '$escaped'" | Out-Null
        Note "created role 'xams'"
    } else {
        Psql "postgres" "ALTER USER xams WITH PASSWORD '$escaped'" | Out-Null
        Note "reset the password for the existing role 'xams'"
    }

    # Idempotent: schema.sql is written with CREATE TABLE IF NOT EXISTS.
    $schema = Join-Path $RepoRoot "sql\schema.sql"
    $r = Invoke-Psql -Database "xams" -PsqlArgs @("-v", "ON_ERROR_STOP=1", "-f", $schema)
    if ($r.ExitCode -ne 0) {
        throw "applying schema.sql failed: $($r.Stderr)"
    }
    # NOTICE lines here are expected and mean the schema was already applied.
    if ($r.Stderr -match "already exists, skipping") {
        Say "schema was already applied; existing tables left untouched"
    }
    Note "applied sql/schema.sql"

    Psql "xams" "GRANT ALL ON ALL TABLES IN SCHEMA public TO xams" | Out-Null
    Psql "xams" "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO xams" | Out-Null
    Psql "xams" "GRANT USAGE, CREATE ON SCHEMA public TO xams" | Out-Null
    Note "granted table and sequence privileges to 'xams'"

    # OWNERSHIP, not just privileges.
    #
    # GRANT ALL lets the role read and write. It does NOT let it create an
    # index or alter a table - those require ownership. Without this, every
    # future schema change needs the postgres superuser password, and
    # re-running sql/schema.sql as the application role fails with
    # "must be owner of table". Found the hard way on 17 September 2026.
    $owned = Psql "xams" "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    foreach ($t in ($owned -split "`n" | Where-Object { $_.Trim() })) {
        Psql "xams" "ALTER TABLE public.$($t.Trim()) OWNER TO xams" | Out-Null
    }
    Psql "xams" "ALTER SCHEMA public OWNER TO xams" | Out-Null
    Note "transferred table ownership to 'xams' so it can manage its own schema"

    # Re-apply the schema as the owner now, so any index the first pass could
    # not create is created on this run rather than the next one.
    $r2 = Invoke-Psql -Database "xams" -PsqlArgs @("-v", "ON_ERROR_STOP=1", "-f", $schema)
    if ($r2.ExitCode -ne 0) { throw "re-applying schema.sql as owner failed: $($r2.Stderr)" }

    $tables = Psql "xams" "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
    Good "database ready: $($tables.Trim()) tables"
} catch {
    Fail $_.Exception.Message
    Say "PostgreSQL configuration did not complete. Fix the above and re-run."
    exit 1
} finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 3. Grafana

Step "3. Grafana"

if (-not $grafInstalled) {
    Say "installing silently ..."
    Start-Process -FilePath "msiexec.exe" `
        -ArgumentList "/i", "`"$grafPath`"", "/qn", "/norestart" -Wait -NoNewWindow
    Start-Sleep -Seconds 5
    Note "installed Grafana $GrafanaVersion"
} else {
    Kept "Grafana (already installed)"
}

# Grafana installs to a fixed path. An earlier version of this script tried to
# discover it with Get-ChildItem, which returns the folder's CONTENTS, not the
# folder - so $customIni became an ARRAY of paths, Test-Path returned an array
# of booleans, -not on a non-empty array is $false, the -or never
# short-circuited, and Get-Content was handed the array. Use the path directly.
$grafHome = Join-Path $env:ProgramFiles "GrafanaLabs\grafana"
if (-not (Test-Path -LiteralPath $grafHome -PathType Container)) {
    Fail "Grafana not found at $grafHome"
    Say  "Install it by hand from $grafPath and re-run this script."
    exit 1
}
Good "Grafana home: $grafHome"

$customIni = Join-Path $grafHome "conf\custom.ini"
$iniDesired = @"
# XAMS slow control. Managed by tools/setup_services.ps1 - see docs/DESIGN.md section 8.
# Loopback only: this is a monitoring surface on the lab PC, not a web service.

[server]
http_addr = 127.0.0.1
http_port = 3000

[users]
allow_sign_up = false

[analytics]
reporting_enabled = false
check_for_updates = false

[paths]
provisioning = $RepoRoot\grafana\provisioning

# Lets the P&ID mimic embed a panel in a popup instead of only linking out to
# it (DESIGN.md section 8.2a). Anonymous access is acceptable only because of
# the loopback bind above: it grants read access to every dashboard to anyone
# who can reach this machine, and anyone who can reach it can already open
# Grafana directly. If this ever binds beyond 127.0.0.1, reconsider both
# settings in the same breath.
[security]
allow_embedding = true

[auth.anonymous]
enabled = true
org_role = Viewer
"@

$iniCurrent = $null
if (Test-Path -LiteralPath $customIni -PathType Leaf) {
    $iniCurrent = [System.IO.File]::ReadAllText($customIni)
}
if ($iniCurrent -ne $iniDesired) {
    if ($null -ne $iniCurrent) {
        Copy-Item -LiteralPath $customIni "$customIni.before-xams" -ErrorAction SilentlyContinue
    }
    Write-ConfigFile $customIni $iniDesired
    Note "wrote $customIni (loopback, provisioning from the repository)"
} else {
    Kept "Grafana custom.ini (already correct)"
}

# The datasource password reaches Grafana through the environment, never
# through a file in the repository (§11).
[Environment]::SetEnvironmentVariable("XAMS_PG_PASSWORD", $XamsPassword, "Machine")
Note "set XAMS_PG_PASSWORD (machine environment, for the Grafana service)"

Restart-Service -Name Grafana -ErrorAction SilentlyContinue
Set-Service -Name Grafana -StartupType Automatic
Start-Sleep -Seconds 5
Good "grafana service running on 127.0.0.1:3000"

# ---------------------------------------------------------------- 4. secrets

Step "4. config/secrets.yaml"

$secretsPath = Join-Path $RepoRoot "config\secrets.yaml"
if (Test-Path $secretsPath) {
    Warn "config/secrets.yaml already exists - left untouched."
    Say  "If the database password changed, update the postgres: block by hand."
    Kept "config/secrets.yaml (already present)"
} else {
    $secrets = @"
# Credentials. NEVER COMMITTED - this file is in .gitignore (DESIGN.md section 11).
# Written by tools/setup_services.ps1.

sms:
  provider: ""           # from the existing SC_software send_sms script
  api_key: ""
  api_url: ""

email:
  smtp_host: ""
  smtp_port: 587
  username: ""
  password: ""
  from_address: ""

postgres:
  host: "127.0.0.1"
  port: 5432
  database: "xams"
  user: "xams"
  password: "$XamsPassword"
"@
    Write-ConfigFile $secretsPath $secrets
    Note "wrote config/secrets.yaml"
    Good "database credentials in place"
}

# ---------------------------------------------------------------- 5. verify

Step "5. Verification"

$allGood = $true

function Check-Port($label, $port) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if (-not $conns) {
        Fail "$label is not listening on port $port"
        return $false
    }
    $exposed = $conns | Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }
    if ($exposed) {
        Fail "$label is listening on $($exposed.LocalAddress -join ', ') - NOT loopback only"
        Say  "This exposes it to the building network. Fix before going further."
        return $false
    }
    Good "$label listening on 127.0.0.1:$port only"
    return $true
}

if (-not (Check-Port "Mosquitto"  1883)) { $allGood = $false }
if (-not (Check-Port "PostgreSQL" 5432)) { $allGood = $false }
if (-not (Check-Port "Grafana"    3000)) { $allGood = $false }

# Prove the xams role can actually connect - a grant that looks right and
# does not work is the failure this catches.
$env:PGPASSWORD = $XamsPassword
$probe = Invoke-Psql -Database "xams" -AsUser "xams" `
                     -PsqlArgs @("-t", "-A", "-c", "SELECT count(*) FROM meas")
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
if ($probe.ExitCode -eq 0) {
    Good "role 'xams' can query meas ($($probe.Output) rows)"
} else {
    Fail "role 'xams' cannot query meas: $($probe.Stderr)"
    $allGood = $false
}

# ---------------------------------------------------------------- summary

Step "Summary"

if ($Changed.Count -gt 0) {
    Write-Host "  Changed:" -ForegroundColor White
    $Changed | ForEach-Object { Say "  - $_" }
}
if ($Skipped.Count -gt 0) {
    Write-Host "`n  Already in place:" -ForegroundColor White
    $Skipped | ForEach-Object { Say "  - $_" }
}

Write-Host ""
if ($allGood) {
    Good "All three services are installed, configured and loopback-only."
    Write-Host ""
    Say "Next, from an ORDINARY (non-elevated) prompt in $RepoRoot :"
    Say ""
    Say "  .\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl start"
    Say "  .\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status"
    Say ""
    Say "Then open http://127.0.0.1:3000 - the XAMS Overview dashboard is"
    Say "provisioned from grafana/dashboards/ and should start filling."
    exit 0
} else {
    Fail "Setup finished with problems - see the FAIL lines above."
    Say  "The script is idempotent: fix the cause and run it again."
    exit 1
}

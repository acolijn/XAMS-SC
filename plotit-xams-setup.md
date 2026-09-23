# Setting up plotit-xams.nikhef.nl

**Goal:** PostgreSQL and Grafana on the VM `plotit-xams.nikhef.nl`, fed by the
lab PC, so the group can see the plots in a browser and there is an outside
watchdog on the lab PC. This carries out DESIGN.md §12 and
[nikhef-vm-request.md](nikhef-vm-request.md).

```
  lab PC (Windows)                               plotit-xams (Linux)
  ────────────────                               ───────────────────
  MQTT ─┬─► pg_writer  → local PostgreSQL        (unchanged)
        ├─► jsonl_writer → data/raw/             (unchanged, the truth)
        └─► pg_writer "nikhef-vm" ── TLS 5432 ──► PostgreSQL (full history)
                                                      │  127.0.0.1
                                                   Grafana (127.0.0.1:3000)
                                                      │
  colleagues' browsers ─────── HTTPS 443 ────────►  nginx
```

Three rules hold throughout:

1. **Every connection goes out from the lab PC.** Nothing on the VM ever
   connects to the lab PC. MQTT stays bound to `127.0.0.1`.
2. **The lab PC does not depend on the VM.** If the VM is down, only the
   remote writer notices. It buffers the rows and writes them when the VM is
   back.
3. **The VM holds no unique data.** It can always be rebuilt from the JSONL
   archive.

Status: connectivity from the lab PC to `plotit-xams.nikhef.nl` has been checked ✔

> **Shortcut: `tools/vm/install_vm.sh` does steps 1–5, 8 and 9 in one go**
> (the VM is Ubuntu 24.04, so use the appendix commands where they differ).
> It is safe to rerun, remembers its settings in `/etc/xams-vm/vm.env`, and
> generates the passwords into `/etc/xams-vm/credentials`:
>
> ```bash
> ssh plotit-xams
> sudo git clone https://github.com/acolijn/XAMS-SC.git /opt/xams-sc
> sudo /opt/xams-sc/tools/vm/install_vm.sh
> # later, once known:
> sudo LABPC_IP=<ip> WATCHDOG_EMAIL=<addr> SMTP_HOST=<relay:port> /opt/xams-sc/tools/vm/install_vm.sh
> ```
>
> The steps below explain what it does, and are the manual route.

---

## Step 0: Find out before starting

| # | Question | Why it matters |
|---|---|---|
| 0.1 | Which OS? (`cat /etc/os-release`) | The commands below are for **Alma/Rocky 9**. The Debian/Ubuntu equivalents are in the [appendix](#appendix-debian--ubuntu-equivalents) |
| 0.2 | Do I have sudo? | Otherwise the Nikhef CT group installs the packages |
| 0.3 | The lab PC's **fixed IP** (`ipconfig` on Windows) | The firewall and `pg_hba.conf` let only this address in |
| 0.4 | Has port **5432/tcp** been opened from the lab PC to the VM? | The earlier connection test may only have covered ping or ssh |
| 0.5 | Is port **443/tcp** open to the Nikhef network or VPN? | Browser access |
| 0.6 | The SMTP relay host and port | Watchdog email |
| 0.7 | TLS certificate for the web front end: from Nikhef, or self-signed? | Needed for nginx |
| 0.8 | Access policy: Nikhef SSO/LDAP, or anonymous Viewer restricted to the Nikhef network? | Sets up the Grafana auth section |

Test port 5432 from the lab PC (in PowerShell) **after step 2**:

```powershell
Test-NetConnection plotit-xams.nikhef.nl -Port 5432
```

---

## Step 1: Prepare the base VM

```bash
sudo dnf -y update
sudo dnf -y install git firewalld policycoreutils-python-utils
sudo systemctl enable --now firewalld

# Repository, used for sql/schema.sql, the Grafana provisioning and the dashboards
sudo git clone <repo-url> /opt/xams-sc
sudo chown -R $USER: /opt/xams-sc
```

Firewall. Only the lab PC may reach 5432, and everyone reaches 443:

```bash
LABPC=<lab PC IP>
sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$LABPC/32 port port=5432 protocol=tcp accept"
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
sudo firewall-cmd --list-all
```

Port 3000 (Grafana) is **not** opened. Only nginx can reach Grafana.

---

## Step 2: Install PostgreSQL with TLS

The lab PC runs PostgreSQL 18, so use the same major version (from PGDG):

```bash
sudo dnf -y install https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm
sudo dnf -qy module disable postgresql
sudo dnf -y install postgresql18-server
sudo /usr/pgsql-18/bin/postgresql-18-setup initdb
```

### 2a. Server certificate

Self-signed is enough. The lab PC pins exactly this certificate:

```bash
cd /var/lib/pgsql/18/data
sudo -u postgres openssl req -new -x509 -days 3650 -nodes \
  -subj "/CN=plotit-xams.nikhef.nl" \
  -addext "subjectAltName=DNS:plotit-xams.nikhef.nl" \
  -keyout server.key -out server.crt
sudo chmod 600 server.key
```

Copy `server.crt` to the lab PC as
`C:\...\XAMS SC\config\plotit-xams-ca.crt`. It is a public certificate, but it
is host-specific, so keep it out of git just like `secrets.yaml`.

### 2b. `postgresql.conf`

```ini
listen_addresses = 'localhost,<VM IP>'
ssl = on
ssl_cert_file = 'server.crt'
ssl_key_file  = 'server.key'
password_encryption = scram-sha-256
```

### 2c. `pg_hba.conf`

Replace the host rules with these:

```
# TYPE    DB    USER         ADDRESS            METHOD
local     all   postgres                        peer
host      xams  xams_reader  127.0.0.1/32       scram-sha-256
host      xams  xams_admin   127.0.0.1/32       scram-sha-256
hostssl   xams  xams_writer  <lab PC IP>/32     scram-sha-256
```

`hostssl` means the writer **cannot** connect without TLS.

```bash
sudo systemctl enable --now postgresql-18
```

---

## Step 3: Database, schema and roles

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE xams_admin  LOGIN PASSWORD '<admin pw>';
CREATE ROLE xams_writer LOGIN PASSWORD '<writer pw>';
CREATE ROLE xams_reader LOGIN PASSWORD '<reader pw>';
CREATE DATABASE xams OWNER xams_admin;
SQL

# The schema is created by the owner, so the unique index gets built
# (on 17 Sept a permissions problem made that fail; see pg_writer.py)
psql "host=127.0.0.1 dbname=xams user=xams_admin" -f /opt/xams-sc/sql/schema.sql

psql "host=127.0.0.1 dbname=xams user=xams_admin" <<'SQL'
-- writer: INSERT only, on the two tables the VM receives
GRANT USAGE ON SCHEMA public TO xams_writer, xams_reader;
GRANT INSERT ON meas, alarm_events TO xams_writer;
-- reader: SELECT only
GRANT SELECT ON ALL TABLES IN SCHEMA public TO xams_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO xams_reader;
SQL
```

`ON CONFLICT DO NOTHING` needs only INSERT rights, so the writer does not need
UPDATE. `flow_periods` (which uses UPDATE) and `audit` (who did what, when)
**stay local only**. The VM has no use for them.

**Disk.** Steady load is about five rows a second (channels are logged as
aggregates, not at 1 Hz), about 100 MB/day, so 250 GB lasts years. Check once
on the lab PC that this is still true:

```sql
SELECT count(*) FROM meas WHERE t > now() - interval '1 hour';   -- ~18 000
```

---

## Step 4: Install Grafana

```bash
sudo tee /etc/yum.repos.d/grafana.repo <<'EOF'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
EOF
sudo dnf -y install grafana
```

### 4a. `/etc/grafana/grafana.ini`

```ini
[server]
http_addr = 127.0.0.1
http_port = 3000
domain    = plotit-xams.nikhef.nl
root_url  = https://plotit-xams.nikhef.nl/

[security]
admin_user = admin
cookie_secure = true

[users]
allow_sign_up = false

# Option A: anonymous viewers (only if 443 is reachable from the Nikhef network/VPN only)
[auth.anonymous]
enabled  = true
org_role = Viewer

# Option B: Nikhef LDAP. Take the settings from CT; see /etc/grafana/ldap.toml
# [auth.ldap]
# enabled = true
# config_file = /etc/grafana/ldap.toml

[smtp]
enabled      = true
host         = <smtp relay>:25
from_address = xams-watchdog@nikhef.nl
```

### 4b. Datasource

The repo file `grafana/provisioning/datasources/xams.yaml` connects as `xams`.
On the VM the reader role is `xams_reader`, so the VM uses
`grafana/provisioning-vm/datasources/xams.yaml` (already in the repo). It is
the same file with `user: xams_reader` and the same `uid: xams-postgres`, so
the dashboards work unchanged.

```bash
sudo ln -s /opt/xams-sc/grafana/provisioning-vm/datasources/xams.yaml \
           /etc/grafana/provisioning/datasources/xams.yaml

# the password goes in as an environment variable, not in git
sudo systemctl edit grafana-server
#   [Service]
#   Environment=XAMS_PG_PASSWORD=<reader pw>

sudo systemctl enable --now grafana-server
curl -s http://127.0.0.1:3000/api/health
```

---

## Step 5: Put nginx in front of it with HTTPS

```bash
sudo dnf -y install nginx
sudo setsebool -P httpd_can_network_connect 1   # SELinux: nginx → 127.0.0.1:3000
```

`/etc/nginx/conf.d/grafana.conf`:

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
    listen 80;
    server_name plotit-xams.nikhef.nl;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name plotit-xams.nikhef.nl;

    ssl_certificate     /etc/pki/tls/certs/plotit-xams.crt;
    ssl_certificate_key /etc/pki/tls/private/plotit-xams.key;

    location / {
        proxy_set_header Host $host;
        proxy_pass http://127.0.0.1:3000;
    }
    location /api/live/ {
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_pass http://127.0.0.1:3000;
    }
}
```

```bash
sudo nginx -t && sudo systemctl enable --now nginx
```

Test: open <https://plotit-xams.nikhef.nl> from a Nikhef machine. You should
see the Grafana login page, or the home page if anonymous access is on. Log in
as `admin`/`admin` and **change the password at once**.

---

## Step 6: Lab PC, the second writer ✔ built

This is the "something that writes into the database". It is in the code and
**does nothing until `secrets.yaml` has a `postgres_remote:` block**, so it can
be deployed to the lab PC before the VM exists.

| What | Where |
|---|---|
| Second `PgWriter` (label `nikhef-vm`) + `AlarmWriter` when `postgres_remote` is set | `src/xams_sc/sinks/__main__.py` |
| `RemoteHealth`: VM trouble is a **WARNING** in `logs/sinks.log` naming the `--since` to replay. Never an ERROR, and never sets the sinks service to `degraded` | same |
| `--no-remote` switch, to run without the VM for a while | same |
| Replay the archive into any database, safe to run twice | `tools/replay_jsonl.py` |
| Template of the block | `config/secrets.example.yaml` |
| Tests | `tests/test_remote_sink.py` |

Only `meas` and `alarm_events` go to the VM. `flow_periods` (needs UPDATE)
and `audit` stay local.

**Buffer:** the remote writer holds up to 300 000 rows in memory. At about five
rows a second that is roughly **16 hours** of VM outage. After that it drops
rows. They are still in the JSONL archive, and the log line says which
replay command gets them onto the VM. Alarm transitions that happen during an
outage are **not** buffered: `alarm_events` on the VM will miss them (they are
still in the local database).

### 6a. Try it before the VM exists (optional)

Use a second database on the lab PC's own PostgreSQL as a stand-in:

```powershell
psql -U postgres -c "CREATE DATABASE xams_vm_test"
psql -U postgres -c "CREATE ROLE xams_writer LOGIN PASSWORD 'test'"
psql -U postgres -d xams_vm_test -f sql\schema.sql
psql -U postgres -d xams_vm_test -c "GRANT INSERT ON meas, alarm_events TO xams_writer"
```

```yaml
postgres_remote:            # temporary, in config/secrets.yaml
  host: "127.0.0.1"
  database: "xams_vm_test"
  user: "xams_writer"
  password: "test"
```

`xams-ctl restart`. Rows should appear in `xams_vm_test`. Then
`Stop-Service postgresql-x64-18` for a few minutes: that stops **both**
databases, so it tests catching up, not independence. Afterwards run
`tools\replay_jsonl.py --target postgres_remote --since <today>`, which should
report 0 new. Remove the block (or `DROP DATABASE xams_vm_test`) when done.

### 6b. `config/secrets.yaml` on the lab PC, once the VM is up

```yaml
postgres_remote:
  host: "plotit-xams.nikhef.nl"
  port: 5432
  database: "xams"
  user: "xams_writer"
  password: "<writer pw>"
  sslmode: "verify-full"
  sslrootcert: "config/plotit-xams-ca.crt"
```

`verify-full` means the lab PC checks that it is talking to *this* VM, not
just to something that speaks TLS. `config/*.crt` is in `.gitignore`.

### 6c. Test the connection first, before touching the service

```powershell
.\.venv\Scripts\python.exe -c "import psycopg; c=psycopg.connect('host=plotit-xams.nikhef.nl dbname=xams user=xams_writer password=<pw> sslmode=verify-full sslrootcert=config/plotit-xams-ca.crt'); print(c.info.ssl_in_use)"
```

This should print `True`.

### 6d. Enable it

```powershell
xams-ctl restart
Get-Content logs\sinks.log -Tail 30   # look for "[nikhef-vm] connected"
```

On the VM:

```bash
psql "host=127.0.0.1 dbname=xams user=xams_reader" \
  -c "SELECT channel, max(t) FROM meas GROUP BY 1 ORDER BY 2 DESC LIMIT 10;"
```

---

## Step 7: Fill in the history

From the lab PC, as `xams_writer`:

```powershell
# everything this system has recorded
.\.venv\Scripts\python.exe tools\replay_jsonl.py --since 2026-09-01
# the LabVIEW history (import to JSONL first, if not done already)
.\.venv\Scripts\python.exe tools\import_labview_csv.py --from 2025-01-01
.\.venv\Scripts\python.exe tools\replay_jsonl.py --data-dir data\imported --since 2025-01-01
```

Do **not** use `import_labview_csv.py --to-postgres` for the VM: it deletes
before inserting, and `xams_writer` has no DELETE. The replay is idempotent
(unique index on `(t, channel, src)`) and prints how many rows were new.
Start with `--dry-run` to see what it will read.

---

## Step 8: Dashboards

On the VM, or from the lab PC through the URL:

```bash
cd /opt/xams-sc
python3 tools/save_dashboard.py --load \
  --url https://plotit-xams.nikhef.nl --password <admin pw>
```

**Make one Grafana the master for editing dashboards.** Two Grafanas that are
both edited will drift apart, and `--load` overwrites without a word (see
[Drift](docs/grafana/drift.md)). Proposal: edit on the **VM** (that is the one
the group looks at), `--save` → commit → `--load` on the lab PC. Write it in
DESIGN §12.

**The VM serves the main dashboards to users, nothing else.** The slow-control
web UI stays on the lab PC, and so do its "open in Grafana" links and mimic
popups: `grafana.url` in the lab PC's `secrets.yaml` keeps pointing at the
local Grafana (`127.0.0.1:3000`). So no `allow_embedding` on the VM, and
whether users log in (LDAP) or view anonymously is a free choice. The
`xams-channel` dashboard (the popup one) does not have to be loaded on the VM.

---

## Step 9: Watchdog alert

This is the reason the VM exists at all. In Grafana on the VM:

1. **Alerting → Contact points** → email → the group's address(es) → *Test*.
2. **Alerting → Alert rules → New**:
   - Query (datasource XAMS):
     ```sql
     SELECT extract(epoch FROM now() - max(t)) AS age_s FROM meas
     ```
   - Condition: `age_s > 900` (15 minutes)
   - **No data / Error handling → Alerting.** An empty result or an unreachable
     database must also fire the alarm.
   - Evaluate every 1m, pending period 0.
3. Test: stop the remote writer (or `xams-ctl stop` outside measuring time).
   Mail should arrive within about 16 minutes.

Export the rule to `grafana/provisioning-vm/alerting/` so it is in git.

---

## Step 10: Checklist before calling it done

- [ ] `nmap -p 5432 plotit-xams.nikhef.nl` from **another** machine: closed/filtered
- [ ] `ss -tlnp` on the VM: 3000 only on 127.0.0.1, 5432 on VM IP + localhost
- [ ] Lab PC `Test-NetConnection -Port 1883` from the VM side: **not** reachable (nothing goes into the lab)
- [ ] `ssl_in_use = True` on the writer connection
- [ ] `xams_writer` cannot UPDATE or DELETE (`psql ... -c "DELETE FROM meas"` → permission denied)
- [ ] `sudo systemctl stop postgresql-18` on the VM for 5 min → lab PC sinks stays `running`, warning in `sinks.log`, remote catches up afterwards
- [ ] Watchdog mail arrives
- [ ] Panels show data at <https://plotit-xams.nikhef.nl>
- [ ] Grafana admin password changed, stored in a password manager
- [ ] DESIGN §12 and `docs/install.md` updated with a VM section

---

## Maintenance

| What | How |
|---|---|
| Update the repo | `cd /opt/xams-sc && git pull`, then `sudo systemctl restart grafana-server` if provisioning changed |
| Schema change | `psql ... user=xams_admin -f sql/schema.sql` (idempotent) |
| VM lost | new VM, run steps 1–5, 7, 8. Nothing is lost |
| OS updates | Nikhef policy. A VM reboot costs the lab PC nothing (it buffers ~16 h; beyond that, `replay_jsonl.py`) |
| Backup | not needed beyond the standard policy. The VM is a copy |

## Undoing it

Remove the `postgres_remote:` block from `secrets.yaml` on the lab PC (or start
the sinks with `--no-remote`) and run `xams-ctl restart`. The lab PC is then exactly as it was before.

---

## Appendix: Debian / Ubuntu equivalents

| EL9 | Debian/Ubuntu |
|---|---|
| `dnf install` | `apt install` |
| firewalld rich rule | `ufw allow from <IP> to any port 5432 proto tcp`; `ufw allow 443/tcp` |
| PGDG repo rpm | `apt install postgresql-common && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh` then `apt install postgresql-18` |
| `/var/lib/pgsql/18/data/` | config in `/etc/postgresql/18/main/`, data in `/var/lib/postgresql/18/main/` |
| `postgresql-18` service | `postgresql` |
| Grafana rpm repo | `apt.grafana.com` (see grafana.com/docs → Install on Debian) |
| `/etc/nginx/conf.d/` | `/etc/nginx/sites-available/` + symlink to `sites-enabled/` |
| `setsebool` | not needed (no SELinux) |

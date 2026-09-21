# Request: a VM to serve XAMS slow-control plots

**From:** Auke-Pieter Colijn · **Date:** 2026-09-22
**In one line:** a small Linux VM running PostgreSQL and Grafana, so the group
can look at XAMS slow-control history in a browser, and so we have an
off-machine watchdog on the lab PC.

---

## 1. What this is

The XAMS slow control runs entirely on one Windows PC in the lab. It reads 63
channels at 1 Hz — pressures, temperatures, cryostat, high voltage, UPS —
stores them, alarms on them, and serves a local web UI. It is self-contained
and stays that way; nothing below changes that.

Two things it cannot do on its own:

1. **Let colleagues see the data** without sitting at the lab PC.
2. **Notice that the lab PC has died.** The alarm engine runs on that PC, so
   if the machine is off, crashed, or rebooting for Windows updates, the thing
   that would tell you is off too. An outside observer is the only way to
   catch it.

The second is the stronger reason, and it is why this is worth a VM rather
than a screen-sharing workaround.

## 2. What I am asking for

| | |
|---|---|
| **OS** | Any Linux you standardise on — Alma/Rocky/Debian/Ubuntu LTS all fine |
| **vCPU** | 2 |
| **RAM** | 4 GB minimum, 8 GB comfortable |
| **Disk** | 250 GB |
| **Software** | PostgreSQL 15+, Grafana, nginx (or your standard reverse proxy) |
| **Backup** | Your normal policy is fine — see §6, this VM holds no unique data |

**Disk sizing.** 63 channels × one 10-second aggregate = ~544 000 rows/day.
At roughly 190 bytes/row including indexes that is **~100 MB/day, ~35–40 GB
per year**. 250 GB gives several years plus room for a one-off import of
historical data from the old LabVIEW system.

## 3. Network — the important part

**All traffic is outbound from the lab PC. Nothing ever connects *into* the
lab PC.** That is a deliberate design property, not a convenience.

```
  lab PC (Windows)                       Nikhef VM
  ────────────────                       ─────────
  slow control  ──── TCP 5432 ────►  PostgreSQL     (outbound only, TLS)
                                          │
                                       Grafana
                                          │
  colleagues  ──────  HTTPS 443  ────►  nginx → Grafana
```

| Rule | Source | Destination | Port | Why |
|---|---|---|---|---|
| Inbound | lab PC IP **only** | VM | 5432/tcp | the slow control writes its readings |
| Inbound | see §4 | VM | 443/tcp | people viewing dashboards |
| Outbound | VM | your SMTP relay | 25/587 | one watchdog alert email |

Nothing inbound to the lab PC, at any point. The lab PC's MQTT broker — which
carries the commands that can set a high voltage — is bound to `127.0.0.1` and
stays there.

**I would like the PostgreSQL connection to use TLS.** It crosses the network
and I would rather not send it in clear, even inside Nikhef.

## 4. Access control — your call, my preference

The dashboards are **view-only**. There is no control path on this VM: nothing
on it can change anything in the lab, and the database role it writes with
cannot even modify existing rows (see §5).

My preference, in order:

1. **Nikhef SSO / LDAP**, Grafana Viewer role for everyone who authenticates.
2. **Restricted to the Nikhef network / VPN**, anonymous Viewer access.
3. Public read-only — only if the first two are awkward. Nothing here is
   sensitive, but I would rather not be the person who put an unauthenticated
   service on the internet.

I do **not** need Editor or Admin for anyone but me.

## 5. Database roles

Three roles, least privilege. I can run the SQL; I am listing it so you can
see there is no surprise in it.

| Role | Rights | Used by |
|---|---|---|
| `xams_writer` | `INSERT` on `meas` and the other tables — **no UPDATE, no DELETE** | the lab PC |
| `xams_reader` | `SELECT` only | Grafana's datasource |
| `xams_admin` | owner, schema changes | me, for upgrades |

The writer only ever inserts; the schema has a unique index that makes
re-sending a reading harmless, so it never needs to update a row. If the VM is
ever compromised, what is on it is a copy of readings that exist in full on
the lab PC.

## 6. This VM holds no unique data

The authoritative archive is a set of JSONL files on the lab PC, backed up
nightly to the Nikhef cluster. **The VM's database is an index over that, not
the record.** If the VM is lost entirely, we rebuild it from the archive and
lose nothing but the time to replay it.

So: your standard backup policy is fine. It does not need special treatment,
and a snapshot regime aimed at "can we rebuild this in a day" is sufficient.

## 7. The watchdog

One Grafana alert rule on the VM:

```
no measurement received for 15 minutes  →  email
```

A dead-man's switch on a different machine, on a different network, with a
completely separate code path from anything in the lab. It catches the one
failure the lab PC cannot report: itself being off. This needs the SMTP
outbound rule in §3 and nothing else.

## 8. Who does what

**You:**

- provision the VM and tell me its hostname
- the three firewall rules in §3
- PostgreSQL, Grafana and a reverse proxy installed, or let me install them
  if you would rather hand me a bare VM with sudo
- TLS certificate for the web front end
- tell me which access-control option in §4 you want

**Me:**

- database schema, roles and Grafana datasource — all scripted and in git
- dashboards — also in git, so the VM gets them with `git pull`
- configure the lab PC to write to the VM in addition to writing locally
- the watchdog rule
- keeping it updated

Installation on the VM is three commands, because the same repository installs
on the lab PC and here:

```bash
git clone <repo> /opt/xams-sc
psql -U postgres -d xams -f /opt/xams-sc/sql/schema.sql
# point Grafana at /opt/xams-sc/grafana/
```

## 9. What I need to know from you

1. Is a VM on these specs straightforward, and roughly when?
2. Which access-control option in §4?
3. Do you install PostgreSQL/Grafana/nginx, or hand me a bare VM with sudo?
4. What is the SMTP relay for the watchdog email?
5. Is there a Nikhef policy on TLS certificates for a service like this?
6. Anything in the above that conflicts with how you normally do things — I
   would rather match your conventions than have a snowflake.

## 10. If the answer is no

The lab PC keeps working exactly as it does now — nothing depends on this VM.
What we lose is the group being able to look at plots, and the outside
watchdog. If a VM is not possible, I would still like to discuss some way of
getting an alert when the lab PC goes quiet, because that is currently our
one blind spot.

---

*The full design is in the project's `DESIGN.md` §12, which specifies this VM
and the two-writer topology it uses. Happy to walk through any of it.*

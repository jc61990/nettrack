# NetTrack

A lightweight network device inventory system for managing physical security and IT devices — cameras, intercoms, access control panels, switches, WAPs, servers, and printers — by floor, switch, and port.

Built as a self-hosted alternative to NetBox for teams that want something simpler and purpose-built for physical security infrastructure.

---

## Features

- **Device inventory** — hostname, IP, MAC, type, status, floor, location, switch, port, VLAN, notes
- **Network scanner** — discovers devices via ping sweep, ARP, SNMP, LLDP, and MAC OUI lookup
- **Discovery queue** — all scan results go into a review queue before entering inventory. Accept, edit, ignore, or block devices before they're added
- **Floors management** — define floors per building, used as dropdowns across the app
- **VLAN management** — define VLANs with ID, name, and description
- **Switches tab** — manage switches separately from device inventory; view all devices per port per switch
- **Scheduled scanning** — configure auto-scan on a cron schedule from the UI
- **Email alerts** — get notified on new device discovery, devices going offline, or scan completion
- **Audit log** — every create, update, delete, and login is logged with user and timestamp
- **Export** — CSV and PDF export for device inventory (full, filtered, per-switch) and audit log
- **Dark mode** — follows system preference, with manual toggle
- **Role-based access** — read_only, modify, admin

---

## Stack

| Component | Technology |
|---|---|
| OS | Fedora 43 |
| Backend | FastAPI + Python 3.14 |
| Database | PostgreSQL 18 |
| Web server | nginx 1.28 |
| Frontend | Single-file HTML/JS (no framework) |
| Auth | JWT (HttpOnly cookies + in-memory Bearer token) |
| Process manager | systemd + uvicorn |

---

## Requirements

**Minimum:** 2 CPU cores, 2 GB RAM, 20 GB disk
**Recommended:** 4 cores, 4 GB RAM, 40 GB disk

The server must have network access to the subnets you want to scan. For ARP-based MAC discovery, the server must be on the same Layer 2 network as the devices.

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/yourorg/nettrack.git /opt/nettrack-src
cd /opt/nettrack-src
```

### 2. Install your TLS certificate

```bash
cp your-cert.crt  /etc/pki/tls/certs/nettrack.crt
cp your-cert.key  /etc/pki/tls/private/nettrack.key
chmod 644 /etc/pki/tls/certs/nettrack.crt
chmod 600 /etc/pki/tls/private/nettrack.key
```

Or generate a self-signed cert for internal use:

```bash
openssl req -x509 -nodes -days 3650 -newkey rsa:4096 \
  -keyout /etc/pki/tls/private/nettrack.key \
  -out    /etc/pki/tls/certs/nettrack.crt \
  -subj   "/CN=nettrack.yourdomain.local"
```

### 3. Set your hostname in nginx config

```bash
sed -i 's/nettrack.yourcompany.com/nettrack.yourdomain.local/g' deploy/nginx.conf
```

### 4. Run the setup script

```bash
bash deploy/setup_fedora43.sh
```

The script installs all dependencies, sets up PostgreSQL, runs migrations, creates an admin account, configures nginx and systemd, and prints the generated admin credentials at the end.

### 5. Update the environment file

```bash
nano /etc/nettrack/nettrack.env
```

Set your hostname:

```env
ALLOWED_ORIGINS=https://nettrack.yourdomain.local
ALLOWED_HOSTS=nettrack.yourdomain.local,localhost,127.0.0.1
```

### 6. Add DNS record

Point your internal DNS to the server IP:

```
nettrack.yourdomain.local  ->  <server IP>
```

### 7. Log in

Open `https://nettrack.yourdomain.local` in your browser, accept the certificate warning, and log in with the credentials printed by the setup script.

---

## First steps after login

1. **Change your admin password** — Users -> Edit -> set a new password
2. **Add floors** — Floors -> Add floor (these populate dropdowns across the app)
3. **Add VLANs** — VLANs -> Add VLAN
4. **Configure subnets** — Network scan -> enter your subnets -> Save config
5. **Run a scan** — Network scan -> Start scan
6. **Review results** — Discovery queue -> review each device, edit if needed, then Accept or Ignore

---

## Updating

```bash
bash /opt/nettrack-src/deploy/update.sh
```

The update script:
- Pulls latest code from git
- Runs pre-flight sanity checks (verifies key files are correct before touching anything)
- Backs up the current app to /opt/nettrack-backups/
- Installs any new Python dependencies
- Validates nginx config
- Runs database migrations
- Restarts the service (auto-restores backup if startup fails)
- Tests the API health endpoint
- Fixes nginx file permissions

---

## File locations

| Path | What it is |
|---|---|
| `/opt/nettrack/` | Deployed application code |
| `/opt/nettrack-src/` | Git repository |
| `/opt/nettrack-backups/` | Auto-backups (last 5 kept) |
| `/etc/nettrack/nettrack.env` | All secrets and environment variables |
| `/var/log/nettrack/` | Application logs |
| `/var/log/nettrack/scans/` | Raw scan result JSON files |
| `/etc/nginx/conf.d/nettrack.conf` | nginx config |
| `/etc/systemd/system/nettrack.service` | systemd unit |

---

## Environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `SECRET_KEY` | JWT signing key (auto-generated at install) |
| `ALLOWED_ORIGINS` | CORS allowed origins (your hostname) |
| `ALLOWED_HOSTS` | Trusted hosts |
| `SCANNER_API_TOKEN` | Long-lived JWT for the scanner to post results (auto-generated) |
| `SNMP_COMMUNITY` | SNMPv2c community string |
| `SNMP_V3_USERNAME` | SNMPv3 username (optional) |
| `SNMP_V3_AUTH_KEY` | SNMPv3 auth passphrase (optional) |
| `SNMP_V3_PRIV_KEY` | SNMPv3 priv passphrase (optional) |

---

## How scanning works

1. **Ping sweep** — ICMP to all hosts in each configured subnet, finds live IPs
2. **ARP scan** — nmap ARP ping to get MAC addresses from Layer 2 (no SNMP required)
3. **SNMP** — queries each live host for system description, hostname, and interface MACs
4. **LLDP** — walks LLDP MIB on switches to discover neighbors and port mappings
5. **MAC bridge table** — pulls the switch MAC address table to map devices to ports
6. **OUI lookup** — identifies vendor from MAC address prefix

All results go to the discovery queue. Nothing is added to inventory automatically. Review each device, edit details if needed, then accept or ignore.

---

## Roles

| Role | Permissions |
|---|---|
| `read_only` | View devices, inventory, audit log, export |
| `modify` | All of the above + add/edit devices, run scans, manage queue, manage floors/VLANs/switches |
| `admin` | All of the above + manage users |

---

## Database migrations

Migrations run automatically during update.sh. To run manually:

```bash
source /etc/nettrack/nettrack.env
export PATH="/home/nettrack/.local/bin:$PATH"
cd /opt/nettrack
sudo -u nettrack --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    alembic upgrade head
```

---

## Troubleshooting

**500 on the login page**
```bash
tail -20 /var/log/nginx/error.log
# Usually a permissions issue:
chmod o+rx /opt/nettrack /opt/nettrack/frontend
find /opt/nettrack/frontend -type f -exec chmod o+r {} \;
usermod -aG nettrack nginx && systemctl restart nginx
```

**Login succeeds but shows blank / loops**
```bash
systemctl status nettrack
curl -sk https://localhost/api/health
```

**Scan finds hosts but nothing appears in queue**
```bash
source /etc/nettrack/nettrack.env
curl -sk https://localhost/api/queue/counts \
  -H "Authorization: Bearer $SCANNER_API_TOKEN"
# If you get "Invalid or expired token", run:
bash /opt/nettrack-src/deploy/update.sh
```

**Devices have no MAC address after scan**
The ARP scan requires the NetTrack server to be on the same Layer 2 network as the devices being scanned. If routing through a different subnet, ARP will not return MACs. Devices without MACs are still added to the queue using IP as the dedup key.

---

## License

MIT

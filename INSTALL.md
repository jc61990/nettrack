# NetTrack — Installation Guide
## Fedora 43 · Native install · No Docker · No venv

---

## Before you start

**What you need:**
- A Fedora 43 server (physical or VM) with root access
- At minimum: 2 CPU cores, 2 GB RAM, 20 GB disk
- Recommended: 4 cores, 4 GB RAM, 40 GB disk
- The server must have network access to the subnets you want to scan
- A domain name or internal hostname pointing at the server (e.g. `nettrack.yourcompany.com`)
- A TLS certificate for that hostname (self-signed is fine for internal use)
- The NetTrack source files (this repository) copied to the server

**What the installer does automatically:**
- Creates the `nettrack` system user
- Installs Python 3.14, PostgreSQL 18, nginx, nmap via DNF
- Installs Python packages via `pip --user` (no venv needed)
- Creates the PostgreSQL database with a random password
- Generates a random `SECRET_KEY`
- Runs database migrations
- Creates an initial admin account with a generated password
- Installs and starts the systemd service
- Configures nginx as a reverse proxy
- Opens firewall ports 80 and 443
- Configures SELinux for nginx → uvicorn proxying

---

## Step 1 — Copy files to the server

On your workstation, copy the project to the server:

```bash
scp -r ./nettrack root@your-server:/opt/nettrack-src
```

Or clone from your git repository:

```bash
ssh root@your-server
git clone https://github.com/yourorg/nettrack.git /opt/nettrack-src
```

---

## Step 2 — Install your TLS certificate

The nginx config expects your certificate at these paths:

```
/etc/pki/tls/certs/nettrack.crt      ← full chain certificate
/etc/pki/tls/private/nettrack.key    ← private key
```

Copy them now before running the setup script:

```bash
# If you have a certificate from your CA:
cp your-cert.crt     /etc/pki/tls/certs/nettrack.crt
cp your-cert.key     /etc/pki/tls/private/nettrack.key
chmod 644 /etc/pki/tls/certs/nettrack.crt
chmod 600 /etc/pki/tls/private/nettrack.key
```

**If you don't have a certificate yet — generate a self-signed one:**

```bash
openssl req -x509 -nodes -days 3650 -newkey rsa:4096 \
  -keyout /etc/pki/tls/private/nettrack.key \
  -out    /etc/pki/tls/certs/nettrack.crt \
  -subj   "/CN=nettrack.yourcompany.com"
chmod 644 /etc/pki/tls/certs/nettrack.crt
chmod 600 /etc/pki/tls/private/nettrack.key
```

> **Note:** Self-signed certificates will show a browser warning. For production,
> use a certificate from your internal CA or Let's Encrypt.

---

## Step 3 — Update nginx config with your hostname

Edit the nginx config before running setup:

```bash
sed -i 's/nettrack.yourcompany.com/YOUR_ACTUAL_HOSTNAME/g' \
    /opt/nettrack-src/deploy/nginx.conf
```

---

## Step 4 — Run the setup script

```bash
cd /opt/nettrack-src
bash deploy/setup_fedora43.sh
```

This takes 3–5 minutes. Watch for any errors in red. At the end you will see:

```
╔══════════════════════════════════════════════════════╗
║  NetTrack deployed on Fedora 43                      ║
╚══════════════════════════════════════════════════════╝

  Initial admin credentials:
  Email:    admin@yourcompany.com
  Password: <generated password>

  Next steps:
  1. Edit ALLOWED_ORIGINS, ALLOWED_HOSTS in /etc/nettrack/nettrack.env
  2. Install TLS cert → /etc/pki/tls/certs/nettrack.crt
  3. Update server_name in /etc/nginx/conf.d/nettrack.conf
  4. systemctl restart nettrack nginx
  5. Change admin password after first login
```

**Write down the generated admin password** — you will need it for first login.

---

## Step 5 — Update the environment file

```bash
nano /etc/nettrack/nettrack.env
```

At minimum update these two lines to match your actual hostname:

```env
ALLOWED_ORIGINS=https://nettrack.yourcompany.com
ALLOWED_HOSTS=nettrack.yourcompany.com,localhost,127.0.0.1
```

If you are using SSO (Azure AD / Okta / Google Workspace), fill in the OIDC
section now:

```env
OIDC_CLIENT_ID=your-client-id
OIDC_CLIENT_SECRET=your-client-secret
OIDC_DISCOVERY_URL=https://login.microsoftonline.com/{tenant-id}/v2.0
OIDC_REDIRECT_URI=https://nettrack.yourcompany.com/api/auth/callback
OIDC_DEFAULT_ROLE=read_only
FRONTEND_URL=https://nettrack.yourcompany.com
```

Save the file, then restart the service:

```bash
systemctl restart nettrack nginx
```

---

## Step 6 — Configure your subnets

Edit the scanner config to tell NetTrack which subnets to scan:

```bash
nano /opt/nettrack/scanner/config.yaml
```

Replace the example subnets with your actual network ranges:

```yaml
subnets:
  - cidr: 192.168.1.0/24
    description: "Floor 1"

  - cidr: 192.168.2.0/24
    description: "Floor 2"

  - cidr: 10.10.0.0/23
    description: "Security devices VLAN"
```

Save the file. No restart needed — the scanner reads this file on every run.

---

## Step 7 — Configure SNMP credentials

SNMP credentials go in the environment file, **not** in config.yaml.

```bash
nano /etc/nettrack/nettrack.env
```

**For SNMPv2c only:**
```env
SNMP_COMMUNITY=your-community-string
```

**For SNMPv3 (recommended):**
```env
SNMP_V3_USERNAME=nettrack
SNMP_V3_AUTH_KEY=your-auth-passphrase
SNMP_V3_PRIV_KEY=your-priv-passphrase
SNMP_V3_AUTH_PROTOCOL=sha
SNMP_V3_PRIV_PROTOCOL=aes
```

The scanner tries v3 first and falls back to v2c automatically.

Restart after editing:

```bash
systemctl restart nettrack
```

---

## Step 8 — First login

Open your browser and navigate to:

```
https://nettrack.yourcompany.com
```

Log in with:
- **Email:** `admin@yourcompany.com`
- **Password:** the generated password from Step 4

**Immediately change the admin password:**

1. Click **Users** in the left sidebar
2. Click **Edit** next to the admin account
3. Enter a new password (minimum 12 characters)
4. Click **Save**

---

## Step 9 — Invite your team

Still in the **Users** panel:

1. Click **+ Invite user**
2. Enter their name, email, and choose a role:
   - **Read only** — can view devices and reports, export data
   - **Modify** — can add, edit devices and trigger scans
   - **Admin** — full access including user management and audit log
3. Set a temporary password and share it with them securely
4. They log in and change their password

---

## Step 10 — Run your first scan

1. Click **Network scan** in the sidebar
2. Verify the subnets shown match what you configured in Step 6
3. Click **Start scan**
4. Watch the live output — the scan runs ping sweep → SNMP → LLDP → ARP → MAC lookup
5. When complete, click **Devices** to see the populated inventory

The first scan populates the database. Subsequent scans update existing records and flag new or offline devices.

---

## Step 11 — Enable scheduled scanning (optional)

1. Go to **Network scan** in the sidebar
2. In the **Auto-scan schedule** panel, choose a preset:
   - Every hour
   - Every 2 hours
   - Every 4 hours (recommended)
   - Daily at 2am
3. Toggle the switch to **on**
4. The countdown timer shows when the next scan will run

---

## Step 12 — Configure email alerts (optional)

In the same scan page, fill in the **Email alerts** panel:

| Field | Example |
|---|---|
| SMTP host | `smtp.yourcompany.com` |
| Port | `587` (STARTTLS) or `465` (SSL) |
| Username | your SMTP username |
| Password | your SMTP password |
| From address | `nettrack@yourcompany.com` |
| Recipients | `ops@yourcompany.com, noc@yourcompany.com` |

Check which alerts you want, click **Save**, then click **Send test** to verify
the SMTP settings before enabling.

---

## Verifying everything works

```bash
# Is the service running?
systemctl status nettrack

# Live logs
journalctl -u nettrack -f

# Is the API responding?
curl -sk https://localhost/api/health
# Expected: {"status":"ok"}

# Is the database connected?
curl -sk https://localhost/api/health
# If this returns 500, check: journalctl -u nettrack -n 50

# PostgreSQL status
systemctl status postgresql

# nginx status
systemctl status nginx
```

---

## Common issues and fixes

**Service won't start — `SECRET_KEY` not set**
```bash
journalctl -u nettrack -n 20
# Look for: RuntimeError: Required environment variable 'SECRET_KEY' is not set
# Fix: check /etc/nettrack/nettrack.env has SECRET_KEY=<value>
systemctl restart nettrack
```

**Login fails — 401 Invalid credentials**
- Double-check the email is `admin@yourcompany.com` (lowercase)
- Check the password from Step 4
- If locked out: reset via psql (see below)

**CORS error in browser console**
```bash
nano /etc/nettrack/nettrack.env
# Update: ALLOWED_ORIGINS=https://your-actual-hostname
systemctl restart nettrack
```

**Scanner times out on all hosts**
- Verify the server can reach the subnet: `ping -c1 <device-ip>`
- Check firewall on the server: `firewall-cmd --list-all`
- If using a management VLAN, confirm the server has a route to it

**SNMP returns no data**
- Test manually: `snmpwalk -v2c -c public <switch-ip> sysDescr`
- Check the community string in `/etc/nettrack/nettrack.env`
- Check the switch ACL allows SNMP from the server's IP

**nginx returns 502 Bad Gateway**
```bash
systemctl status nettrack       # is uvicorn running?
curl http://127.0.0.1:8000/api/health   # can nginx reach it?
setsebool -P httpd_can_network_connect 1   # SELinux fix
```

**Reset the admin password via psql:**
```bash
python3 -c "from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash('NewPassword123!'))"
# Copy the hash, then:
sudo -u postgres psql nettrack
UPDATE users SET password_hash = '<paste hash>' WHERE email = 'admin@yourcompany.com';
\q
```

---

## File locations reference

| Path | What it is |
|---|---|
| `/opt/nettrack/` | Application code |
| `/opt/nettrack/scanner/config.yaml` | Subnet and tuning config |
| `/etc/nettrack/nettrack.env` | All secrets and environment variables |
| `/var/log/nettrack/` | Log directory |
| `/var/log/nettrack/scans/` | Raw scan result JSON files |
| `/etc/systemd/system/nettrack.service` | Systemd unit file |
| `/etc/nginx/conf.d/nettrack.conf` | nginx config |
| `/home/nettrack/.local/` | Python packages (pip --user) |

---

## Useful commands

```bash
# Restart after config changes
systemctl restart nettrack

# View live logs
journalctl -u nettrack -f

# Apply a database migration after updating code
cd /opt/nettrack
source /etc/nettrack/nettrack.env
alembic upgrade head

# Check scheduled scan next run time
curl -sk -H "Authorization: Bearer <token>" \
  https://nettrack.yourcompany.com/api/scan/schedule

# Manual scan via API
curl -sk -X POST \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"subnets":["192.168.1.0/24"]}' \
  https://nettrack.yourcompany.com/api/scan/trigger
```

---

## Updating NetTrack

```bash
# Pull latest code
cd /opt/nettrack-src
git pull

# Copy updated files
cp -r . /opt/nettrack/

# Apply any new migrations
cd /opt/nettrack
source /etc/nettrack/nettrack.env
/home/nettrack/.local/bin/alembic upgrade head

# Restart
systemctl restart nettrack
```

#!/usr/bin/env bash
# NetTrack setup script — Fedora 43
# Native install: system Python 3.14, system PostgreSQL 18, no venv, no Docker.
#
# Run as root:  bash deploy/setup_fedora43.sh

set -euo pipefail

APP_DIR=/opt/nettrack
LOG_DIR=/var/log/nettrack
ENV_FILE=/etc/nettrack/nettrack.env
SERVICE_FILE=/etc/systemd/system/nettrack.service
APP_USER=nettrack
APP_GROUP=nettrack
DB_NAME=nettrack
DB_USER=nettrack
PYTHON=python3
PIP="sudo -u ${APP_USER} pip3 install --user --break-system-packages"
UVICORN="sudo -u ${APP_USER} /home/nettrack/.local/bin/uvicorn"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root"
. /etc/os-release
[[ "${ID}" != "fedora" ]] && warn "This script targets Fedora — detected: ${ID}"

# ── 1. System user ────────────────────────────────────────────────────────────
info "Creating system user '${APP_USER}'..."
if ! id "${APP_USER}" &>/dev/null; then
    useradd --system --create-home --home-dir /home/nettrack \
            --shell /sbin/nologin \
            --comment "NetTrack service account" "${APP_USER}"
fi
# --create-home is needed so pip --user has somewhere to install

# ── 2. System packages via DNF5 ───────────────────────────────────────────────
info "Installing system packages..."
dnf install -y \
    python3 python3-pip python3-devel \
    postgresql postgresql-server postgresql-devel \
    python3-sqlalchemy \
    python3-psycopg2 \
    python3-pyyaml \
    python3-requests \
    python3-httpx \
    gcc \
    nginx \
    nmap \
    iputils \
    git \
    libpq-devel

# ── 3. PostgreSQL 18 setup ────────────────────────────────────────────────────
info "Setting up PostgreSQL 18..."
if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then
    postgresql-setup --initdb
fi
systemctl enable --now postgresql

DB_PASS=$(openssl rand -hex 24)

su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'\"" | grep -q 1 || \
    su - postgres -c "psql -c \"CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';\""

su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" | grep -q 1 || \
    su - postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};\""

info "PostgreSQL 18 ready."

# ── 4. Deploy application ─────────────────────────────────────────────────────
info "Deploying application to ${APP_DIR}..."
mkdir -p "${APP_DIR}" "${LOG_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
      "${REPO_ROOT}/" "${APP_DIR}/"

# ── 5. Python dependencies ────────────────────────────────────────────────────
# Install as the nettrack user (pip --user) so no system-wide pollution.
# DNF already provided: sqlalchemy, psycopg2, pyyaml, requests, httpx
# Install the rest via pip --user as nettrack.
info "Installing Python packages as '${APP_USER}'..."

# Ensure pip is up to date for the user
$PIP --upgrade pip

$PIP \
    "fastapi==0.111.0" \
    "uvicorn[standard]==0.29.0" \
    "pydantic==2.7.1" \
    "python-dotenv==1.0.1" \
    "python-jose[cryptography]==3.3.0" \
    "passlib[bcrypt]==1.7.4" \
    "python-multipart==0.0.9" \
    "slowapi==0.1.9" \
    "pysnmp==6.1.4" \
    "apscheduler==3.10.4" \
    "alembic==1.13.1"

# Verify uvicorn is reachable
sudo -u "${APP_USER}" /home/nettrack/.local/bin/uvicorn --version \
    || error "uvicorn not found after install"
info "Python dependencies installed ✓"

# ── 6. Environment file ───────────────────────────────────────────────────────
info "Creating environment file at ${ENV_FILE}..."
mkdir -p /etc/nettrack
chmod 750 /etc/nettrack

SECRET_KEY=$(openssl rand -hex 32)

if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" << ENVEOF
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}
SECRET_KEY=${SECRET_KEY}
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7
ALLOWED_ORIGINS=https://nettrack.yourcompany.com
ALLOWED_HOSTS=nettrack.yourcompany.com,localhost,127.0.0.1
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
OIDC_DISCOVERY_URL=
OIDC_REDIRECT_URI=https://nettrack.yourcompany.com/api/auth/callback
OIDC_DEFAULT_ROLE=read_only
FRONTEND_URL=https://nettrack.yourcompany.com
SNMP_COMMUNITY=public
SNMP_V3_USERNAME=
SNMP_V3_AUTH_KEY=
SNMP_V3_PRIV_KEY=
SNMP_V3_AUTH_PROTOCOL=sha
SNMP_V3_PRIV_PROTOCOL=aes
SCANNER_API_TOKEN=
ENVIRONMENT=production
ENVEOF
    chmod 640 "${ENV_FILE}"
    chown root:${APP_GROUP} "${ENV_FILE}"
else
    warn "${ENV_FILE} already exists — skipping."
fi

# ── 7. Permissions ────────────────────────────────────────────────────────────
info "Setting permissions..."
chown -R root:${APP_GROUP} "${APP_DIR}"
chmod -R 750 "${APP_DIR}"
chown -R ${APP_USER}:${APP_GROUP} "${LOG_DIR}"
chmod 770 "${LOG_DIR}"

# ── 8. Ping capability ────────────────────────────────────────────────────────
info "Checking ping capability..."
if su -s /bin/bash "${APP_USER}" -c "ping -c1 -W1 127.0.0.1" &>/dev/null; then
    info "ping works as '${APP_USER}' ✓ (Fedora allows ICMP via ping_group_range)"
else
    PYTHON_BIN=$(su -s /bin/bash "${APP_USER}" -c "which python3")
    warn "ping restricted — granting cap_net_raw to ${PYTHON_BIN}..."
    setcap cap_net_raw+ep "${PYTHON_BIN}"
    su -s /bin/bash "${APP_USER}" -c "ping -c1 -W1 127.0.0.1" &>/dev/null \
        && info "cap_net_raw applied ✓" \
        || warn "ping still failing — nmap fallback will be used (already in scanner)"
fi

# ── 9. Database migration ─────────────────────────────────────────────────────
info "Running Alembic migrations..."
cd "${APP_DIR}"
set -a; source "${ENV_FILE}"; set +a
# Add ~/.local/bin to PATH so alembic is found
export PATH="/home/nettrack/.local/bin:${PATH}"
sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    /home/nettrack/.local/bin/alembic upgrade head

# ── 10. Bootstrap admin user ──────────────────────────────────────────────────
info "Creating initial admin user..."
ADMIN_PASS=$(openssl rand -base64 16)
export ADMIN_PASS DATABASE_URL SECRET_KEY
sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,ADMIN_PASS \
    ${PYTHON} -c "
import sys, os
sys.path.insert(0, '${APP_DIR}')
from database import SessionLocal
import models, auth

db = SessionLocal()
existing = db.query(models.User).filter(models.User.email == 'admin@yourcompany.com').first()
if not existing:
    admin = models.User(
        email='admin@yourcompany.com',
        full_name='Admin',
        role='admin',
        auth_provider='local',
        is_active=True,
        password_hash=auth.hash_password(os.environ['ADMIN_PASS']),
    )
    db.add(admin)
    db.commit()
    print('Admin user created')
else:
    print('Admin user already exists — skipping')
db.close()
"

# ── 11. Systemd service ───────────────────────────────────────────────────────
info "Installing systemd service..."

# Write a Fedora-specific service file pointing at ~/.local/bin/uvicorn
cat > "${SERVICE_FILE}" << SVCEOF
[Unit]
Description=NetTrack API
After=network.target postgresql.service
Wants=postgresql.service

[Service]
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PATH=/home/nettrack/.local/bin:/usr/bin:/bin
Environment=PYTHONPATH=${APP_DIR}

ExecStart=/home/nettrack/.local/bin/uvicorn main:app \\
    --host 127.0.0.1 \\
    --port 8000 \\
    --workers 2 \\
    --log-level info

ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${LOG_DIR} ${APP_DIR}
ProtectHome=read-only
ProtectKernelModules=true
ProtectKernelTunables=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryDenyWriteExecute=true
LockPersonality=true
SystemCallFilter=@system-service @network-io
SystemCallErrorNumber=EPERM

StandardOutput=journal
StandardError=journal
SyslogIdentifier=nettrack

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable nettrack
systemctl start nettrack
sleep 2
systemctl is-active --quiet nettrack \
    && info "NetTrack service running ✓" \
    || error "Service failed — check: journalctl -u nettrack -n 50"

# ── 12. Nginx ─────────────────────────────────────────────────────────────────
info "Configuring nginx..."
if ! grep -q "login_limit" /etc/nginx/nginx.conf; then
    sed -i '/^http {/a\    limit_req_zone $binary_remote_addr zone=login_limit:10m rate=10r/m;' \
        /etc/nginx/nginx.conf
fi
cp "${SCRIPT_DIR}/nginx.conf" /etc/nginx/conf.d/nettrack.conf
nginx -t && systemctl enable --now nginx \
    || warn "nginx config test failed — edit /etc/nginx/conf.d/nettrack.conf"

# ── 13. Firewall ──────────────────────────────────────────────────────────────
info "Opening firewall ports..."
firewall-cmd --permanent --add-service=http  2>/dev/null || true
firewall-cmd --permanent --add-service=https 2>/dev/null || true
firewall-cmd --reload 2>/dev/null || true

# ── 14. SELinux ───────────────────────────────────────────────────────────────
info "Configuring SELinux..."
setsebool -P httpd_can_network_connect 1

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  NetTrack deployed on Fedora 43                      ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Python:      $(python3 --version)"
echo -e "  PostgreSQL:  $(psql --version)"
echo -e "  App dir:     ${APP_DIR}"
echo -e "  Env file:    ${ENV_FILE}"
echo -e "  Logs:        journalctl -u nettrack -f"
echo ""
echo -e "${YELLOW}  Initial admin credentials:${NC}"
echo -e "  Email:    admin@yourcompany.com"
echo -e "  Password: ${ADMIN_PASS}"
echo ""
echo -e "${YELLOW}  Next steps:${NC}"
echo -e "  1. Edit ALLOWED_ORIGINS, ALLOWED_HOSTS in ${ENV_FILE}"
echo -e "  2. Install TLS cert → /etc/pki/tls/certs/nettrack.crt"
echo -e "  3. Update server_name in /etc/nginx/conf.d/nettrack.conf"
echo -e "  4. systemctl restart nettrack nginx"
echo -e "  5. Change admin password after first login"
echo ""

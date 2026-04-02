#!/usr/bin/env bash
# NetTrack server setup script — RHEL / CentOS 8+
#
# Run as root:  bash deploy/setup.sh
#
# What this does:
#   1. Creates the nettrack system user
#   2. Installs Python 3.11, nginx, postgresql
#   3. Creates the PostgreSQL database and user
#   4. Deploys the app to /opt/nettrack
#   5. Creates the environment file at /etc/nettrack/nettrack.env
#   6. Installs and enables the systemd service
#   7. Installs the nginx config
#   8. Sets file permissions and capabilities

set -euo pipefail

APP_DIR=/opt/nettrack
LOG_DIR=/var/log/nettrack
ENV_FILE=/etc/nettrack/nettrack.env
SERVICE_FILE=/etc/systemd/system/nettrack.service
APP_USER=nettrack
APP_GROUP=nettrack
DB_NAME=nettrack
DB_USER=nettrack

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run this script as root"

# ── 1. System user ────────────────────────────────────────────────────────────
info "Creating system user '${APP_USER}'..."
if ! id "${APP_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /sbin/nologin \
            --comment "NetTrack service account" "${APP_USER}"
fi

# ── 2. System packages ────────────────────────────────────────────────────────
info "Installing system packages..."
dnf install -y epel-release
dnf install -y python3.11 python3.11-pip python3.11-devel \
               nginx postgresql postgresql-server postgresql-devel \
               gcc iputils git

# ── 3. PostgreSQL setup ───────────────────────────────────────────────────────
info "Setting up PostgreSQL..."
if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then
    postgresql-setup --initdb
fi
systemctl enable --now postgresql

# Generate a random DB password
DB_PASS=$(openssl rand -hex 24)

su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'\"" | grep -q 1 || \
    su - postgres -c "psql -c \"CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';\""

su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'\"" | grep -q 1 || \
    su - postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};\""

info "PostgreSQL ready. DB password stored in ${ENV_FILE}"

# ── 4. Application directory ──────────────────────────────────────────────────
info "Deploying application to ${APP_DIR}..."
mkdir -p "${APP_DIR}" "${LOG_DIR}"

# Copy application files (assumes script is run from the repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
      "${REPO_ROOT}/" "${APP_DIR}/"

# ── 5. Python virtual environment ─────────────────────────────────────────────
info "Creating Python virtual environment..."
python3.11 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# ── 6. Environment file ───────────────────────────────────────────────────────
info "Creating environment file at ${ENV_FILE}..."
mkdir -p /etc/nettrack
chmod 750 /etc/nettrack

SECRET_KEY=$(openssl rand -hex 32)

if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" << ENVEOF
# NetTrack environment — keep this file secret (chmod 640, owned by root:nettrack)

# Database
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}

# JWT secret — do not change after first deployment (invalidates all sessions)
SECRET_KEY=${SECRET_KEY}
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# CORS / host security — update with your actual domain
ALLOWED_ORIGINS=https://nettrack.yourcompany.com
ALLOWED_HOSTS=nettrack.yourcompany.com,localhost,127.0.0.1

# SSO (optional — leave blank to use local accounts only)
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
OIDC_DISCOVERY_URL=
OIDC_REDIRECT_URI=https://nettrack.yourcompany.com/api/auth/callback
OIDC_DEFAULT_ROLE=read_only
FRONTEND_URL=https://nettrack.yourcompany.com

# Scanner SNMP credentials
SNMP_COMMUNITY=public
SNMP_V3_USERNAME=
SNMP_V3_AUTH_KEY=
SNMP_V3_PRIV_KEY=
SNMP_V3_AUTH_PROTOCOL=sha
SNMP_V3_PRIV_PROTOCOL=aes
SCANNER_API_TOKEN=

# Environment
ENVIRONMENT=production
ENVEOF
    chmod 640 "${ENV_FILE}"
    chown root:${APP_GROUP} "${ENV_FILE}"
    info "Environment file created. Edit ${ENV_FILE} to add SSO and SNMP credentials."
else
    warn "${ENV_FILE} already exists — skipping. Update manually if needed."
fi

# ── 7. File permissions ───────────────────────────────────────────────────────
info "Setting file permissions..."
chown -R root:${APP_GROUP} "${APP_DIR}"
chmod -R 750 "${APP_DIR}"
chown -R ${APP_USER}:${APP_GROUP} "${LOG_DIR}"
chmod 770 "${LOG_DIR}"

# venv needs to be executable by the app user
chmod -R 755 "${APP_DIR}/venv"

# ── 8. ping capability for scanner ───────────────────────────────────────────
info "Setting ping capability for scanner (non-root ICMP)..."
# On RHEL, ping is typically setuid — confirm it works for the nettrack user
# If not, grant cap_net_raw to Python:
if ! su -s /bin/bash "${APP_USER}" -c "ping -c1 -W1 127.0.0.1" &>/dev/null; then
    warn "ping not working as '${APP_USER}' — granting cap_net_raw to Python..."
    setcap cap_net_raw+ep "${APP_DIR}/venv/bin/python3.11"
    info "cap_net_raw granted to venv Python"
else
    info "ping works without extra capabilities ✓"
fi

# ── 9. Database migration ─────────────────────────────────────────────────────
info "Running database migrations..."
cd "${APP_DIR}"
set -a; source "${ENV_FILE}"; set +a
"${APP_DIR}/venv/bin/alembic" upgrade head

# ── 10. Bootstrap admin user ──────────────────────────────────────────────────
info "Creating initial admin user..."
ADMIN_PASS=$(openssl rand -base64 16)
"${APP_DIR}/venv/bin/python3" << PYEOF
import sys
sys.path.insert(0, '${APP_DIR}')
import os
os.environ.setdefault('DATABASE_URL', '${DATABASE_URL:-}')
os.environ.setdefault('SECRET_KEY', '${SECRET_KEY}')
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
        password_hash=auth.hash_password('${ADMIN_PASS}'),
    )
    db.add(admin)
    db.commit()
    print('Admin user created: admin@yourcompany.com')
else:
    print('Admin user already exists — skipping')
db.close()
PYEOF

# ── 11. Systemd service ───────────────────────────────────────────────────────
info "Installing systemd service..."
cp "${SCRIPT_DIR}/nettrack.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable nettrack
systemctl start nettrack
sleep 2
systemctl is-active --quiet nettrack && info "NetTrack service is running ✓" || \
    error "Service failed to start. Check: journalctl -u nettrack -n 50"

# ── 12. Nginx ─────────────────────────────────────────────────────────────────
info "Installing nginx configuration..."

# Add rate limit zone to nginx.conf if not present
if ! grep -q "login_limit" /etc/nginx/nginx.conf; then
    sed -i '/^http {/a\    limit_req_zone $binary_remote_addr zone=login_limit:10m rate=10r/m;' \
        /etc/nginx/nginx.conf
fi

cp "${SCRIPT_DIR}/nginx.conf" /etc/nginx/conf.d/nettrack.conf
nginx -t && systemctl enable --now nginx || \
    warn "nginx config test failed — check /etc/nginx/conf.d/nettrack.conf"

# ── 13. Firewall ──────────────────────────────────────────────────────────────
info "Opening firewall ports..."
firewall-cmd --permanent --add-service=http  2>/dev/null || true
firewall-cmd --permanent --add-service=https 2>/dev/null || true
firewall-cmd --reload 2>/dev/null || true

# ── 14. SELinux ───────────────────────────────────────────────────────────────
info "Configuring SELinux for nginx reverse proxy..."
setsebool -P httpd_can_network_connect 1 2>/dev/null || \
    warn "Could not set SELinux boolean — set manually: setsebool -P httpd_can_network_connect 1"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  NetTrack deployment complete                        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  App directory:  ${APP_DIR}"
echo -e "  Env file:       ${ENV_FILE}"
echo -e "  Logs:           journalctl -u nettrack -f"
echo -e "  Service:        systemctl status nettrack"
echo ""
echo -e "${YELLOW}  Initial admin credentials:${NC}"
echo -e "  Email:    admin@yourcompany.com"
echo -e "  Password: ${ADMIN_PASS}"
echo ""
echo -e "${YELLOW}  Next steps:${NC}"
echo -e "  1. Update ALLOWED_ORIGINS and ALLOWED_HOSTS in ${ENV_FILE}"
echo -e "  2. Install your TLS certificate at /etc/pki/tls/certs/nettrack.crt"
echo -e "  3. Update server_name in /etc/nginx/conf.d/nettrack.conf"
echo -e "  4. systemctl restart nettrack nginx"
echo -e "  5. Change the admin password after first login"
echo ""

#!/usr/bin/env bash
# NetTrack update script — with sanity checks
# Usage: bash /opt/nettrack-src/deploy/update.sh

set -euo pipefail

SRC_DIR=/opt/nettrack-src
APP_DIR=/opt/nettrack
BACKUP_DIR=/opt/nettrack-backups
ENV_FILE=/etc/nettrack/nettrack.env
ALEMBIC=/home/nettrack/.local/bin/alembic
APP_USER=nettrack

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }
check() { echo -e "${GREEN}[✓]${NC} $*"; }
fail()  { echo -e "${RED}[✗]${NC} $*"; CHECKS_FAILED=$((CHECKS_FAILED+1)); }

CHECKS_FAILED=0

[[ $EUID -ne 0 ]] && error "Run as root"
[[ ! -d "${SRC_DIR}/.git" ]] && error "${SRC_DIR} is not a git repository"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
info "Pulling latest code from git..."
cd "${SRC_DIR}"
git fetch origin
CHANGES=$(git log HEAD..origin/main --oneline 2>/dev/null | wc -l)
if [[ "${CHANGES}" -eq 0 ]]; then
    warn "Already up to date — no new commits. Running deploy steps anyway..."
else
    info "$(git log HEAD..origin/main --oneline)"
fi
git pull origin main

# ── 2. Pre-flight sanity checks on source files ───────────────────────────────
info "Running pre-flight checks on source files..."

# Check scanner/scanner.py is the engine, not the router
if grep -q "class NetworkScanner" "${SRC_DIR}/scanner/scanner.py"; then
    check "scanner/scanner.py contains NetworkScanner ✓"
else
    fail "scanner/scanner.py is WRONG — missing NetworkScanner class"
    fail "  This file should be the discovery engine, not the API router"
    fail "  Check your git repo — scanner/scanner.py and routers/scanner.py may be swapped"
fi

# Check routers/scanner.py is the router
if grep -q "APIRouter" "${SRC_DIR}/routers/scanner.py"; then
    check "routers/scanner.py contains APIRouter ✓"
else
    fail "routers/scanner.py is WRONG — missing APIRouter"
fi

# Quick Python import check on all source files
info "Checking Python syntax on all source files..."
SYNTAX_ERRORS=0
while IFS= read -r -d '' pyfile; do
    if ! python3 -m py_compile "${pyfile}" 2>/dev/null; then
        fail "Syntax error in: ${pyfile}"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS+1))
    fi
done < <(find "${SRC_DIR}" -name "*.py" -not -path "*/__pycache__/*" -print0)

if [[ "${SYNTAX_ERRORS}" -eq 0 ]]; then
    check "All Python files pass syntax check ✓"
fi

if [[ "${CHECKS_FAILED}" -gt 0 ]]; then
    error "${CHECKS_FAILED} pre-flight check(s) failed — aborting deploy to protect your running app"
fi

# ── 3. Backup current app ─────────────────────────────────────────────────────
info "Backing up current app..."
mkdir -p "${BACKUP_DIR}"
BACKUP_STAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_PATH="${BACKUP_DIR}/nettrack_${BACKUP_STAMP}"
cp -r "${APP_DIR}" "${BACKUP_PATH}"
# Keep only last 5 backups
ls -dt "${BACKUP_DIR}"/nettrack_* 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null || true
check "Backup saved to ${BACKUP_PATH} ✓"

# ── 4. Install any new Python dependencies ────────────────────────────────────
info "Checking for new Python dependencies..."
sudo -u "${APP_USER}" pip3 install \
    --user \
    --break-system-packages \
    --quiet \
    -r "${SRC_DIR}/requirements.txt"

# ── 5. Copy updated files to app directory ────────────────────────────────────
info "Copying files to ${APP_DIR}..."
cp -r "${SRC_DIR}"/. "${APP_DIR}"/
chown -R root:"${APP_USER}" "${APP_DIR}"
chmod -R 750 "${APP_DIR}"

# ── 6. Verify nginx config ────────────────────────────────────────────────────
info "Verifying nginx configuration..."
if nginx -t 2>/dev/null; then
    check "nginx config is valid ✓"
else
    warn "nginx config test failed — check /etc/nginx/conf.d/nettrack.conf"
    warn "nginx will NOT be reloaded"
fi

# ── 7. Run database migrations ────────────────────────────────────────────────
info "Running database migrations..."
cd "${APP_DIR}"
set -a; source "${ENV_FILE}"; set +a
export PATH="/home/nettrack/.local/bin:${PATH}"

MIGRATION_OUTPUT=$(sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    "${ALEMBIC}" upgrade head 2>&1)
echo "${MIGRATION_OUTPUT}"

if echo "${MIGRATION_OUTPUT}" | grep -q "ERROR\|error"; then
    error "Migration failed — app NOT restarted. Restore from backup: cp -r ${BACKUP_PATH}/. ${APP_DIR}/"
fi

# Verify we're at head
CURRENT_REV=$(sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    "${ALEMBIC}" current 2>/dev/null | grep -o '[a-f0-9]*' | head -1)
check "Database at revision: ${CURRENT_REV} ✓"

# ── 8. Restart service ────────────────────────────────────────────────────────
info "Restarting NetTrack service..."
systemctl restart nettrack
sleep 3

if ! systemctl is-active --quiet nettrack; then
    error "Service failed to start — restoring backup automatically..."
    cp -r "${BACKUP_PATH}"/. "${APP_DIR}"/
    systemctl restart nettrack
    sleep 2
    systemctl is-active --quiet nettrack \
        && warn "Restored from backup successfully. Check the error and try again." \
        || error "Restore also failed — check: journalctl -u nettrack -n 50"
    exit 1
fi
check "NetTrack service is running ✓"

# ── 9. Test API responds ──────────────────────────────────────────────────────
info "Testing API health endpoint..."
sleep 1
API_RESPONSE=$(curl -sk https://localhost/api/health 2>/dev/null || echo "")
if echo "${API_RESPONSE}" | grep -q '"ok"'; then
    check "API is responding: ${API_RESPONSE} ✓"
else
    warn "API health check failed — got: ${API_RESPONSE}"
    warn "Service is running but API may not be ready yet"
    warn "Check: journalctl -u nettrack -n 20"
fi

# ── 10. Fix frontend permissions for nginx ────────────────────────────────────
info "Setting frontend permissions..."
chmod o+rx "${APP_DIR}" "${APP_DIR}/frontend"
find "${APP_DIR}/frontend" -type f -exec chmod o+r {} \;
nginx -t 2>/dev/null && systemctl reload nginx || true

# ── 11. Generate scanner API token if not set ─────────────────────────────────
CURRENT_TOKEN=$(grep "^SCANNER_API_TOKEN=" "${ENV_FILE}" | cut -d= -f2)
if [[ -z "${CURRENT_TOKEN}" ]]; then
    info "Generating scanner API token..."
    SCANNER_TOKEN=$(sudo -u "${APP_USER}" --preserve-env=SECRET_KEY,PYTHONPATH \
        python3 -c "
import sys, os
sys.path.insert(0, '${APP_DIR}')
from datetime import datetime, timezone, timedelta
from jose import jwt
token = jwt.encode(
    {'sub': '1', 'role': 'modify', 'type': 'access',
     'exp': datetime.now(timezone.utc) + timedelta(days=3650)},
    os.environ['SECRET_KEY'], algorithm='HS256'
)
print(token)
" 2>/dev/null || true)
    if [[ -n "${SCANNER_TOKEN}" ]]; then
        sed -i "s|^SCANNER_API_TOKEN=.*|SCANNER_API_TOKEN=${SCANNER_TOKEN}|" "${ENV_FILE}"
        info "Scanner API token generated ✓"
        systemctl restart nettrack
        sleep 2
    else
        warn "Could not generate scanner token — set SCANNER_API_TOKEN manually in ${ENV_FILE}"
    fi
else
    check "Scanner API token already set ✓"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  NetTrack update complete                            ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Version: $(cd ${SRC_DIR} && git log -1 --format='%h %s (%cr)')"
echo -e "  Backup:  ${BACKUP_PATH}"
echo -e "  Logs:    journalctl -u nettrack -f"
echo ""

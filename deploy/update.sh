#!/usr/bin/env bash
# NetTrack update script
# Pulls latest code from git, runs sanity checks, backs up, deploys, verifies.
#
# Usage:
#   bash /opt/nettrack-src/deploy/update.sh
#
# Run as root.

set -euo pipefail

SRC_DIR=/opt/nettrack-src
APP_DIR=/opt/nettrack
ENV_FILE=/etc/nettrack/nettrack.env
BACKUP_DIR=/opt/nettrack-backups
ALEMBIC=/home/nettrack/.local/bin/alembic
APP_USER=nettrack

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${GREEN}[+]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
check()   { echo -e "${BLUE}[?]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }

ERRORS=0
fail() { echo -e "${RED}[✗]${NC} $*"; ERRORS=$((ERRORS+1)); }

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
    info "${CHANGES} new commit(s):"
    git log HEAD..origin/main --oneline
fi
git pull origin main

# ── 2. Sanity check: verify key files ────────────────────────────────────────
check "Verifying key files..."

if grep -q "class NetworkScanner" "${SRC_DIR}/scanner/scanner.py" 2>/dev/null; then
    success "scanner/scanner.py contains NetworkScanner ✓"
else
    fail "scanner/scanner.py does NOT contain NetworkScanner — wrong file!"
fi

if grep -q "APIRouter" "${SRC_DIR}/routers/scanner.py" 2>/dev/null; then
    success "routers/scanner.py contains APIRouter ✓"
else
    fail "routers/scanner.py does NOT contain APIRouter — wrong file!"
fi

if grep -q "_accessToken" "${SRC_DIR}/frontend/index.html" 2>/dev/null; then
    success "frontend/index.html has auth token fix ✓"
else
    fail "frontend/index.html missing _accessToken — login may not work!"
fi

if grep -q "secure=False" "${SRC_DIR}/routers/users.py" 2>/dev/null; then
    success "routers/users.py has cookie fix ✓"
else
    fail "routers/users.py missing secure=False — login may not work!"
fi

if [[ $ERRORS -gt 0 ]]; then
    error "${ERRORS} sanity check(s) failed. Aborting deploy to protect your running instance."
fi

# ── 3. Sanity check: Python syntax ───────────────────────────────────────────
check "Running Python syntax checks..."
SYNTAX_ERRORS=0
while IFS= read -r -d '' pyfile; do
    if ! python3 -c "
import ast, sys
try:
    ast.parse(open('${pyfile}').read())
except SyntaxError as e:
    print(f'Syntax error in ${pyfile}: {e}')
    sys.exit(1)
" 2>/dev/null; then
        fail "Syntax error in: ${pyfile}"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS+1))
    fi
done < <(find "${SRC_DIR}" -maxdepth 3 -name "*.py" -not -path "*/__pycache__/*" -print0)

if [[ $SYNTAX_ERRORS -eq 0 ]]; then
    success "All Python files pass syntax check ✓"
else
    error "${SYNTAX_ERRORS} Python syntax error(s) found. Aborting."
fi

# ── 4. Sanity check: nginx config ────────────────────────────────────────────
check "Verifying nginx configuration..."
SKIP_NGINX=0
if nginx -t 2>/dev/null; then
    success "nginx config is valid ✓"
else
    warn "nginx config has errors — will not restart nginx after deploy"
    SKIP_NGINX=1
fi

# ── 5. Backup current app ────────────────────────────────────────────────────
info "Backing up current app..."
mkdir -p "${BACKUP_DIR}"
BACKUP_NAME="nettrack-backup-$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"
cp -r "${APP_DIR}" "${BACKUP_PATH}"
success "Backup saved to ${BACKUP_PATH}"

# Keep only last 5 backups
ls -dt "${BACKUP_DIR}"/nettrack-backup-* 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null || true

# ── 6. Install Python dependencies ───────────────────────────────────────────
info "Checking for new Python dependencies..."
sudo -u "${APP_USER}" pip3 install \
    --user --break-system-packages --quiet \
    -r "${SRC_DIR}/requirements.txt"

# ── 7. Copy files ─────────────────────────────────────────────────────────────
info "Copying files to ${APP_DIR}..."
cp -r "${SRC_DIR}"/. "${APP_DIR}"/
chown -R root:"${APP_USER}" "${APP_DIR}"
chmod -R 750 "${APP_DIR}"

# ── 8. Run migrations ────────────────────────────────────────────────────────
info "Running database migrations..."
cd "${APP_DIR}"
set -a; source "${ENV_FILE}"; set +a
export PATH="/home/nettrack/.local/bin:${PATH}"

sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    "${ALEMBIC}" upgrade head

AFTER_REV=$(sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    "${ALEMBIC}" current 2>/dev/null || echo "")
if echo "${AFTER_REV}" | grep -q "(head)"; then
    success "Database migrations up to date ✓"
else
    warn "Could not verify migration state — check: alembic current"
fi

# ── 9. Restart service ───────────────────────────────────────────────────────
info "Restarting NetTrack service..."
systemctl restart nettrack
sleep 3

if ! systemctl is-active --quiet nettrack; then
    warn "Service failed — rolling back to ${BACKUP_NAME}..."
    cp -r "${BACKUP_PATH}"/. "${APP_DIR}"/
    chown -R root:"${APP_USER}" "${APP_DIR}"
    systemctl restart nettrack
    sleep 2
    systemctl is-active --quiet nettrack \
        && warn "Rolled back successfully" \
        || error "Rollback failed — check: journalctl -u nettrack -n 50"
    error "Deploy failed — rolled back to previous version"
fi
success "NetTrack service is running ✓"

# ── 10. API health check ──────────────────────────────────────────────────────
check "Testing API health..."
sleep 1
HEALTH=$(curl -sk https://localhost/api/health 2>/dev/null || echo "")
if echo "${HEALTH}" | grep -q '"ok"'; then
    success "API health check passed ✓"
else
    warn "API health check failed — response: ${HEALTH:-no response}"
    warn "Service may still be starting up. Try: curl -sk https://localhost/api/health"
fi

# ── 11. Frontend permissions ──────────────────────────────────────────────────
info "Setting frontend permissions..."
chmod o+rx "${APP_DIR}" "${APP_DIR}/frontend"
find "${APP_DIR}/frontend" -type f -exec chmod o+r {} \;

# ── 12. Reload nginx ─────────────────────────────────────────────────────────
if [[ "${SKIP_NGINX}" -eq 0 ]]; then
    nginx -t 2>/dev/null && systemctl reload nginx && success "nginx reloaded ✓"
fi

# ── 13. Scanner API token ────────────────────────────────────────────────────
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
        success "Scanner API token generated ✓"
        systemctl restart nettrack
        sleep 2
    else
        warn "Could not generate scanner token — set SCANNER_API_TOKEN manually in ${ENV_FILE}"
    fi
else
    success "Scanner API token already set ✓"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  NetTrack update complete                            ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Version: $(cd ${SRC_DIR} && git log -1 --format='%h %s (%cr)')"
echo -e "  Backup:  ${BACKUP_PATH}"
echo -e "  Logs:    journalctl -u nettrack -f"
echo ""

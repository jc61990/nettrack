#!/usr/bin/env bash
# NetTrack update script
# Pulls latest code from git, copies to app dir, runs migrations, restarts service.
#
# Usage:
#   bash /opt/nettrack-src/deploy/update.sh
#
# Run as root or a user with sudo access.

set -euo pipefail

SRC_DIR=/opt/nettrack-src
APP_DIR=/opt/nettrack
ENV_FILE=/etc/nettrack/nettrack.env
ALEMBIC=/home/nettrack/.local/bin/alembic
APP_USER=nettrack

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root"
[[ ! -d "${SRC_DIR}/.git" ]] && error "${SRC_DIR} is not a git repository"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
info "Pulling latest code from git..."
cd "${SRC_DIR}"
git fetch origin
CHANGES=$(git log HEAD..origin/main --oneline 2>/dev/null | wc -l)

if [[ "${CHANGES}" -eq 0 ]]; then
    warn "Already up to date — no new commits. Running deploy steps anyway..."
fi

info "$(git log HEAD..origin/main --oneline)"
git pull origin main

# ── 2. Install any new Python dependencies ────────────────────────────────────
info "Checking for new Python dependencies..."
sudo -u "${APP_USER}" pip3 install \
    --user \
    --break-system-packages \
    --quiet \
    -r "${SRC_DIR}/requirements.txt"

# ── 3. Copy updated files to app directory ────────────────────────────────────
info "Copying files to ${APP_DIR}..."
cp -r "${SRC_DIR}"/. "${APP_DIR}"/
chown -R root:"${APP_USER}" "${APP_DIR}"
chmod -R 750 "${APP_DIR}"

# ── 4. Run database migrations ────────────────────────────────────────────────
info "Running database migrations..."
cd "${APP_DIR}"
set -a; source "${ENV_FILE}"; set +a
export PATH="/home/nettrack/.local/bin:${PATH}"

PENDING=$(sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    "${ALEMBIC}" heads --verbose 2>/dev/null | grep -c "(head)" || true)

sudo -u "${APP_USER}" --preserve-env=DATABASE_URL,SECRET_KEY,PATH \
    "${ALEMBIC}" upgrade head

# ── 5. Restart service ────────────────────────────────────────────────────────
info "Restarting NetTrack service..."
systemctl restart nettrack
sleep 2
systemctl is-active --quiet nettrack \
    && info "NetTrack restarted successfully ✓" \
    || error "Service failed to restart — check: journalctl -u nettrack -n 50"

# ── Fix frontend permissions for nginx ───────────────────────────────────────
info "Setting frontend permissions..."
chmod o+rx "${APP_DIR}" "${APP_DIR}/frontend"
find "${APP_DIR}/frontend" -type f -exec chmod o+r {} \;

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Update complete.${NC}"
echo -e "  Version: $(cd ${SRC_DIR} && git log -1 --format='%h %s (%cr)')"
echo -e "  Logs:    journalctl -u nettrack -f"
echo ""

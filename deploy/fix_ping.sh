#!/usr/bin/env bash
# NetTrack — ping permission fix for RHEL/CentOS
#
# On RHEL, non-root processes cannot send raw ICMP by default.
# This script tests which approach works on your system and applies it.
#
# Strategies (tried in order):
#   1. Existing setuid ping     — works on most RHEL 8+ systems out of the box
#   2. cap_net_raw on Python    — grants ICMP to the venv Python binary
#   3. nmap fallback            — uses nmap -sn instead of ping (no raw socket needed)
#
# Run as root:  bash deploy/fix_ping.sh

set -euo pipefail

APP_DIR=/opt/nettrack
APP_USER=nettrack
PYTHON="${APP_DIR}/venv/bin/python3"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root"
[[ ! -f "${PYTHON}" ]] && error "Python not found at ${PYTHON} — run setup.sh first"

# ── Strategy 1: test existing setuid ping ────────────────────────────────────
info "Testing ping as '${APP_USER}'..."
if su -s /bin/bash "${APP_USER}" -c "ping -c1 -W1 127.0.0.1" &>/dev/null; then
    info "ping works without changes ✓"
    info "No action needed — your system's ping is already accessible."
    exit 0
fi
warn "ping fails as '${APP_USER}' — applying fix..."

# ── Strategy 2: cap_net_raw on the venv Python binary ────────────────────────
info "Trying cap_net_raw on ${PYTHON}..."
if command -v setcap &>/dev/null; then
    setcap cap_net_raw+ep "${PYTHON}"

    # Verify it works now
    if su -s /bin/bash "${APP_USER}" -c "ping -c1 -W1 127.0.0.1" &>/dev/null; then
        info "cap_net_raw applied and verified ✓"
        echo ""
        echo -e "${YELLOW}  Note:${NC} If you upgrade Python or rebuild the venv, re-run this script."
        echo -e "  Check capability:  getcap ${PYTHON}"
        echo -e "  Remove if needed:  setcap -r ${PYTHON}"
        exit 0
    else
        warn "cap_net_raw applied but ping still fails — removing and trying fallback..."
        setcap -r "${PYTHON}" 2>/dev/null || true
    fi
else
    warn "setcap not available — skipping cap_net_raw strategy"
fi

# ── Strategy 3: nmap fallback ─────────────────────────────────────────────────
warn "Falling back to nmap-based host discovery..."
if ! command -v nmap &>/dev/null; then
    info "Installing nmap..."
    dnf install -y nmap
fi

if ! command -v nmap &>/dev/null; then
    error "nmap install failed. Install manually: dnf install -y nmap"
fi

# Verify nmap works as the app user (uses TCP SYN, no raw ICMP needed)
if su -s /bin/bash "${APP_USER}" -c "nmap -sn -T4 127.0.0.1" &>/dev/null; then
    info "nmap is available and works as '${APP_USER}' ✓"
    info "Patching scanner to use nmap for host discovery..."

    # Patch scanner.py to use nmap instead of ping
    python3 << 'PYEOF'
import re

path = '/opt/nettrack/scanner/scanner.py'
with open(path) as f:
    content = f.read()

old_ping = '''def ping_host(ip: str, timeout_ms: int = 800) -> bool:
    """Returns True if host responds to ICMP ping."""
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(max(1, timeout_ms // 1000)), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_ms / 1000 + 1,
        )
        return result.returncode == 0
    except Exception:
        return False'''

new_ping = '''def ping_host(ip: str, timeout_ms: int = 800) -> bool:
    """
    Returns True if the host is reachable.
    Tries ICMP ping first; falls back to nmap -sn if ping fails due to permissions.
    nmap uses TCP SYN probes and does not require raw socket privileges.
    """
    import shutil
    timeout_s = max(1, timeout_ms // 1000)

    # Try standard ping first
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(timeout_s), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_s + 1,
        )
        if result.returncode == 0:
            return True
        # returncode 1 = no reply, 2 = permission error — fall through to nmap
        if result.returncode != 2:
            return False
    except Exception:
        pass

    # Fallback: nmap ping scan (no raw socket required)
    if shutil.which('nmap'):
        try:
            result = subprocess.run(
                ['nmap', '-sn', '-T4', '--host-timeout', f'{timeout_s}s', ip],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout_s + 2,
            )
            return b'Host is up' in result.stdout
        except Exception:
            pass

    return False'''

if old_ping in content:
    content = content.replace(old_ping, new_ping)
    with open(path, 'w') as f:
        f.write(content)
    print("scanner.py patched to use nmap fallback ✓")
else:
    print("ping_host function not found in expected form — patch scanner.py manually")
    print("Replace the ping_host function with one that calls:")
    print("  nmap -sn -T4 --host-timeout <timeout>s <ip>")
    print("  and checks stdout for 'Host is up'")
PYEOF

    info "Restarting NetTrack service..."
    systemctl restart nettrack

    echo ""
    echo -e "${GREEN}Done.${NC} Scanner will use nmap for host discovery."
    echo -e "  nmap path:  $(command -v nmap)"
    echo -e "  Verify:     su -s /bin/bash ${APP_USER} -c 'nmap -sn 127.0.0.1'"
    exit 0
fi

# ── All strategies failed ─────────────────────────────────────────────────────
error "All strategies failed. Manual options:
  1. Run the scanner as root (not recommended)
  2. Add '${APP_USER}' to a group with ping access:
       groupadd -r pingusers
       usermod -aG pingusers ${APP_USER}
       chmod g+s /usr/bin/ping
  3. Check SELinux: ausearch -m avc | grep ping
     Then: semanage permissive -a nettrack_t  (temporary, for diagnosis)"

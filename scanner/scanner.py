"""
NetTrack network scanner — core discovery engine.

Discovery pipeline per subnet:
  1. Ping sweep (concurrent ICMP)        → live hosts
  2. ARP table collection (from switches) → MAC → IP mapping
  3. SNMP queries on live hosts           → sysDescr, sysName
  4. LLDP neighbor walk on switches       → port-level topology
  5. MAC bridge table walk on switches    → non-LLDP devices
  6. OUI lookup                           → vendor from MAC prefix
  7. Merge + emit results
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Generator, Optional

import httpx
import requests

from scanner.config import ScannerConfig, SubnetConfig, load_config
from scanner.lldp import get_lldp_neighbors, get_mac_port_table
from scanner.snmp import SNMPClient

log = logging.getLogger(__name__)

# ── OUI database (trimmed — extend or swap for a full offline DB) ─────────────
# Full lookup: pip install manuf   →  from manuf import MacParser
OUI_PREFIXES = {
    '00:50:56': 'VMware',
    '00:0C:29': 'VMware',
    'AA:BB:CC': 'Demo Device',
    'B8:27:EB': 'Raspberry Pi',
    'DC:A6:32': 'Raspberry Pi',
    '00:1A:11': 'Google',
    'FC:EC:DA': 'Ubiquiti',
    '24:A4:3C': 'Ubiquiti',
    '00:18:0A': 'Cisco',
    '00:1B:D4': 'Cisco',
    '58:AC:78': 'Cisco',
    'A0:EC:F9': 'Cisco',
    '00:1E:13': 'Cisco',
    '00:26:CB': 'Cisco',
    'E4:AA:5D': 'HID Global',
    '00:06:8E': 'HID Global',
    '9C:8E:CD': 'Hikvision',
    'C4:2F:90': 'Hikvision',
    '00:12:17': 'Axis Communications',
    'AC:CC:8E': 'Axis Communications',
    '40:8D:5C': 'Dahua',
    '70:85:C2': 'Dahua',
    '00:17:C8': '2N Telekomunikace',
    '70:EE:50': 'HP Enterprise',
    '3C:D9:2B': 'HP Enterprise',
    '10:02:B5': 'HP',
    '00:21:5A': 'HP',
}

try:
    from manuf import MacParser
    _mac_parser = MacParser()
    def oui_lookup(mac: str) -> str:
        result = _mac_parser.get_manuf(mac)
        return result or _oui_prefix_lookup(mac)
except ImportError:
    def oui_lookup(mac: str) -> str:
        return _oui_prefix_lookup(mac)

def _oui_prefix_lookup(mac: str) -> str:
    prefix = mac.upper()[:8]
    return OUI_PREFIXES.get(prefix, '')


# ── Device type inference ─────────────────────────────────────────────────────

VENDOR_TYPE_MAP = {
    'hikvision':  'Camera',
    'axis':       'Camera',
    'dahua':      'Camera',
    'hanwha':     'Camera',
    'bosch':      'Camera',
    'hid':        'Access Control',
    'lenel':      'Access Control',
    'assa abloy': 'Access Control',
    '2n':         'Intercom',
    'algo':       'Intercom',
    'cisco':      'Switch',
    'hp enterprise': 'Switch',
    'aruba':      'WAP',
    'ubiquiti':   'WAP',
    'ruckus':     'WAP',
    'meraki':     'WAP',
}

def infer_type(vendor: str, sys_descr: str, hostname: str) -> str:
    combined = (vendor + ' ' + sys_descr + ' ' + hostname).lower()
    for keyword, device_type in VENDOR_TYPE_MAP.items():
        if keyword in combined:
            return device_type
    if any(k in combined for k in ['switch', 'catalyst', 'nexus', 'ios']):
        return 'Switch'
    if any(k in combined for k in ['access point', 'wireless', ' ap ']):
        return 'WAP'
    if any(k in combined for k in ['printer', 'laserjet', 'officejet']):
        return 'Printer'
    if any(k in combined for k in ['server', 'linux', 'windows server']):
        return 'Server'
    return 'Other'


# ── Ping sweep ────────────────────────────────────────────────────────────────

def ping_host(ip: str, timeout_ms: int = 800) -> bool:
    """
    Returns True if the host is reachable.

    Tries ICMP ping first. If ping fails with a permission error (exit code 2),
    falls back to nmap -sn which uses TCP SYN probes and requires no raw socket
    privileges. This makes the scanner work on RHEL without cap_net_raw or
    setuid ping.
    """
    import shutil
    timeout_s = max(1, timeout_ms // 1000)

    # Strategy 1: standard ICMP ping
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(timeout_s), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_s + 1,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False   # host unreachable, not a permissions issue
        # returncode 2 typically means operation not permitted — fall through
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        pass

    # Strategy 2: nmap ping scan (no raw socket needed)
    if shutil.which('nmap'):
        try:
            result = subprocess.run(
                ['nmap', '-sn', '-T4', '--host-timeout', f'{timeout_s}s', ip],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout_s + 3,
            )
            return b'Host is up' in result.stdout
        except Exception:
            pass

    log.warning(
        f"Cannot ping {ip}: no working ping method found. "
        "Run deploy/fix_ping.sh or install nmap."
    )
    return False

def ping_sweep(subnet: str, timeout_ms: int = 800, workers: int = 64,
               progress_cb: Optional[Callable[[str, bool], None]] = None) -> list[str]:
    """Ping all hosts in subnet concurrently. Returns list of live IPs."""
    network = ipaddress.ip_network(subnet, strict=False)
    hosts   = list(network.hosts())
    live    = []

    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as ex:
        futures = {ex.submit(ping_host, str(h), timeout_ms): str(h) for h in hosts}
        for future in as_completed(futures):
            ip   = futures[future]
            up   = future.result()
            if up:
                live.append(ip)
            if progress_cb:
                progress_cb(ip, up)
    return live


# ── Reverse DNS ───────────────────────────────────────────────────────────────

def reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ''


# ── Main scanner ──────────────────────────────────────────────────────────────

def check_ping_capability() -> dict:
    """
    Check what host discovery methods are available on this system.
    Returns a dict with status info — called at startup and surfaced via /api/scan/status.
    """
    import shutil, subprocess
    result = {'ping': False, 'nmap': False, 'method': None, 'warning': None}

    try:
        r = subprocess.run(['ping', '-c', '1', '-W', '1', '127.0.0.1'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        result['ping'] = (r.returncode == 0)
    except Exception:
        pass

    result['nmap'] = bool(shutil.which('nmap'))

    if result['ping']:
        result['method'] = 'ping'
    elif result['nmap']:
        result['method'] = 'nmap'
        result['warning'] = (
            'ICMP ping unavailable — using nmap for host discovery. '
            'Run deploy/fix_ping.sh for the fastest scan performance.'
        )
    else:
        result['method'] = None
        result['warning'] = (
            'Neither ping nor nmap is available to the nettrack user. '
            'Run deploy/fix_ping.sh to fix this before scanning.'
        )
    return result


class NetworkScanner:
    def __init__(self, config: Optional[ScannerConfig] = None):
        self.config = config or load_config()

    def _snmp_client(self, ip: str, subnet_cfg: SubnetConfig) -> SNMPClient:
        v3 = subnet_cfg.snmp_v3 or self.config.snmp_v3
        return SNMPClient(
            host=ip,
            port=self.config.snmp_port,
            timeout=self.config.snmp_timeout,
            retries=self.config.snmp_retries,
            community=subnet_cfg.snmp_community or self.config.snmp_community,
            v3_username=v3.username if v3 else None,
            v3_auth_key=v3.auth_key if v3 else None,
            v3_priv_key=v3.priv_key if v3 else None,
            v3_auth_protocol=v3.auth_protocol if v3 else 'sha',
            v3_priv_protocol=v3.priv_protocol if v3 else 'aes',
        )

    def scan(self, subnets: Optional[list[str]] = None,
             emit: Optional[Callable[[str, str], None]] = None) -> list[dict]:
        """
        Run a full network scan.

        Args:
            subnets: Override subnet list (uses config if None)
            emit(level, message): Callback for streaming log lines to UI
                level: 'info' | 'ok' | 'warn'

        Returns:
            List of discovered device dicts ready for bulk-upsert.
        """
        def log_emit(level: str, msg: str):
            log.info(msg)
            if emit:
                emit(level, msg)

        target_subnets = subnets or [s.cidr for s in self.config.subnets]
        if not target_subnets:
            log_emit('warn', '[!] No subnets configured. Aborting.')
            return []

        subnet_map = {s.cidr: s for s in self.config.subnets}
        discovered: dict[str, dict] = {}   # mac → device dict
        switch_ips: list[str] = []

        # ── Phase 1: Ping sweep ───────────────────────────────────────────────
        log_emit('info', f'[+] Starting scan of {len(target_subnets)} subnet(s)')
        all_live: list[tuple[str, str]] = []   # (ip, subnet_cidr)

        for cidr in target_subnets:
            log_emit('info', f'[~] Ping sweep: {cidr}')
            count = {'up': 0, 'down': 0}

            def ping_cb(ip, up, c=count):
                if up: c['up'] += 1
                else:  c['down'] += 1

            live = ping_sweep(cidr, self.config.ping_timeout_ms, self.config.ping_workers, ping_cb)
            log_emit('ok', f'[✓] {cidr}: {len(live)} hosts up')
            all_live.extend((ip, cidr) for ip in live)

        if not all_live:
            log_emit('warn', '[!] No hosts responded to ping. Check network access.')
            return []

        # ── Phase 2: Reverse DNS + SNMP on live hosts ─────────────────────────
        log_emit('info', f'[~] Querying {len(all_live)} live hosts via SNMP...')
        for ip, cidr in all_live:
            subnet_cfg = subnet_map.get(cidr, self._default_subnet(cidr))
            client     = self._snmp_client(ip, subnet_cfg)
            device     = {'ip': ip, 'status': 'Online', 'last_seen': datetime.now(timezone.utc).isoformat()}

            # Reverse DNS
            hostname = reverse_dns(ip)
            if hostname:
                device['hostname'] = hostname

            # SNMP
            try:
                sys_info = client.get_sys_info()
                if sys_info:
                    device['snmp_descr'] = sys_info.get('sys_descr', '')
                    device['snmp_name']  = sys_info.get('sys_name', '')
                    if not device.get('hostname') and device['snmp_name']:
                        device['hostname'] = device['snmp_name']

                    # Identify switches for LLDP/ARP walk
                    descr_lower = device['snmp_descr'].lower()
                    if any(k in descr_lower for k in ['ios', 'nexus', 'catalyst', 'switch']):
                        switch_ips.append(ip)
                    log_emit('ok', f'[✓] SNMP {ip}: {device["snmp_descr"][:60]}')
                else:
                    log_emit('info', f'[~] SNMP {ip}: no response')
            except Exception as e:
                log_emit('warn', f'[!] SNMP {ip}: {e}')

            # Try to get MAC from SNMP interfaces
            try:
                ifaces = client.get_interfaces()
                for iface in ifaces:
                    if iface.get('mac') and iface['mac'] != '00:00:00:00:00:00':
                        device['mac'] = iface['mac']
                        break
            except Exception:
                pass

            mac = device.get('mac', ip)   # use IP as key if no MAC yet
            discovered[mac] = device

        # ── Phase 3: ARP table from switches ─────────────────────────────────
        log_emit('info', f'[~] Collecting ARP tables from {len(switch_ips)} switch(es)...')
        for sw_ip in switch_ips:
            subnet_cfg = self._find_subnet_cfg(sw_ip, subnet_map)
            client     = self._snmp_client(sw_ip, subnet_cfg)
            try:
                arp_entries = client.get_arp_table()
                for entry in arp_entries:
                    mac = entry.get('mac', '')
                    ip  = entry.get('ip', '')
                    if mac and ip:
                        existing = discovered.get(mac)
                        if existing:
                            existing.setdefault('ip', ip)
                        else:
                            discovered[mac] = {'ip': ip, 'mac': mac, 'status': 'Unknown'}
                log_emit('ok', f'[✓] ARP {sw_ip}: {len(arp_entries)} entries')
            except Exception as e:
                log_emit('warn', f'[!] ARP {sw_ip}: {e}')

        # ── Phase 4: LLDP neighbor walk on switches ───────────────────────────
        log_emit('info', f'[~] Collecting LLDP neighbors from {len(switch_ips)} switch(es)...')
        port_map: dict[str, dict] = {}  # mac → {switch, port}

        for sw_ip in switch_ips:
            sw_name    = next((d.get('hostname', sw_ip) for d in discovered.values() if d.get('ip') == sw_ip), sw_ip)
            subnet_cfg = self._find_subnet_cfg(sw_ip, subnet_map)
            client     = self._snmp_client(sw_ip, subnet_cfg)
            try:
                neighbors = get_lldp_neighbors(client)
                ifaces    = {i['index']: i.get('descr', f'Port{i["index"]}') for i in client.get_interfaces()}
                for n in neighbors:
                    port_name = ifaces.get(n['local_port_idx'], f'Port{n["local_port_idx"]}')
                    chassis   = n.get('remote_chassis', '')
                    sys_name  = n.get('remote_sys_name', '')
                    mgmt_ip   = n.get('remote_mgmt_ip', '')
                    log_emit('ok', f'[✓] LLDP: {sys_name or chassis} → {sw_name} {port_name}')

                    # Match by MAC or management IP
                    matched_mac = None
                    for mac, dev in discovered.items():
                        if (chassis and mac.upper() == chassis.upper()) or \
                           (mgmt_ip and dev.get('ip') == mgmt_ip):
                            matched_mac = mac
                            break
                    if matched_mac:
                        discovered[matched_mac].update({
                            'switch': sw_name, 'port': port_name,
                            'hostname': discovered[matched_mac].get('hostname') or sys_name,
                        })
                    else:
                        port_map[chassis] = {'switch': sw_name, 'port': port_name, 'hostname': sys_name}

                log_emit('ok', f'[✓] LLDP {sw_name}: {len(neighbors)} neighbors')
            except Exception as e:
                log_emit('warn', f'[!] LLDP {sw_ip}: {e}')

        # ── Phase 5: MAC bridge table for non-LLDP devices ───────────────────
        log_emit('info', '[~] Collecting MAC bridge tables...')
        for sw_ip in switch_ips:
            sw_name    = next((d.get('hostname', sw_ip) for d in discovered.values() if d.get('ip') == sw_ip), sw_ip)
            subnet_cfg = self._find_subnet_cfg(sw_ip, subnet_map)
            client     = self._snmp_client(sw_ip, subnet_cfg)
            try:
                mac_entries = get_mac_port_table(client)
                ifaces      = {i['index']: i.get('descr', '') for i in client.get_interfaces()}
                for entry in mac_entries:
                    mac      = entry['mac']
                    port_idx = entry.get('port_idx')
                    port_name = ifaces.get(port_idx, f'Port{port_idx}')
                    if mac in discovered and not discovered[mac].get('switch'):
                        discovered[mac].update({'switch': sw_name, 'port': port_name})
            except Exception as e:
                log_emit('warn', f'[!] MAC table {sw_ip}: {e}')

        # ── Phase 6: OUI vendor lookup + type inference ───────────────────────
        log_emit('info', '[~] Running OUI vendor lookup...')
        for mac, device in discovered.items():
            vendor = oui_lookup(mac) if ':' in mac else ''
            if vendor:
                device['vendor'] = vendor
                log_emit('ok', f'[✓] {mac[:8]}:xx → {vendor}')
            if not device.get('type'):
                device['type'] = infer_type(
                    vendor,
                    device.get('snmp_descr', ''),
                    device.get('hostname', ''),
                )

        # ── Finalise ──────────────────────────────────────────────────────────
        results = []
        for mac, device in discovered.items():
            if ':' in mac:
                device['mac'] = mac
            results.append({
                'hostname': device.get('hostname') or device.get('ip', ''),
                'ip':       device.get('ip', ''),
                'mac':      device.get('mac', ''),
                'type':     device.get('type', 'Other'),
                'status':   device.get('status', 'Unknown'),
                'switch':   device.get('switch', ''),
                'port':     device.get('port', ''),
                'notes':    device.get('vendor', ''),
            })

        log_emit('ok', f'[✓] Scan complete. {len(results)} devices discovered.')
        return results

    def _default_subnet(self, cidr: str):
        from scanner.config import SubnetConfig
        return SubnetConfig(cidr=cidr, snmp_community=self.config.snmp_community, snmp_v3=self.config.snmp_v3)

    def _find_subnet_cfg(self, ip: str, subnet_map: dict):
        addr = ipaddress.ip_address(ip)
        for cidr, cfg in subnet_map.items():
            if addr in ipaddress.ip_network(cidr, strict=False):
                return cfg
        return self._default_subnet('0.0.0.0/0')


# ── Results persistence ───────────────────────────────────────────────────────

def save_results(results: list[dict], results_dir: str) -> str:
    os.makedirs(results_dir, exist_ok=True)
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    filepath = os.path.join(results_dir, f'scan_{ts}.json')
    with open(filepath, 'w') as f:
        json.dump({'scanned_at': ts, 'device_count': len(results), 'devices': results}, f, indent=2, default=str)
    return filepath


def post_results(results: list[dict], api_url: str, token: str,
                  scan_id: str = None) -> bool:
    """POST discovered devices to the discovery queue endpoint."""
    try:
        import uuid as _uuid
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        payload = {
            'devices': results,
            'scan_id': scan_id or str(_uuid.uuid4())[:8],
        }
        res = requests.post(
            f'{api_url.rstrip("/")}/api/queue/ingest',
            json=payload,
            headers=headers,
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
        log.info(
            f'Queue ingest: {data.get("new",0)} new, '
            f'{data.get("changed",0)} changed, '
            f'{data.get("known",0)} known, '
            f'{data.get("blocked",0)} blocked'
        )
        return True
    except Exception as e:
        log.error(f'Failed to POST results to queue: {e}')
        return False

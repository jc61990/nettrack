"""
LLDP neighbor discovery via SNMP (lldpRemTable).

MIB: LLDP-MIB  1.0.8802.1.1.2
  lldpRemChassisId   1.0.8802.1.1.2.1.4.1.1.5
  lldpRemPortId      1.0.8802.1.1.2.1.4.1.1.7
  lldpRemPortDesc    1.0.8802.1.1.2.1.4.1.1.8
  lldpRemSysName     1.0.8802.1.1.2.1.4.1.1.9
  lldpRemSysDesc     1.0.8802.1.1.2.1.4.1.1.10
  lldpRemManAddrTable 1.0.8802.1.1.2.1.4.2.1

Returns a list of neighbors:
  {
    local_port_idx: int,   # ifIndex of the local port
    remote_chassis: str,   # MAC or chassis ID of the remote device
    remote_port: str,      # Port ID advertised by remote
    remote_port_desc: str, # Human-readable port description
    remote_sys_name: str,  # Hostname of remote device
    remote_sys_desc: str,  # sysDescr of remote device
    remote_mgmt_ip: str,   # Management IP if available
  }
"""

import logging
from scanner.snmp import SNMPClient

log = logging.getLogger(__name__)

LLDP_REM_BASE = '1.0.8802.1.1.2.1.4.1.1'
LLDP_REM_COLS = {
    '5':  'remote_chassis',
    '7':  'remote_port',
    '8':  'remote_port_desc',
    '9':  'remote_sys_name',
    '10': 'remote_sys_desc',
}
LLDP_MGMT_BASE = '1.0.8802.1.1.2.1.4.2.1.4'


def get_lldp_neighbors(client: SNMPClient) -> list[dict]:
    """
    Walk the lldpRemTable on a switch and return structured neighbor records.
    Each row key is (timeMark, localPortNum, remoteIndex).
    """
    table: dict[tuple, dict] = {}

    try:
        for oid_str, val in client.walk_with_fallback(LLDP_REM_BASE):
            # OID format: 1.0.8802.1.1.2.1.4.1.1.<col>.<timeMark>.<localPort>.<remIdx>
            suffix = oid_str[len(LLDP_REM_BASE):].lstrip('.')
            parts  = suffix.split('.')
            if len(parts) < 4:
                continue
            col, time_mark, local_port, rem_idx = parts[0], parts[1], parts[2], parts[3]
            key = (time_mark, local_port, rem_idx)
            row = table.setdefault(key, {'local_port_idx': int(local_port)})
            field = LLDP_REM_COLS.get(col)
            if field:
                row[field] = str(val).strip()

        # Fetch management IPs separately
        for oid_str, val in client.walk_with_fallback(LLDP_MGMT_BASE):
            suffix = oid_str[len(LLDP_MGMT_BASE):].lstrip('.')
            parts  = suffix.split('.')
            # Format: <addrSubtype>.<addrLen>.<addr octets...>.<timeMark>.<localPort>.<remIdx>
            # For IPv4 (subtype=1, len=4): parts = [1, 4, a, b, c, d, timeMark, localPort, remIdx]
            if len(parts) >= 9 and parts[0] == '1' and parts[1] == '4':
                ip  = '.'.join(parts[2:6])
                key = (parts[6], parts[7], parts[8])
                if key in table:
                    table[key]['remote_mgmt_ip'] = ip

    except Exception as e:
        log.warning(f"LLDP walk failed on {client.host}: {e}")
        return []

    neighbors = list(table.values())
    log.debug(f"{client.host}: found {len(neighbors)} LLDP neighbors")
    return neighbors


def get_mac_port_table(client: SNMPClient) -> list[dict]:
    """
    Walk the dot1dTpFdbTable (bridge MIB) to get MAC → port mappings.
    Returns [{mac: str, port_idx: int, status: str}]
    Useful on switches that don't support LLDP for end devices.

    dot1dTpFdbAddress  1.3.6.1.2.1.17.4.3.1.1
    dot1dTpFdbPort     1.3.6.1.2.1.17.4.3.1.2
    dot1dTpFdbStatus   1.3.6.1.2.1.17.4.3.1.3
    """
    STATUS_MAP = {'1': 'other', '2': 'invalid', '3': 'learned', '4': 'self', '5': 'mgmt'}
    entries: dict[str, dict] = {}

    try:
        # MAC addresses are encoded in the OID itself
        for oid_str, val in client.walk_with_fallback('1.3.6.1.2.1.17.4.3.1.1'):
            mac_octets = oid_str.rsplit('.', 6)[-6:]
            mac = ':'.join(f'{int(o):02X}' for o in mac_octets)
            entries.setdefault(mac, {})['mac'] = mac

        for oid_str, val in client.walk_with_fallback('1.3.6.1.2.1.17.4.3.1.2'):
            mac_octets = oid_str.rsplit('.', 6)[-6:]
            mac = ':'.join(f'{int(o):02X}' for o in mac_octets)
            entries.setdefault(mac, {})['port_idx'] = int(str(val))

        for oid_str, val in client.walk_with_fallback('1.3.6.1.2.1.17.4.3.1.3'):
            mac_octets = oid_str.rsplit('.', 6)[-6:]
            mac = ':'.join(f'{int(o):02X}' for o in mac_octets)
            entries.setdefault(mac, {})['status'] = STATUS_MAP.get(str(val), str(val))

    except Exception as e:
        log.debug(f"MAC table walk failed on {client.host}: {e}")

    return [v for v in entries.values() if v.get('port_idx') and v.get('status') == 'learned']

"""
SNMP helper — tries v3 first, falls back to v2c automatically.

OIDs used:
  sysDescr      1.3.6.1.2.1.1.1.0
  sysName       1.3.6.1.2.1.1.5.0
  ifTable       1.3.6.1.2.1.2.2.1
  ifXTable      1.3.6.1.2.1.31.1.1.1
  dot1dTpFdbTable (bridge MIB)  1.3.6.1.2.1.17.4.3
  lldpRemTable  1.0.8802.1.1.2.1.4.1.1
"""

from pysnmp.hlapi import (
    SnmpEngine, CommunityData, UsmUserData,
    UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity,
    getCmd, nextCmd,
    usmHMACMD5AuthProtocol, usmHMACSHAAuthProtocol,
    usmHMAC128SHA224AuthProtocol, usmHMAC192SHA256AuthProtocol,
    usmDESPrivProtocol, usm3DESEDEPrivProtocol,
    usmAesCfb128Protocol, usmAesCfb192Protocol,
)
from typing import Optional, Generator, Any
import logging

log = logging.getLogger(__name__)

AUTH_PROTOCOLS = {
    'md5':    usmHMACMD5AuthProtocol,
    'sha':    usmHMACSHAAuthProtocol,
    'sha224': usmHMAC128SHA224AuthProtocol,
    'sha256': usmHMAC192SHA256AuthProtocol,
}
PRIV_PROTOCOLS = {
    'des':    usmDESPrivProtocol,
    '3des':   usm3DESEDEPrivProtocol,
    'aes':    usmAesCfb128Protocol,
    'aes128': usmAesCfb128Protocol,
    'aes192': usmAesCfb192Protocol,
}


class SNMPClient:
    def __init__(
        self,
        host: str,
        port: int = 161,
        timeout: int = 2,
        retries: int = 1,
        # v2c
        community: str = 'public',
        # v3
        v3_username: Optional[str] = None,
        v3_auth_key: Optional[str] = None,
        v3_priv_key: Optional[str] = None,
        v3_auth_protocol: str = 'sha',
        v3_priv_protocol: str = 'aes',
    ):
        self.host     = host
        self.port     = port
        self.timeout  = timeout
        self.retries  = retries
        self.community = community
        self.v3_user  = v3_username
        self.v3_auth  = v3_auth_key
        self.v3_priv  = v3_priv_key
        self.v3_auth_proto = AUTH_PROTOCOLS.get(v3_auth_protocol.lower(), usmHMACSHAAuthProtocol)
        self.v3_priv_proto = PRIV_PROTOCOLS.get(v3_priv_protocol.lower(), usmAesCfb128Protocol)
        self._transport = None
        self._engine    = SnmpEngine()
        self._use_v3    = False   # resolved after first successful query

    def _transport_target(self):
        return UdpTransportTarget((self.host, self.port), timeout=self.timeout, retries=self.retries)

    def _v3_auth(self):
        if self.v3_auth and self.v3_priv:
            return UsmUserData(
                self.v3_user,
                authKey=self.v3_auth, privKey=self.v3_priv,
                authProtocol=self.v3_auth_proto,
                privProtocol=self.v3_priv_proto,
            )
        elif self.v3_auth:
            return UsmUserData(self.v3_user, authKey=self.v3_auth, authProtocol=self.v3_auth_proto)
        else:
            return UsmUserData(self.v3_user)

    def _credentials(self, force_v2c=False):
        if not force_v2c and self.v3_user:
            return self._v3_auth()
        return CommunityData(self.community, mpModel=1)   # mpModel=1 → SNMPv2c

    def get(self, *oids: str, force_v2c=False) -> dict:
        """GET one or more OIDs. Returns {oid_str: value}."""
        objects = [ObjectType(ObjectIdentity(o)) for o in oids]
        error_indication, error_status, _, var_binds = next(
            getCmd(
                self._engine,
                self._credentials(force_v2c),
                self._transport_target(),
                ContextData(),
                *objects,
            )
        )
        if error_indication or error_status:
            raise ConnectionError(f"SNMP GET failed on {self.host}: {error_indication or error_status}")
        return {str(vb[0]): vb[1].prettyPrint() for vb in var_binds}

    def walk(self, oid: str, force_v2c=False) -> Generator[tuple[str, Any], None, None]:
        """WALK an OID subtree, yielding (oid_str, value) tuples."""
        for (error_indication, error_status, _, var_binds) in nextCmd(
            self._engine,
            self._credentials(force_v2c),
            self._transport_target(),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if error_indication or error_status:
                break
            for vb in var_binds:
                yield str(vb[0]), vb[1]

    def get_with_fallback(self, *oids: str) -> dict:
        """Try v3, fall back to v2c if v3 fails. Caches result for this session."""
        if self._use_v3 or not self.v3_user:
            try:
                result = self.get(*oids)
                self._use_v3 = True
                return result
            except Exception:
                if not self.community:
                    raise
        # fallback to v2c
        result = self.get(*oids, force_v2c=True)
        log.debug(f"{self.host}: SNMP v3 failed, using v2c")
        return result

    def walk_with_fallback(self, oid: str) -> Generator[tuple[str, Any], None, None]:
        """Walk with v3/v2c fallback."""
        try:
            items = list(self.walk(oid, force_v2c=not self.v3_user))
            yield from items
        except Exception:
            if self.community:
                yield from self.walk(oid, force_v2c=True)

    def get_sys_info(self) -> dict:
        """Fetch sysDescr and sysName in one call."""
        try:
            result = self.get_with_fallback(
                '1.3.6.1.2.1.1.1.0',  # sysDescr
                '1.3.6.1.2.1.1.5.0',  # sysName
            )
            vals = list(result.values())
            return {
                'sys_descr': vals[0] if len(vals) > 0 else '',
                'sys_name':  vals[1] if len(vals) > 1 else '',
            }
        except Exception as e:
            log.debug(f"{self.host}: get_sys_info failed: {e}")
            return {}

    def get_interfaces(self) -> list[dict]:
        """Return list of interfaces with index, name, description, MAC, speed."""
        ifaces = {}
        try:
            for oid, val in self.walk_with_fallback('1.3.6.1.2.1.2.2.1'):   # ifTable
                parts = oid.rsplit('.', 2)
                if len(parts) < 2:
                    continue
                col   = int(parts[-2].split('.')[-1])
                idx   = int(parts[-1])
                iface = ifaces.setdefault(idx, {'index': idx})
                if col == 1:   iface['index']   = int(str(val))
                elif col == 2: iface['descr']   = str(val)
                elif col == 6: iface['mac']     = ':'.join(f'{b:02X}' for b in bytes(val)) if val else ''
                elif col == 5: iface['speed']   = int(str(val))
        except Exception as e:
            log.debug(f"{self.host}: get_interfaces failed: {e}")
        return list(ifaces.values())

    def get_arp_table(self) -> list[dict]:
        """Return ARP table entries as [{ip, mac}]."""
        entries = {}
        try:
            for oid, val in self.walk_with_fallback('1.3.6.1.2.1.4.22.1'):  # ipNetToMediaTable
                parts = oid.rsplit('.', 1)
                col_part = parts[0].split('.')[-1]
                if col_part == '2':   # ipNetToMediaPhysAddress
                    ip = '.'.join(oid.split('.')[-4:])
                    entries.setdefault(ip, {})['mac'] = ':'.join(f'{b:02X}' for b in bytes(val))
                elif col_part == '3': # ipNetToMediaNetAddress
                    ip = str(val)
                    entries.setdefault(ip, {})['ip'] = ip
        except Exception as e:
            log.debug(f"{self.host}: get_arp_table failed: {e}")
        return [{'ip': k, **v} for k, v in entries.items() if 'mac' in v]

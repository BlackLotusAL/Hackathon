"""Server settings, path resolution and organizer IP access rules."""
from dataclasses import dataclass, fields, replace
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Optional

from engine.protocol import json_object

from .arena import parse_peer_url
from .domain import PHASES


CONTEST_ROOT = Path(__file__).resolve().parents[1]
PATH_FIELDS = ('db', 'archive_root', 'maps')
TEXT_FIELDS = ('arena_url', 'admin_user', 'admin_password')


class IPWhitelist:
    def __init__(self, entries):
        if entries is None:
            self.networks = None
            return
        if not isinstance(entries, list):
            raise ValueError('配置 ip_whitelist 须为 IP/CIDR 字符串数组或 null')
        networks = []
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                raise ValueError('配置 ip_whitelist 每项须为 IP 或 CIDR 字符串')
            try:
                networks.append(ip_network(entry, strict=False))
            except ValueError as error:
                raise ValueError('配置 ip_whitelist 包含无效的 IP 或 CIDR') from error
        self.networks = tuple(networks)

    def allows(self, address):
        if self.networks is None:
            return True
        try:
            remote = ip_address(address)
        except ValueError:
            return False
        candidates = [remote]
        if remote.version == 6 and remote.ipv4_mapped is not None:
            candidates.append(remote.ipv4_mapped)
        return any(candidate.version == network.version and candidate in network
                   for candidate in candidates for network in self.networks)


@dataclass(frozen=True)
class Settings:
    db: Optional[Path] = None
    archive_root: Optional[Path] = None
    maps: Optional[Path] = None
    phase: str = 'prelim-ladder'
    host: str = '0.0.0.0'
    port: int = 8082
    ip_whitelist: Optional[list[str]] = None
    arena_url: Optional[str] = None
    admin_user: Optional[str] = None
    admin_password: Optional[str] = None

    @classmethod
    def load(cls, path=None):
        if path is None:
            return cls()
        path = path.expanduser().resolve()
        values = json_object(path.read_text(encoding='utf-8-sig').encode('utf-8'))
        unknown = set(values) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError('配置含未知字段: ' + ', '.join(sorted(unknown)))
        for name in PATH_FIELDS:
            value = values.get(name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f'配置 {name} 须为非空路径字符串')
                base = CONTEST_ROOT if name == 'archive_root' else path.parent
                values[name] = (base / Path(value).expanduser()).resolve()
        settings = cls(**values)
        settings.validate()
        return settings

    def override(self, **values):
        """Apply explicit command-line overrides."""
        for name in PATH_FIELDS:
            if name in values and values[name] is not None:
                values[name] = values[name].expanduser().resolve()
        settings = replace(self, **values)
        settings.validate()
        return settings

    def validate(self):
        if self.phase not in PHASES:
            raise ValueError('配置 phase 须为 ' + '、'.join(PHASES))
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError('配置 port 须为 0 至 65535 的整数')
        if not isinstance(self.host, str):
            raise ValueError('配置 host 须为监听 IP 字符串')
        try:
            ip_address(self.host)
        except ValueError as error:
            raise ValueError('配置 host 须为有效的监听 IP，如 0.0.0.0 或 127.0.0.1') from error
        IPWhitelist(self.ip_whitelist)
        for name in TEXT_FIELDS:
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f'配置 {name} 须为字符串或 null')
        if self.arena_url is not None:
            try:
                parse_peer_url(self.arena_url)
            except ValueError as error:
                raise ValueError('配置 arena_url 须为 HTTP 基础 URL') from error

    def for_server(self):
        """Validate server settings before creating persistent state."""
        self.validate()
        if self.db is None:
            raise ValueError('请在配置文件提供 db，或通过 --db 指定数据库')
        user, password = self.admin_user or '', self.admin_password or ''
        if bool(user) != bool(password) or ':' in user:
            raise ValueError('配置 admin_user 和 admin_password 须一并提供，账号不能包含冒号')
        if self.arena_url is None:
            raise ValueError('请在配置文件提供 arena_url，或通过 --arena-url 指定 Arena 地址')
        if self.archive_root is None:
            raise ValueError('请在配置文件提供 archive_root，或通过 --archive-root 指定共享归档目录')
        return self

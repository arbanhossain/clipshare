"""Configuration: storage paths, peers, sync options."""
from __future__ import annotations

import json
import os
import secrets
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "clipshare"
DEFAULT_PORT = 58321
DISCOVERY_PORT = 58322
PROTOCOL_VERSION = 1


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))


def parse_peer(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    if not host:
        host, port = addr, ""
    return host, int(port or DEFAULT_PORT)


def format_peer(host: str, port: int) -> str:
    return f"{host}:{port}"


@dataclass
class Config:
    device_name: str = field(default_factory=socket.gethostname)
    device_id: str = field(default_factory=lambda: secrets.token_hex(8))
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    port: int = DEFAULT_PORT
    peers: list[str] = field(default_factory=list)
    auto_push: bool = True
    auto_add_peers: bool = True
    discover: bool = True
    watch_interval_ms: int = 700
    history_limit: int = 500
    retention_days: int = 30
    max_image_bytes: int = 10 * 1024 * 1024

    @property
    def config_dir(self) -> Path:
        return getattr(self, "_config_dir", None) or (_xdg_config_home() / APP_NAME)

    @config_dir.setter
    def config_dir(self, value: Path) -> None:
        self._config_dir = Path(value)

    @property
    def data_dir(self) -> Path:
        return getattr(self, "_data_dir", None) or (_xdg_data_home() / APP_NAME)

    @data_dir.setter
    def data_dir(self, value: Path) -> None:
        self._data_dir = Path(value)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "history.db"

    def add_peer(self, host: str, port: int | None = None) -> bool:
        addr = format_peer(host, port or self.port)
        if addr in self.peers:
            return False
        self.peers.append(addr)
        self.save()
        return True

    def remove_peer(self, addr: str) -> bool:
        if addr in self.peers:
            self.peers.remove(addr)
            self.save()
            return True
        return False

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = cfg.config_dir / "config.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for key, value in data.items():
                    if hasattr(cfg, key):
                        setattr(cfg, key, value)
            except (json.JSONDecodeError, OSError):
                pass
        cfg.config_dir.mkdir(parents=True, exist_ok=True)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            cfg.save()
        return cfg

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_dir / "config.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)

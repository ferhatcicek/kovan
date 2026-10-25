"""Uygulama yapılandırması ve veri dizini."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


PORTABLE_MARKER = "kovan.portable"
# Önceki adıyla (0.1–0.2) oluşturulmuş veriler otomatik tanınır; hiçbir yapılandırma kaybolmaz.
LEGACY_PORTABLE_MARKER = "pydeck.portable"
LEGACY_DB_NAME = "pydeck.db"


def app_dir() -> Path | None:
    """Paketlenmiş uygulamada .exe klasörü; kaynak koddan çalışırken None."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def portable_data_dir() -> Path | None:
    """Taşınabilir mod: .exe yanında `kovan.portable` dosyası veya `data/` klasörü varsa veri orada tutulur."""
    base = app_dir()
    if base and ((base / PORTABLE_MARKER).exists() or (base / LEGACY_PORTABLE_MARKER).exists()
                 or (base / "data").is_dir()):
        return base / "data"
    return None


def _platform_data_dir(name: str) -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / name
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / name.lower()


def legacy_data_dir() -> Path:
    return _platform_data_dir("PyDeck")


def default_data_dir() -> Path:
    env = os.environ.get("KOVAN_DATA_DIR") or os.environ.get("PYDECK_DATA_DIR")
    if env:
        return Path(env)
    portable = portable_data_dir()
    if portable:
        return portable
    return _platform_data_dir("Kovan")


def migrate_legacy_data(data_dir: Path) -> str | None:
    """Önceki sürümün verisini yeni konuma taşır (eski kopya yerinde bırakılır). Dönüş: açıklama veya None."""
    data_dir = Path(data_dir)
    new_db = data_dir / "kovan.db"
    if new_db.exists():
        return None
    # 1) Aynı klasörde eski veritabanı adı (ör. --data-dir veya taşınabilir mod)
    old_db = data_dir / LEGACY_DB_NAME
    if old_db.exists():
        for suffix in ("", "-wal", "-shm"):
            src = data_dir / (LEGACY_DB_NAME + suffix)
            if src.exists():
                shutil.copy2(src, data_dir / ("kovan.db" + suffix))
        return f"{old_db} → {new_db}"
    # 2) Varsayılan konum: eski 'PyDeck' veri klasörü
    legacy = legacy_data_dir()
    if data_dir == _platform_data_dir("Kovan") and (legacy / LEGACY_DB_NAME).exists():
        shutil.copytree(legacy, data_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.lock", "pydeck.lock"))
        for suffix in ("", "-wal", "-shm"):
            src = data_dir / (LEGACY_DB_NAME + suffix)
            if src.exists():
                src.rename(data_dir / ("kovan.db" + suffix))
        return f"{legacy} → {data_dir}"
    return None


def default_roots() -> list[str]:
    if os.name == "nt" and Path("D:/code").is_dir():
        return [str(Path("D:/code"))]
    return []


@dataclass
class AppConfig:
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"  # bilinçli olarak sabit: panel ağa açılmaz
    port: int = 8765
    open_browser: bool = True
    log_buffer_lines: int = 5000
    log_retention_days: int = 14
    log_quota_mb: int = 2048
    poll_interval: float = 5.0
    legacy_migration: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Kovan yalnızca yerel arayüzde (127.0.0.1) dinleyebilir")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kovan.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def portable(self) -> bool:
        pd = portable_data_dir()
        return pd is not None and pd.resolve() == self.data_dir.resolve()

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_migration = migrate_legacy_data(self.data_dir)
        for d in (self.data_dir, self.logs_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(d, 0o700)

"""Yedekleme: günlük + işlem öncesi otomatik yedekler, saklama politikası, geri yükleme ve JSON aynası."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from ..infrastructure import migrations
from ..infrastructure.db import Repository
from . import portability

KINDS = ("auto", "manual", "pre-import", "pre-restore", "pre-migrate")
KEEP_AUTO = 10
_NAME = re.compile(r"^(auto|manual|pre-import|pre-restore|pre-migrate)-[\w.-]+\.db$")
MIRROR_NAME = "config-latest.kovan.json"


class BackupError(ValueError):
    pass


class BackupService:
    def __init__(self, repo: Repository, data_dir: Path):
        self.repo = repo
        self.dir = data_dir / "backups"
        self.mirror_path = data_dir / MIRROR_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.dir, 0o700)
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    # ---------- yedek oluşturma
    def create(self, kind: str = "manual") -> dict:
        if kind not in KINDS:
            raise BackupError("Geçersiz yedek türü")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.dir / f"{kind}-{stamp}-v{self.repo.schema_version}.db"
        n = 1
        while dest.exists():
            dest = self.dir / f"{kind}-{stamp}-{n}-v{self.repo.schema_version}.db"
            n += 1
        with self.repo._lock:
            migrations.backup_database(self.repo.connection, dest)
        if kind == "auto":
            self.repo.set_setting("last_backup", time.time())
        self._prune()
        self.repo.audit("backup_create", dest.name)
        return self._info(dest)

    def daily_if_due(self) -> dict | None:
        last = self.repo.get_setting("last_backup", 0) or 0
        if time.time() - last >= 24 * 3600 and (self.repo.list_projects() or self.repo.get_setting("roots")):
            return self.create("auto")
        return None

    def _prune(self) -> None:
        autos = sorted(self.dir.glob("auto-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in autos[KEEP_AUTO:]:
            p.unlink(missing_ok=True)
        for kind in ("pre-import", "pre-restore", "pre-migrate"):
            olds = sorted(self.dir.glob(f"{kind}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in olds[KEEP_AUTO:]:
                p.unlink(missing_ok=True)

    def _info(self, p: Path) -> dict:
        st = p.stat()
        m = re.search(r"-v(\d+)\.db$", p.name)
        return {"name": p.name, "kind": p.name.split("-")[0] if not p.name.startswith("pre-")
                else "-".join(p.name.split("-")[:2]), "size": st.st_size, "ts": st.st_mtime,
                "schema": int(m.group(1)) if m else None}

    def list(self) -> list[dict]:
        return sorted((self._info(p) for p in self.dir.glob("*.db") if _NAME.match(p.name)),
                      key=lambda x: x["ts"], reverse=True)

    def path_of(self, name: str) -> Path:
        if not _NAME.match(name) or "/" in name or "\\" in name:
            raise BackupError("Geçersiz yedek adı")
        p = (self.dir / name).resolve()
        if p.parent != self.dir.resolve() or not p.is_file():
            raise BackupError("Yedek bulunamadı")
        return p

    # ---------- geri yükleme
    def restore(self, name: str) -> dict:
        src_path = self.path_of(name)
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("Yedek dosyası bozuk")
            row = src.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            version = int(json.loads(row[0])) if row else 1
            if version > migrations.LATEST:
                raise BackupError("Yedek daha yeni bir Kovan sürümüne ait")
            safety = self.create("pre-restore")
            current_password = self.repo.get_setting("password_hash")
            with self.repo._lock:
                conn = self.repo.connection
                src.backup(conn)
                # Yedek dosyası yalnızca veri taşımalı: tetikleyici/görünüm gibi nesneler kaldırılır
                for kind, name in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall():
                    conn.execute(f'DROP {kind.upper()} IF EXISTS "{name.replace(chr(34), "")}"')
                migrations.migrate(conn, self.dir)  # eski şemalı yedek ise yükselt
                # Panel parolası yedekten gelmez: geri yükleme kimlik doğrulamayı değiştiremez
                if current_password:
                    self.repo.set_setting("password_hash", current_password)
        finally:
            src.close()
        self.repo.audit("backup_restore", name)
        self.write_mirror()
        return {"restored": name, "safety_backup": safety["name"]}

    # ---------- insan-okunur ayna (gizliler hariç)
    def schedule_mirror(self, delay: float = 2.0) -> None:
        with self._timer_lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self.write_mirror)
            self._timer.daemon = True
            self._timer.start()

    def write_mirror(self) -> None:
        try:
            doc = portability.export_config(self.repo, include_secrets=False)
            tmp = self.mirror_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.mirror_path)  # atomik: yarım yazılmış dosya kalmaz
        except Exception:  # noqa: BLE001 - ayna en iyi çaba ile yazılır
            pass

    def cancel(self) -> None:
        with self._timer_lock:
            if self._timer:
                self._timer.cancel()

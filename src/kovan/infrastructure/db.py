"""SQLite deposu (WAL). Projeler/kategoriler JSON olarak saklanır; şema pydantic ile doğrulanır.

Dayanıklılık: şema migrasyonları (migrations.py), okunamayan kayıtların silinmeden karantinaya alınması
ve her yapılandırma değişikliğinde `on_change` geri çağrısı (yedek aynası için).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import ValidationError

from ..domain.models import Category, Project
from . import migrations

RECORD_VERSION = 2

DEFAULT_CATEGORIES = [
    ("Web API", "#4f46e5", "api"), ("Web UI", "#0891b2", "ui"), ("Bot/Script", "#16a34a", "bot"),
    ("Worker", "#d97706", "worker"), ("Notebook", "#db2777", "nb"),
]


class Repository:
    def __init__(self, path: Path, backups_dir: Path | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migration = migrations.migrate(self._conn, backups_dir)
        self.on_change: Callable[[], None] | None = None
        if self.get_setting("seeded") is None:
            for i, (name, color, icon) in enumerate(DEFAULT_CATEGORIES):
                self.save_category(Category(name=name, color=color, icon=icon, sort=i))
            self.set_setting("seeded", "1")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _x(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or 0

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 - ayna hatası asıl işlemi bozmamalı
                pass

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Birden çok kaydı atomik yazar. Ayna/geri çağrılar commit sonrasında bir kez tetiklenir."""
        with self._lock:
            cb, self.on_change = self.on_change, None
            self._conn.execute("BEGIN")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self.on_change = cb
        self._changed()

    def integrity_ok(self) -> bool:
        return self._q("PRAGMA integrity_check")[0][0] == "ok"

    @property
    def schema_version(self) -> int:
        return int(self.get_setting("schema_version", migrations.LATEST))

    # --- ayarlar ---
    def get_setting(self, key: str, default=None):
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value) -> None:
        self._x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))
        if key not in ("seeded", "schema_version", "last_backup"):
            self._changed()

    # --- karantina: okunamayan kayıtlar silinmez ---
    def _load(self, model, row: sqlite3.Row, source: str):
        try:
            return model(id=row["id"], **json.loads(row["data"]))
        except (ValidationError, ValueError, TypeError) as e:
            raw = row["data"]
            if not self._q("SELECT 1 FROM quarantine WHERE source=? AND raw=?", (source, raw)):
                self._x("INSERT INTO quarantine(source,ts,raw,error) VALUES(?,?,?,?)",
                        (source, time.time(), raw, str(e)[:2000]))
            return None

    def list_quarantine(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM quarantine ORDER BY id DESC")]

    def _dump(self, obj) -> str:
        data = obj.model_dump(mode="json", exclude={"id"})
        data["_v"] = RECORD_VERSION
        return json.dumps(data, ensure_ascii=False)

    # --- kategoriler ---
    def list_categories(self) -> list[Category]:
        cats = [c for r in self._q("SELECT * FROM categories") if (c := self._load(Category, r, "category"))]
        return sorted(cats, key=lambda c: (c.sort, c.name.lower()))

    def get_category(self, cid: int) -> Category | None:
        rows = self._q("SELECT * FROM categories WHERE id=?", (cid,))
        return self._load(Category, rows[0], "category") if rows else None

    def save_category(self, c: Category) -> Category:
        data = self._dump(c)
        if c.id is None:
            c = c.model_copy(update={"id": self._x("INSERT INTO categories(data) VALUES(?)", (data,))})
        else:
            self._x("UPDATE categories SET data=? WHERE id=?", (data, c.id))
        self._changed()
        return c

    def delete_category(self, cid: int) -> None:
        with self._lock:
            for p in self.list_projects():
                if p.category_id == cid:
                    self.save_project(p.model_copy(update={"category_id": None}))
            for c in self.list_categories():
                if c.parent_id == cid:
                    self.save_category(c.model_copy(update={"parent_id": None}))
            self._x("DELETE FROM categories WHERE id=?", (cid,))
        self._changed()

    # --- projeler ---
    def list_projects(self) -> list[Project]:
        return [p for r in self._q("SELECT * FROM projects ORDER BY id") if (p := self._load(Project, r, "project"))]

    def get_project(self, pid: int) -> Project | None:
        rows = self._q("SELECT * FROM projects WHERE id=?", (pid,))
        return self._load(Project, rows[0], "project") if rows else None

    def find_project_by_path(self, path: str) -> Project | None:
        rows = self._q("SELECT * FROM projects WHERE path=?", (path,))
        return self._load(Project, rows[0], "project") if rows else None

    def save_project(self, p: Project) -> Project:
        data = self._dump(p)
        if p.id is None:
            p = p.model_copy(update={"id": self._x("INSERT INTO projects(path,data) VALUES(?,?)", (p.path, data))})
        else:
            self._x("UPDATE projects SET path=?, data=? WHERE id=?", (p.path, data, p.id))
        self._changed()
        return p

    def delete_project(self, pid: int) -> None:
        self._x("DELETE FROM projects WHERE id=?", (pid,))
        self._x("DELETE FROM live_processes WHERE project_id=?", (pid,))
        self._changed()

    def replace_configuration(self, categories: list[Category], projects: list[Project],
                              settings: dict) -> None:
        """İçe aktarmanın 'Değiştir' modu: tek transaction içinde tüm yapılandırmayı değiştirir."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM projects")
                self._conn.execute("DELETE FROM categories")
                self._conn.execute("DELETE FROM live_processes")  # proje kimlikleri yeniden numaralanır
                for c in categories:
                    self._conn.execute("INSERT INTO categories(id,data) VALUES(?,?)", (c.id, self._dump(c)))
                for p in projects:
                    self._conn.execute("INSERT INTO projects(id,path,data) VALUES(?,?,?)",
                                       (p.id, p.path, self._dump(p)))
                for k, v in settings.items():
                    self._conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._changed()

    # --- çalıştırma geçmişi ---
    def start_run(self, project_id: int, profile: str, trigger: str, log_file: str) -> int:
        return self._x("INSERT INTO runs(project_id,profile,started,trigger,log_file) VALUES(?,?,?,?,?)",
                       (project_id, profile, time.time(), trigger, log_file))

    def end_run(self, run_id: int, exit_code: int | None, end_reason: str | None = None,
                signal: str | None = None) -> None:
        self._x("UPDATE runs SET ended=?, exit_code=?, end_reason=?, signal=? WHERE id=? AND ended IS NULL",
                (time.time(), exit_code, end_reason, signal, run_id))

    def list_runs(self, project_id: int, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM runs WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))]

    def get_run(self, run_id: int) -> dict | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    # --- canlı süreç kaydı (panel yeniden açıldığında devralma için) ---
    def record_live(self, project_id: int, run_id: int, pid: int, create_time: float | None,
                    argv: list[str], cwd: str) -> None:
        self._x("INSERT OR REPLACE INTO live_processes(project_id,run_id,pid,create_time,argv,cwd,started) "
                "VALUES(?,?,?,?,?,?,?)", (project_id, run_id, pid, create_time, json.dumps(argv), cwd, time.time()))

    def clear_live(self, project_id: int) -> None:
        self._x("DELETE FROM live_processes WHERE project_id=?", (project_id,))

    def list_live(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM live_processes")]

    # --- olaylar ve bildirimler ---
    def add_event(self, project_id: int | None, kind: str, detail: str = "") -> None:
        self._x("INSERT INTO events(project_id,ts,kind,detail) VALUES(?,?,?,?)",
                (project_id, time.time(), kind, detail[:1000]))

    def list_events(self, project_id: int, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))]

    def add_notification(self, project_id: int | None, level: str, title: str, body: str) -> dict:
        nid = self._x("INSERT INTO notifications(project_id,ts,level,title,body) VALUES(?,?,?,?,?)",
                      (project_id, time.time(), level, title[:200], body[:500]))
        self._x("DELETE FROM notifications WHERE id <= ?", (nid - 500,))  # son 500 tutulur
        return dict(self._q("SELECT * FROM notifications WHERE id=?", (nid,))[0])

    def list_notifications(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))]

    def mark_notifications_read(self) -> None:
        self._x("UPDATE notifications SET read=1 WHERE read=0")

    # --- denetim kaydı (yalnız ekleme) ---
    def audit(self, action: str, detail: str = "") -> None:
        self._x("INSERT INTO audit(ts,action,detail) VALUES(?,?,?)", (time.time(), action, detail[:2000]))

    def list_audit(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

"""Şema sürümleme ve sıralı migrasyonlar.

Kurallar:
- Her migrasyon tek bir transaction içinde çalışır; hata olursa hiçbir değişiklik kalmaz.
- Migrasyondan önce veritabanının tam yedeği alınır (sqlite backup API).
- Veritabanı uygulamanın bildiğinden yeni bir şemadaysa açılmaz (sürüm düşürmede veri bozulmasın).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, profile TEXT,
    started REAL NOT NULL, ended REAL, exit_code INTEGER, trigger TEXT, log_file TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL, detail TEXT
);
"""


class SchemaTooNew(RuntimeError):
    def __init__(self, db_version: int, app_version: int):
        super().__init__(f"Veritabanı şeması v{db_version}, bu Kovan en fazla v{app_version} destekliyor. "
                         "Veriniz korunuyor; lütfen Kovan'in daha yeni sürümünü kullanın.")
        self.db_version, self.app_version = db_version, app_version


def _get_version(conn: sqlite3.Connection) -> int | None:
    has_settings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
    if not has_settings:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    if row:
        return int(json.loads(row[0]))
    has_projects = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
    return 1 if has_projects else None


def _set_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES('schema_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(v),))


def _run_statements(conn: sqlite3.Connection, script: str) -> None:
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


# ---------------------------------------------------------------- migrasyonlar
def _m2(conn: sqlite3.Connection) -> None:
    """v0.2: kararlı uid'ler, çıkış nedeni, canlı süreç kaydı, olaylar, bildirimler, karantina."""
    for table in ("projects", "categories"):
        for rid, data in conn.execute(f"SELECT id, data FROM {table}").fetchall():  # noqa: S608 - sabit tablo adı
            obj = json.loads(data)
            obj.setdefault("uid", uuid.uuid4().hex)
            conn.execute(f"UPDATE {table} SET data=? WHERE id=?", (json.dumps(obj, ensure_ascii=False), rid))  # noqa: S608
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "end_reason" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN end_reason TEXT")
    if "signal" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN signal TEXT")
    # v0.1'de kapanmadan kalan çalıştırmalar
    conn.execute("UPDATE runs SET ended=started, end_reason='panel_restart' WHERE ended IS NULL")
    # executescript() açık transaction'ı commit eder; atomiklik için ifadeler tek tek çalıştırılır
    _run_statements(conn, """
    CREATE TABLE IF NOT EXISTS live_processes (
        project_id INTEGER PRIMARY KEY, run_id INTEGER, pid INTEGER NOT NULL, create_time REAL,
        argv TEXT, cwd TEXT, started REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, ts REAL NOT NULL, kind TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX IF NOT EXISTS events_project ON events(project_id, id);
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, ts REAL NOT NULL, level TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT, read INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS quarantine (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, ts REAL NOT NULL, raw TEXT NOT NULL,
        error TEXT
    );
    """)


MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [(2, _m2)]
LATEST = MIGRATIONS[-1][0]


def backup_database(conn: sqlite3.Connection, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def migrate(conn: sqlite3.Connection, backups_dir: Path | None) -> dict:
    """Veritabanını en son şemaya taşır. Dönüş: {"from": v, "to": v, "backup": yol|None}."""
    version = _get_version(conn)
    if version is None:  # boş veritabanı: doğrudan son şema
        conn.execute("BEGIN")
        try:
            _run_statements(conn, BASE_SCHEMA)
            for _, fn in MIGRATIONS:
                fn(conn)
            _set_version(conn, LATEST)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"from": None, "to": LATEST, "backup": None}
    if version > LATEST:
        raise SchemaTooNew(version, LATEST)
    if version == LATEST:
        return {"from": version, "to": version, "backup": None}

    backup = None
    if backups_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_database(conn, backups_dir / f"pre-migrate-v{version}-to-v{LATEST}-{stamp}.db")
    for target, fn in MIGRATIONS:
        if target <= version:
            continue
        conn.execute("BEGIN")
        try:
            fn(conn)
            _set_version(conn, target)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"from": version, "to": LATEST, "backup": str(backup) if backup else None}

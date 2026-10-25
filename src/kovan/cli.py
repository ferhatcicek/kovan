"""Komut satırı yardımcıları: yapılandırma dışa/içe aktarma, yedekleme, geri yükleme ve tanılama.

Panel açıkken veritabanına yazan komutlar (import, restore) kilit dosyası nedeniyle reddedilir.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import psutil

from .config import AppConfig

LOCK_NAME = "kovan.lock"


# ---------------------------------------------------------------- kilit dosyası
def acquire_lock(data_dir: Path) -> Path:
    """Atomik kilit (O_EXCL). Sahibi yaşamayan eski kilit dosyası temizlenip yeniden denenir."""
    lock = data_dir / LOCK_NAME
    payload = json.dumps({"pid": os.getpid(), "create_time": psutil.Process().create_time()}).encode()
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            held = panel_running(data_dir)
            if held:
                raise SystemExit(f"Kovan bu veri dizini için zaten çalışıyor (pid {held}).") from None
            lock.unlink(missing_ok=True)  # eski (sahipsiz) kilit
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        return lock
    raise SystemExit("Kilit dosyası oluşturulamadı")


def write_private(path: Path, text: str) -> None:
    """Yalnızca kullanıcının okuyabileceği dosya (0600) yazar."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)


def release_lock(data_dir: Path) -> None:
    lock = data_dir / LOCK_NAME
    try:
        if json.loads(lock.read_text()).get("pid") == os.getpid():
            lock.unlink()
    except (OSError, ValueError):
        pass


def panel_running(data_dir: Path) -> int | None:
    try:
        info = json.loads((data_dir / LOCK_NAME).read_text())
        p = psutil.Process(int(info["pid"]))
        if abs(p.create_time() - float(info["create_time"])) < 1.0:
            return p.pid
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return None


# ---------------------------------------------------------------- komutlar
def _repo(cfg: AppConfig):
    from .infrastructure.db import Repository
    cfg.ensure_dirs()
    return Repository(cfg.db_path, cfg.backups_dir)


def _require_closed(cfg: AppConfig) -> None:
    pid = panel_running(cfg.data_dir)
    if pid:
        raise SystemExit(f"Önce paneli kapatın (çalışan pid {pid}); bu komut veritabanına yazar.")


def cmd_export(cfg: AppConfig, args) -> int:
    from .application import portability
    passphrase = None
    if args.include_secrets:
        passphrase = os.environ.get("KOVAN_PASSPHRASE") or getpass.getpass("Gizliler için şifreleme parolası: ")
    doc = portability.export_config(_repo(cfg), args.include_secrets, passphrase)
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if args.output == "-":
        sys.stdout.write(text + "\n")
    else:
        write_private(Path(args.output), text)
        print(f"Dışa aktarıldı: {args.output} ({len(doc['projects'])} proje, {len(doc['categories'])} kategori)"
              + (" — gizli değerler dahil edilmedi" if doc.get("secrets_omitted") else ""))
    return 0


def _parse_map(items: list[str], doc) -> dict[str, str]:
    """--map 'D:\\code=C:\\work' veya --map 'r1=C:\\work' biçimlerini kök kimliğine çevirir."""
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"Geçersiz --map: {item} (eski=yeni biçiminde olmalı)")
        old, new = item.split("=", 1)
        rid = next((r.id for r in doc.roots if r.id == old or os.path.normcase(r.path) == os.path.normcase(old)),
                   None)
        if not rid:
            raise SystemExit(f"Dosyada böyle bir kök yok: {old}. Kökler: " +
                             ", ".join(f"{r.id}={r.path}" for r in doc.roots))
        out[rid] = new
    return out


def cmd_import(cfg: AppConfig, args) -> int:
    from .application import portability
    from .application.backup_service import BackupService
    if not args.dry_run:
        _require_closed(cfg)
    repo = _repo(cfg)
    doc = portability.parse_import(Path(args.file).read_bytes())
    passphrase = None
    if doc.secrets:
        passphrase = os.environ.get("KOVAN_PASSPHRASE") or (
            getpass.getpass("Dosya şifreli gizliler içeriyor; parola (boş = gizlileri atla): ") or None)
    plan = portability.plan_import(repo, doc, _parse_map(args.map, doc), passphrase, args.keep_watch)
    s = plan["summary"]
    print(f"Kaynak: {doc.source.get('host', '?')} · Kovan {doc.app_version}")
    print(f"Eklenecek {s['add']} · güncellenecek {s['update']} · değişmeyecek {s['unchanged']} · atlanacak {s['skip']}")
    for it in plan["projects"]:
        mark = {"add": "+", "update": "~", "unchanged": "=", "skip": "!"}[it["action"]]
        print(f"  {mark} {it['name']:<24} {it['path']}  {it['reason']}")
        if it["action"] in ("add", "update"):
            for c in it["commands"]:
                print(f"        [{c['profile']}] {c['argv']}  (yorumlayıcı: {c['interpreter']}, dizin: {c['cwd']}, "
                      f"yeniden başlatma: {c['restart']})")
                if c["env"]:
                    print(f"            env: {' '.join(c['env'])}")
                if c["danger"]:
                    print(f"            DİKKAT: {', '.join(c['danger'])}")
    for e in plan["errors"]:
        print(f"  UYARI: {e}")
    if args.dry_run:
        return 0
    backup = BackupService(repo, cfg.data_dir).create("pre-import")
    result = portability.apply_import(repo, doc, plan, args.mode)
    repo.audit("config_import_cli", json.dumps({**result, "backup": backup["name"]}))
    BackupService(repo, cfg.data_dir).write_mirror()
    print(f"Tamamlandı ({args.mode}). Önceki durumun yedeği: {backup['name']}")
    return 0


def cmd_backup(cfg: AppConfig, args) -> int:
    from .application.backup_service import BackupService
    b = BackupService(_repo(cfg), cfg.data_dir).create("manual")
    print(f"Yedek alındı: {cfg.backups_dir / b['name']}")
    return 0


def cmd_restore(cfg: AppConfig, args) -> int:
    from .application.backup_service import BackupService
    svc = BackupService(_repo(cfg), cfg.data_dir)
    if not args.name:
        for b in svc.list():
            print(f"  {b['name']}  ({b['size'] // 1024} KB)")
        return 0
    _require_closed(cfg)
    res = svc.restore(args.name)
    print(f"Geri yüklendi: {res['restored']} (önceki durum: {res['safety_backup']})")
    return 0


def cmd_doctor(cfg: AppConfig, args) -> int:
    from .application.backup_service import BackupService
    from .infrastructure import migrations
    repo = _repo(cfg)
    print(f"Veri dizini   : {cfg.data_dir}{'  (taşınabilir mod)' if cfg.portable else ''}")
    print(f"Şema sürümü   : v{repo.schema_version} (desteklenen v{migrations.LATEST})")
    print(f"Bütünlük      : {'tamam' if repo.integrity_ok() else 'BOZUK — restore komutunu kullanın'}")
    print(f"Projeler      : {len(repo.list_projects())}, kategoriler: {len(repo.list_categories())}")
    q = repo.list_quarantine()
    print(f"Karantina     : {len(q)} okunamayan kayıt" + (" (verileriniz silinmedi)" if q else ""))
    print(f"Yedekler      : {len(BackupService(repo, cfg.data_dir).list())} adet, {cfg.backups_dir}")
    pid = panel_running(cfg.data_dir)
    print(f"Panel         : {'çalışıyor (pid %d)' % pid if pid else 'kapalı'}")
    return 0


COMMANDS = {"export": cmd_export, "import": cmd_import, "backup": cmd_backup, "restore": cmd_restore,
            "doctor": cmd_doctor}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kovan", description="Python projeleri için yerel yönetim paneli")
    ap.add_argument("--data-dir", type=Path, default=None, help="Veri dizini (varsayılan: kullanıcı veri klasörü)")
    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("serve", help="Paneli başlat (varsayılan)")
    s.add_argument("--port", type=int, default=int(os.environ.get("KOVAN_PORT", 8765)))
    s.add_argument("--root", action="append", default=[], help="İzinli kök klasör ekle (tekrarlanabilir)")
    s.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    s.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS, dest="data_dir", help=argparse.SUPPRESS)
    e = sub.add_parser("export", help="Yapılandırmayı taşınabilir JSON dosyasına aktar")
    e.add_argument("-o", "--output", default="kovan-config.json")
    e.add_argument("--include-secrets", action="store_true", help="Gizli değerleri parolayla şifreleyerek dahil et")
    i = sub.add_parser("import", help="JSON yapılandırma dosyasını içe aktar")
    i.add_argument("file")
    i.add_argument("--map", action="append", default=[], help="Kök eşleme: 'D:\\code=C:\\work' (tekrarlanabilir)")
    i.add_argument("--mode", choices=["merge", "replace"], default="merge")
    i.add_argument("--dry-run", action="store_true", help="Yalnızca ne olacağını göster")
    i.add_argument("--keep-watch", action="store_true",
                   help="Dosyadaki otomatik yeniden başlatma ayarlarını da al (varsayılan: yerel ayar korunur)")
    sub.add_parser("backup", help="Veritabanının yedeğini al")
    r = sub.add_parser("restore", help="Yedekten geri yükle (ad verilmezse yedekleri listeler)")
    r.add_argument("name", nargs="?")
    sub.add_parser("doctor", help="Veri dizini, şema, bütünlük ve yedek durumunu göster")
    return ap


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    cfg = AppConfig()
    if args.data_dir:
        cfg.data_dir = args.data_dir
    try:
        return COMMANDS[args.command](cfg, args)
    except Exception as e:  # noqa: BLE001 - CLI'da okunur hata mesajı
        from .application.portability import ImportError_
        from .application.backup_service import BackupError
        from .infrastructure.crypto import DecryptError
        from .infrastructure.migrations import SchemaTooNew
        if isinstance(e, (ImportError_, BackupError, DecryptError, SchemaTooNew, ValueError, OSError)):
            print(f"Hata: {e}", file=sys.stderr)
            return 2
        raise

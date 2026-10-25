import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from kovan.application import portability
from kovan.application.backup_service import BackupError, BackupService
from kovan.domain.models import Category, EnvVar, Project, RunProfile
from kovan.infrastructure import crypto, migrations
from kovan.infrastructure.db import Repository

V01 = Path(__file__).parents[1] / "fixtures" / "db" / "v0_1.db"


@pytest.fixture
def old_db(tmp_path) -> Path:
    p = tmp_path / "kovan.db"
    shutil.copy(V01, p)
    return p


def test_v01_database_migrates_without_loss(old_db, tmp_path):
    repo = Repository(old_db, tmp_path / "backups")
    assert repo.migration["from"] == 1 and repo.migration["to"] == migrations.LATEST
    assert Path(repo.migration["backup"]).exists()
    projects = {p.name: p for p in repo.list_projects()}
    order = projects["order-api"]
    assert order.tags == ["shop", "backend"] and order.favorite and order.notes == "8001 portu sabit"
    assert [p.name for p in order.profiles] == ["dev", "prod"] and order.active_profile == "dev"
    assert {e.key: e.value for e in order.profiles[0].env} == {"DB_URL": "postgres://x", "API_KEY": "gizli-123"}
    assert order.uid and len({p.uid for p in projects.values()}) == 2
    cats = {c.id: c for c in repo.list_categories()}
    assert cats[order.category_id].name == "Shop" and cats[cats[order.category_id].parent_id].name == "Web API"
    assert repo.get_setting("port_range") == [8100, 8200]
    assert repo.get_setting("password_hash", "").startswith("$argon2")   # parola korunur
    assert all(r["ended"] is not None for r in repo.list_runs(2))          # açık kalan çalıştırma kapandı
    # ikinci açılış: migrasyon tekrarlanmaz, uid'ler sabit kalır
    uids = {p.uid for p in projects.values()}
    repo.close()
    repo2 = Repository(old_db, tmp_path / "backups")
    assert repo2.migration["backup"] is None and {p.uid for p in repo2.list_projects()} == uids


def test_newer_schema_is_refused_without_touching_data(old_db, tmp_path):
    conn = sqlite3.connect(old_db)
    conn.execute("INSERT INTO settings(key,value) VALUES('schema_version','99')")
    conn.commit()
    conn.close()
    before = old_db.read_bytes()
    with pytest.raises(migrations.SchemaTooNew):
        Repository(old_db, tmp_path / "b")
    assert old_db.read_bytes() == before


def test_failed_migration_rolls_back(old_db, tmp_path, monkeypatch):
    def boom(conn):
        conn.execute("ALTER TABLE runs ADD COLUMN end_reason TEXT")
        raise RuntimeError("migrasyon hatası")
    monkeypatch.setattr(migrations, "MIGRATIONS", [(2, boom)])
    monkeypatch.setattr(migrations, "LATEST", 2)
    with pytest.raises(RuntimeError):
        Repository(old_db, tmp_path / "b")
    conn = sqlite3.connect(old_db)
    assert "end_reason" not in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert not conn.execute("SELECT 1 FROM settings WHERE key='schema_version'").fetchone()


def test_unreadable_record_is_quarantined_not_deleted(tmp_path):
    repo = Repository(tmp_path / "db.sqlite")
    repo.save_project(Project(name="ok", path="/a"))
    repo._x("INSERT INTO projects(path,data) VALUES(?,?)", ("/b", json.dumps({"name": "", "path": "/b"})))
    assert [p.name for p in repo.list_projects()] == ["ok"]
    q = repo.list_quarantine()
    assert len(q) == 1 and json.loads(q[0]["raw"])["path"] == "/b"
    assert repo._q("SELECT COUNT(*) FROM projects")[0][0] == 2       # ham kayıt yerinde
    repo.list_projects()
    assert len(repo.list_quarantine()) == 1                            # tekrar eklenmez


def test_unknown_fields_from_newer_version_are_ignored():
    p = Project(name="x", path="/x", future_field=1, profiles=[{"name": "d", "yeni_alan": True}])
    assert p.name == "x" and p.profiles[0].name == "d"


def _populated(tmp_path, root: Path) -> Repository:
    repo = Repository(tmp_path / "src.db")
    repo.set_setting("roots", [str(root)])
    parent = repo.save_category(Category(name="İş", color="#112233"))
    child = repo.save_category(Category(name="API", color="#445566", parent_id=parent.id))
    repo.save_project(Project(name="order", path=str(root / "order"), category_id=child.id, tags=["t1"],
                              favorite=True, notes="not",
                              profiles=[RunProfile(name="dev", target="main.py", port=8001,
                                                   env=[EnvVar(key="K", value="v"),
                                                        EnvVar(key="SECRET", value="s3cr3t!", secret=True)])]))
    return repo


def test_export_has_relative_paths_and_no_plain_secrets(tmp_path):
    root = tmp_path / "code"
    (root / "order").mkdir(parents=True)
    doc = portability.export_config(_populated(tmp_path, root))
    assert doc["projects"][0]["rel_path"] == "order" and doc["projects"][0]["root"] == "r1"
    assert "s3cr3t!" not in json.dumps(doc) and doc["secrets_omitted"] is True


def test_roundtrip_to_new_machine_with_root_mapping(tmp_path):
    old_root, new_root = tmp_path / "D_code", tmp_path / "C_work"
    (old_root / "order").mkdir(parents=True)
    (new_root / "order").mkdir(parents=True)
    src = _populated(tmp_path, old_root)
    raw = json.dumps(portability.export_config(src, include_secrets=True, passphrase="tasima-parolasi"))
    assert "s3cr3t!" not in raw

    dst = Repository(tmp_path / "dst.db")
    doc = portability.parse_import(raw)
    plan = portability.plan_import(dst, doc, {"r1": str(new_root)}, "tasima-parolasi")
    assert plan["summary"]["add"] == 1 and not plan["errors"]
    portability.apply_import(dst, doc, plan, "merge")
    p = dst.list_projects()[0]
    assert p.path == str((new_root / "order").resolve()) and p.tags == ["t1"] and p.notes == "not"
    assert {e.key: e.value for e in p.profiles[0].env}["SECRET"] == "s3cr3t!"
    cats = {c.id: c for c in dst.list_categories()}
    assert cats[p.category_id].name == "API" and cats[cats[p.category_id].parent_id].name == "İş"
    assert str(new_root.resolve()) in dst.get_setting("roots")
    # ikinci içe aktarma: değişiklik yok
    plan2 = portability.plan_import(dst, portability.parse_import(raw), {"r1": str(new_root)}, "tasima-parolasi")
    assert plan2["summary"] == {"add": 0, "update": 0, "unchanged": 1, "skip": 0}


def test_wrong_passphrase_and_merge_keeps_local_secret(tmp_path):
    root = tmp_path / "code"
    (root / "order").mkdir(parents=True)
    repo = _populated(tmp_path, root)
    raw = json.dumps(portability.export_config(repo, include_secrets=True, passphrase="dogru-parola"))
    doc = portability.parse_import(raw)
    assert any("Parola hatalı" in e for e in portability.plan_import(repo, doc, {}, "yanlis-parola")["errors"])
    # parolasız birleştirme (aynı makine): yerel gizli değer silinmez
    plan = portability.plan_import(repo, doc, {}, None)
    portability.apply_import(repo, doc, plan, "merge")
    env = {e.key: e.value for e in repo.list_projects()[0].profiles[0].env}
    assert env["SECRET"] == "s3cr3t!"


def test_path_outside_roots_is_skipped(tmp_path):
    root = tmp_path / "code"
    (root / "order").mkdir(parents=True)
    doc = portability.parse_import(json.dumps(portability.export_config(_populated(tmp_path, root))))
    dst = Repository(tmp_path / "dst.db")  # kök eşlemesi yok, kök listesi boş
    plan = portability.plan_import(dst, doc, {}, None)
    assert plan["summary"]["skip"] == 1


def test_replace_mode(tmp_path):
    root = tmp_path / "code"
    (root / "order").mkdir(parents=True)
    src = _populated(tmp_path, root)
    raw = json.dumps(portability.export_config(src))
    dst = Repository(tmp_path / "dst.db")
    dst.set_setting("roots", [str(root)])
    dst.save_category(Category(name="Sadece hedefte"))
    dst.save_project(Project(name="silinecek", path=str(root)))
    doc = portability.parse_import(raw)
    portability.apply_import(dst, doc, portability.plan_import(dst, doc, {}, None), "replace")
    assert [p.name for p in dst.list_projects()] == ["order"]
    assert {c.name for c in dst.list_categories()} == {c.name for c in src.list_categories()}


@pytest.mark.parametrize("content, msg", [
    ("bozuk{", "JSON"), ('{"format": "baska"}', "Kovan"),
    ('{"format": "kovan-config", "format_version": 99}', "daha yeni"),
    ('{"format": "kovan-config", "format_version": 1, "projects": [{"uid": "u", "name": "x", '
     '"abs_path": "/x", "rel_path": "../../etc", "root": "r1", "profiles": [{}]}]}', "doğrulanamadı"),
    ('{"format": "kovan-config", "format_version": 1, "projects": [{"uid": "u", "name": "x", '
     '"abs_path": "/x", "profiles": [{"args": ["a\\u0000b"]}]}]}', "doğrulanamadı"),
    ("x" * (portability.MAX_BYTES + 1), "büyük"),
])
def test_malformed_imports_rejected(content, msg):
    with pytest.raises(portability.ImportError_, match=msg):
        portability.parse_import(content)


def test_crypto_roundtrip_and_tamper():
    blob = crypto.encrypt_json({"a": 1}, "uzun-parola")
    assert crypto.decrypt_json(blob, "uzun-parola") == {"a": 1}
    blob["ciphertext"] = blob["ciphertext"][:-4] + "AAAA"
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_json(blob, "uzun-parola")
    with pytest.raises(ValueError):
        crypto.encrypt_json({}, "kisa")


def test_backup_create_list_restore(tmp_path):
    repo = Repository(tmp_path / "kovan.db", tmp_path / "backups")
    svc = BackupService(repo, tmp_path)
    repo.save_project(Project(name="ilk", path="/a"))
    b = svc.create("manual")
    repo.save_project(Project(name="sonra", path="/b"))
    assert [x["name"] for x in svc.list()][0] == b["name"]
    res = svc.restore(b["name"])
    assert [p.name for p in repo.list_projects()] == ["ilk"]
    assert any(x["name"] == res["safety_backup"] for x in svc.list())   # geri dönüş için güvenlik yedeği
    for bad in ("../kovan.db", "auto-x/../../y.db", "baska.db"):
        with pytest.raises(BackupError):
            svc.restore(bad)


def test_auto_backup_retention_and_mirror(tmp_path):
    repo = Repository(tmp_path / "kovan.db", tmp_path / "backups")
    svc = BackupService(repo, tmp_path)
    repo.save_project(Project(name="p", path="/p", profiles=[RunProfile(env=[EnvVar(key="S", value="gizli!!", secret=True)])]))
    for _ in range(13):
        svc.create("auto")
    assert len([b for b in svc.list() if b["kind"] == "auto"]) == 10
    svc.write_mirror()
    mirror = svc.mirror_path.read_text()
    assert '"name": "p"' in mirror and "gizli!!" not in mirror


# ---------------- güvenlik incelemesi regresyon testleri ----------------
def _doc(**proj):
    base = {"uid": "u1", "name": "x", "abs_path": "/x", "profiles": [{}]}
    return {"format": "kovan-config", "format_version": 1, "projects": [{**base, **proj}]}


@pytest.mark.parametrize("rel", ["C:\\\\Windows", "..\\\\..\\\\x", "\\\\\\\\evil\\\\share\\\\p", "a/../../b", "/etc", "C:x"])
def test_windows_and_unc_rel_paths_rejected(rel):
    with pytest.raises(portability.ImportError_):
        portability.parse_import(json.dumps(_doc(root="r1", rel_path=rel)))


@pytest.mark.parametrize("abs_path", ["\\\\\\\\evil.com@80\\\\x", "//evil/share"])
def test_unc_abs_path_rejected(abs_path):
    with pytest.raises(portability.ImportError_):
        portability.parse_import(json.dumps(_doc(abs_path=abs_path)))


def test_outside_root_never_touches_filesystem(tmp_path, monkeypatch):
    dst = Repository(tmp_path / "dst.db")
    dst.set_setting("roots", [str(tmp_path / "code")])
    touched = []
    real = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: touched.append(str(self)) or real(self))
    doc = portability.parse_import(json.dumps(_doc(abs_path="/somewhere/else")))
    plan = portability.plan_import(dst, doc, {}, None)
    assert plan["projects"][0]["action"] == "skip"
    assert not any("somewhere" in t for t in touched)


@pytest.mark.parametrize("doc, msg", [
    ({"projects": [{"uid": "a", "name": "x", "abs_path": "/x", "profiles": [{}]},
                   {"uid": "a", "name": "y", "abs_path": "/y", "profiles": [{}]}]}, "Yinelenen"),
    ({"settings": {"port_range": [70000, 80000]}}, "port"),
    ({"categories": [{"uid": "a", "name": "A"}, {"uid": "b", "name": "B", "parent_uid": "a"},
                     {"uid": "c", "name": "C", "parent_uid": "b"}]}, "bir seviye"),
    ({"categories": [{"uid": "a", "name": ""}]}, "doğrulanamadı"),
])
def test_inconsistent_documents_rejected(doc, msg):
    with pytest.raises(portability.ImportError_, match=msg):
        portability.parse_import(json.dumps({"format": "kovan-config", "format_version": 1, **doc}))


def test_deeply_nested_json_rejected():
    with pytest.raises(portability.ImportError_):
        portability.parse_import('{"format": "kovan-config", "format_version": 1, "x": ' + "[" * 100000 + "]" * 100000 + "}")


def test_import_ignores_restart_settings_unless_opted_in(tmp_path):
    root = tmp_path / "code"
    (root / "p").mkdir(parents=True)
    dst = Repository(tmp_path / "dst.db")
    dst.set_setting("roots", [str(root)])
    raw = json.dumps(_doc(abs_path=str(root / "p"), profiles=[{"restart": {"policy": "always", "on_unhealthy": True}}]))
    doc = portability.parse_import(raw)
    plan = portability.plan_import(dst, doc, {}, None)
    assert plan["projects"][0]["commands"][0]["restart"] == "varsayılan"
    portability.apply_import(dst, doc, plan, "merge")
    r = dst.list_projects()[0].profiles[0].restart
    assert r.policy is None and r.on_unhealthy is False
    plan = portability.plan_import(dst, doc, {}, None, keep_watch=True)
    portability.apply_import(dst, doc, plan, "merge")
    assert dst.list_projects()[0].profiles[0].restart.policy.value == "always"


def test_preview_shows_interpreter_env_and_danger(tmp_path):
    root = tmp_path / "code"
    (root / "p").mkdir(parents=True)
    dst = Repository(tmp_path / "dst.db")
    dst.set_setting("roots", [str(root)])
    raw = json.dumps(_doc(abs_path=str(root / "p"), profiles=[{
        "cwd": "sub", "interpreter": {"kind": "custom", "path": "/opt/py/python"},
        "env": [{"key": "LD_PRELOAD", "value": "/tmp/x.so"}, {"key": "TOKEN", "value": "gizli", "secret": True}]}]))
    c = portability.plan_import(dst, portability.parse_import(raw), {}, None)["projects"][0]["commands"][0]
    assert "/opt/py/python" in c["interpreter"] and c["cwd"] == "sub"
    assert "LD_PRELOAD=/tmp/x.so" in c["env"] and "TOKEN=***" in c["env"]
    assert "LD_PRELOAD" in c["danger"] and "özel yorumlayıcı yolu" in c["danger"]


def test_failed_merge_leaves_nothing_behind(tmp_path, monkeypatch):
    root = tmp_path / "code"
    for n in ("a", "b"):
        (root / n).mkdir(parents=True)
    dst = Repository(tmp_path / "dst.db")
    dst.set_setting("roots", [str(root)])
    raw = json.dumps({"format": "kovan-config", "format_version": 1, "projects": [
        {"uid": "a", "name": "a", "abs_path": str(root / "a"), "profiles": [{}]},
        {"uid": "b", "name": "b", "abs_path": str(root / "b"), "profiles": [{}]}]})
    doc = portability.parse_import(raw)
    plan = portability.plan_import(dst, doc, {}, None)
    calls = []
    real = dst.save_project

    def flaky(p):
        calls.append(p)
        if len(calls) == 2:
            raise RuntimeError("disk dolu")
        return real(p)
    monkeypatch.setattr(dst, "save_project", flaky)
    with pytest.raises(RuntimeError):
        portability.apply_import(dst, doc, plan, "merge")
    assert dst.list_projects() == []


def test_restore_keeps_current_password_and_drops_triggers(tmp_path):
    repo = Repository(tmp_path / "kovan.db", tmp_path / "backups")
    svc = BackupService(repo, tmp_path)
    repo.set_setting("password_hash", "ESKI")
    b = svc.create("manual")
    planted = sqlite3.connect(svc.path_of(b["name"]))
    planted.execute("CREATE TRIGGER t AFTER INSERT ON audit BEGIN DELETE FROM projects; END")
    planted.commit()
    planted.close()
    repo.set_setting("password_hash", "YENI")
    svc.restore(b["name"])
    assert repo.get_setting("password_hash") == "YENI"
    assert not repo._q("SELECT 1 FROM sqlite_master WHERE type='trigger'")


def test_replace_clears_live_process_records(tmp_path):
    repo = Repository(tmp_path / "db.sqlite")
    repo.record_live(1, 1, 999999, 1.0, [], "")
    repo.replace_configuration([], [], {})
    assert repo.list_live() == []


def test_terminate_tree_never_signals_unverifiable_pid(monkeypatch):
    from kovan.infrastructure import os_adapters
    sent = []
    monkeypatch.setattr(os_adapters.os, "killpg", lambda *a: sent.append(a), raising=False)
    os_adapters.terminate_tree(2 ** 22 + 12345, 1.0, force=True)  # var olmayan PID
    assert sent == []


def test_cli_export_is_private_and_lock_is_exclusive(tmp_path):
    import os
    import stat

    from kovan import cli
    cli.write_private(tmp_path / "e.json", "{}")
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "e.json").stat().st_mode) == 0o600
    cli.acquire_lock(tmp_path)
    with pytest.raises(SystemExit):
        cli.acquire_lock(tmp_path)
    cli.release_lock(tmp_path)
    (tmp_path / cli.LOCK_NAME).write_text('{"pid": 999999999, "create_time": 1}')  # sahipsiz eski kilit
    cli.acquire_lock(tmp_path)
    cli.release_lock(tmp_path)


def test_default_location_migrates_from_legacy_folder(tmp_path, monkeypatch):
    from kovan import config
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("KOVAN_DATA_DIR", raising=False)
    legacy = config.legacy_data_dir()
    legacy.mkdir(parents=True)
    shutil.copy(V01, legacy / "pydeck.db")
    (legacy / "backups").mkdir()
    cfg = config.AppConfig()
    cfg.ensure_dirs()
    assert cfg.legacy_migration and (cfg.data_dir / "kovan.db").exists()
    assert (cfg.data_dir / "backups").is_dir() and (legacy / "pydeck.db").exists()
    cfg2 = config.AppConfig()
    cfg2.ensure_dirs()
    assert cfg2.legacy_migration is None  # ikinci açılışta tekrar kopyalanmaz

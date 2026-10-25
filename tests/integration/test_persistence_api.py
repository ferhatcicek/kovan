"""Güncelleme ve taşıma senaryoları: eski sürüm veritabanıyla açılış, dışa/içe aktarma, yedekten geri yükleme."""
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from kovan.config import AppConfig
from kovan.web.app import create_app
from helpers import BASE, ORIGIN, PORT, add_project  # noqa: F401

V01 = Path(__file__).parents[1] / "fixtures" / "db" / "v0_1.db"


def login(c, password="parola123", setup=True):
    if setup:
        c.post("/auth/setup", data={"password": password, "password2": password}, headers=ORIGIN)
    else:
        c.post("/auth/login", data={"password": password}, headers=ORIGIN)
    csrf = c.get("/").text.split('name="csrf-token" content="')[1].split('"')[0]
    c.headers.update({"X-CSRF-Token": csrf, **ORIGIN})
    return c


def test_upgrade_from_v01_keeps_everything(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(V01, data / "kovan.db")
    app = create_app(AppConfig(data_dir=data, port=PORT))
    with TestClient(app, base_url=BASE) as c:
        # v0.1'deki parola korunduğu için kurulum ekranı değil giriş ekranı gelir
        assert "Giriş yap" in c.get("/").text
        diag_ctx = app.state.ctx
        assert diag_ctx.repo.migration["from"] == 1
        names = {p.name for p in diag_ctx.repo.list_projects()}
        assert names == {"order-api", "bot"}
    backups = list((data / "backups").glob("pre-migrate-v1-to-v2-*.db"))
    assert backups, "migrasyon öncesi yedek alınmalı"
    mirror = json.loads((data / "config-latest.kovan.json").read_text())
    assert {p["name"] for p in mirror["projects"]} == {"order-api", "bot"}
    assert "gizli-123" not in json.dumps(mirror)


def test_newer_schema_shows_error_page(tmp_path):
    import sqlite3
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(V01, data / "kovan.db")
    conn = sqlite3.connect(data / "kovan.db")
    conn.execute("INSERT INTO settings(key,value) VALUES('schema_version','9')")
    conn.commit()
    conn.close()
    with TestClient(create_app(AppConfig(data_dir=data, port=PORT)), base_url=BASE) as c:
        r = c.get("/")
        assert r.status_code == 503 and "daha yeni" in r.text


def test_export_import_between_machines(authed, code_root, python_exe, tmp_path):
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", port=8123, python=python_exe)
    p = next(x for x in authed.get("/api/state").json()["projects"] if x["id"] == pid)
    p["tags"], p["notes"] = ["shop"], "taşınacak not"
    p["profiles"][0]["env"] = [{"key": "API_KEY", "value": "cok-gizli", "secret": True}]
    authed.put(f"/api/projects/{pid}", json=p)

    r = authed.post("/api/config/export", json={"include_secrets": True, "passphrase": "kisa"})
    assert r.status_code == 400
    r = authed.post("/api/config/export", json={"include_secrets": True, "passphrase": "tasima-parolasi"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    content = r.text
    assert "cok-gizli" not in content

    # "Yeni bilgisayar": farklı veri dizini ve farklı kök klasör
    new_root = tmp_path / "yeni_kok"
    shutil.copytree(code_root, new_root)
    app2 = create_app(AppConfig(data_dir=tmp_path / "data2", port=PORT))
    with TestClient(app2, base_url=BASE) as c2:
        login(c2, "baska-parola1")
        body = {"content": content, "root_map": {"r1": str(new_root)}, "passphrase": "tasima-parolasi"}
        prev = c2.post("/api/config/import/preview", json=body).json()
        assert prev["summary"]["add"] == 1 and prev["secrets_ok"] and not prev["errors"]
        assert "_secrets" not in prev and "uvicorn" in prev["projects"][0]["commands"][0]["argv"]
        assert c2.post("/api/config/import/apply", json={**body, "password": "yanlis"}).status_code == 403
        r = c2.post("/api/config/import/apply", json={**body, "password": "baska-parola1"})
        assert r.status_code == 200, r.text
        assert r.json()["backup"].startswith("pre-import-")
        st = c2.get("/api/state").json()
        proj = st["projects"][0]
        assert proj["path"] == str((new_root / "fastapi_app").resolve())
        assert proj["tags"] == ["shop"] and proj["notes"] == "taşınacak not"
        assert proj["profiles"][0]["env"][0]["value"] == "cok-gizli"
        assert str(new_root.resolve()) in st["settings"]["roots"]
        assert not st["runtimes"].get(str(proj["id"]))  # içe aktarma hiçbir şeyi otomatik başlatmaz


def test_import_rejects_bad_files(authed):
    for content in ("bozuk", json.dumps({"format": "x"}), "a" * 10):
        r = authed.post("/api/config/import/preview", json={"content": content})
        assert r.status_code == 400


def test_backup_and_restore_via_api(authed, code_root, python_exe):
    add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    b = authed.post("/api/backups").json()
    add_project(authed, code_root, "crasher", "script", "crash.py", python=python_exe)
    assert len(authed.get("/api/state").json()["projects"]) == 2
    assert authed.post("/api/backups/restore", json={"name": b["name"], "password": "x"}).status_code == 403
    assert authed.post("/api/backups/restore", json={"name": "../kovan.db", "password": "parola123"}).status_code == 400
    r = authed.post("/api/backups/restore", json={"name": b["name"], "password": "parola123"})
    assert r.status_code == 200, r.text
    assert [p["name"] for p in authed.get("/api/state").json()["projects"]] == ["script_bot"]
    names = [x["name"] for x in authed.get("/api/backups").json()["backups"]]
    assert r.json()["safety_backup"] in names


def test_diagnostics(authed):
    d = authed.get("/api/diagnostics").json()
    assert d["integrity_ok"] and d["schema_version"] == d["schema_supported"] and d["version"] == "0.2.0"


def test_data_from_previous_name_is_picked_up(tmp_path):
    """Önceki adla (pydeck.db) oluşturulmuş veri, yeniden adlandırmadan sonra kaybolmaz."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(V01, data / "pydeck.db")
    app = create_app(AppConfig(data_dir=data, port=PORT))
    with TestClient(app, base_url=BASE):
        assert {p.name for p in app.state.ctx.repo.list_projects()} == {"order-api", "bot"}
    assert (data / "kovan.db").exists() and (data / "pydeck.db").exists()  # eski kopya yerinde


def test_legacy_export_format_accepted(authed):
    doc = authed.post("/api/config/export", json={}).json()
    doc["format"] = "pydeck-config"
    r = authed.post("/api/config/import/preview", json={"content": json.dumps(doc)})
    assert r.status_code == 200

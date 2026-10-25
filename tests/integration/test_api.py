import time

import psutil
import pytest
from helpers import BASE, ORIGIN, PORT, add_project, free_port, wait_for, ws_headers  # noqa: F401


# ---------------- güvenlik ----------------
def test_wrong_host_rejected(client):
    assert client.get("/", headers={"Host": "evil.com"}).status_code == 400
    assert client.get("/", headers={"Host": f"evil.com:{PORT}"}).status_code == 400


def test_api_requires_session(client):
    assert client.get("/api/state").status_code == 401


def test_setup_only_once_and_login(client):
    client.post("/auth/setup", data={"password": "parola123", "password2": "parola123"}, headers=ORIGIN)
    client.cookies.clear()
    r = client.post("/auth/setup", data={"password": "baskaparola", "password2": "baskaparola"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303 and "kovan_session" not in r.cookies
    assert client.post("/auth/login", data={"password": "yanlis"}, headers=ORIGIN).status_code == 401
    r = client.post("/auth/login", data={"password": "parola123"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "kovan_session" in r.cookies


def test_short_password_rejected(client):
    r = client.post("/auth/setup", data={"password": "kisa", "password2": "kisa"}, headers=ORIGIN)
    assert r.status_code == 400


def test_login_rate_limit(client):
    client.post("/auth/setup", data={"password": "parola123", "password2": "parola123"}, headers=ORIGIN)
    client.cookies.clear()
    for _ in range(5):
        client.post("/auth/login", data={"password": "x"}, headers=ORIGIN)
    r = client.post("/auth/login", data={"password": "parola123"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 401  # doğru parola bile kilit süresince reddedilir


def test_csrf_and_origin(authed):
    token = authed.headers.pop("X-CSRF-Token")
    assert authed.put("/api/settings", json={"metrics_enabled": True}).status_code == 403
    authed.headers["X-CSRF-Token"] = token
    assert authed.put("/api/settings", json={"metrics_enabled": True},
                      headers={"Origin": "http://evil.com"}).status_code == 403
    assert authed.put("/api/settings", json={"metrics_enabled": True}).status_code == 200


def test_security_headers(authed):
    r = authed.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_once_token_single_use(client):
    client.post("/auth/setup", data={"password": "parola123", "password2": "parola123"}, headers=ORIGIN)
    client.cookies.clear()
    tok = client.ctx.auth.issue_once_token()
    r = client.get(f"/auth/once?token={tok}", follow_redirects=False)
    assert "kovan_session" in r.cookies
    client.cookies.clear()
    r = client.get(f"/auth/once?token={tok}", follow_redirects=False)
    assert "kovan_session" not in r.cookies


def test_websocket_rejects_foreign_origin(authed):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with authed.websocket_connect("/ws", headers=ws_headers(authed, "http://evil.com")) as ws:
            ws.receive_json()


def test_project_outside_roots_rejected(authed, tmp_path):
    r = authed.post("/api/projects", json={"name": "x", "path": str(tmp_path)})
    assert r.status_code == 400


def test_cwd_traversal_rejected(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe, cwd="../..")
    r = authed.post(f"/api/projects/{pid}/start")
    assert r.status_code == 403


def test_docs_endpoint_safety(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", python=python_exe)
    docs = [d["path"] for d in authed.get(f"/api/projects/{pid}/docs").json()["docs"]]
    assert ".env" not in docs and "README.md" in docs
    assert authed.get(f"/api/projects/{pid}/docs/view", params={"path": ".env"}).status_code == 403
    assert authed.get(f"/api/projects/{pid}/docs/view", params={"path": "../../../etc/passwd"}).status_code == 403
    html = authed.get(f"/api/projects/{pid}/docs/view", params={"path": "README.md"}).json()["html"]
    assert "<script" not in html and "<img src=\"x\"" not in html


# ---------------- süreç yönetimi ----------------
def test_script_logs_redaction_and_stop(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    r = authed.post(f"/api/projects/{pid}/start")
    assert r.status_code == 200, r.text
    lines = wait_for(lambda: [ln for ln in authed.get(f"/api/projects/{pid}/logs").json()["lines"]
                              if ln["stream"] == "err"])
    texts = [ln["text"] for ln in authed.get(f"/api/projects/{pid}/logs").json()["lines"]]
    assert any("bot başladı" in t for t in texts)
    assert all("abc123secret" not in t for t in texts)  # token=... maskelendi
    assert lines[0]["stream"] == "err"
    os_pid = authed.get("/api/state").json()["runtimes"][str(pid)]["pid"]
    r = authed.post(f"/api/projects/{pid}/stop")
    assert r.status_code == 200
    wait_for(lambda: authed.get("/api/state").json()["runtimes"][str(pid)]["state"] == "stopped")
    assert not psutil.pid_exists(os_pid) or psutil.Process(os_pid).status() == psutil.STATUS_ZOMBIE
    runs = authed.get(f"/api/projects/{pid}/runs").json()["runs"]
    assert runs[0]["ended"] is not None
    log = authed.get(f"/api/projects/{pid}/runs/{runs[0]['id']}/log")
    assert log.status_code == 200 and "tick" in log.text


def test_crash_detected_with_traceback(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "crasher", "script", "crash.py", python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    st = wait_for(lambda: (s := authed.get("/api/state").json()["runtimes"][str(pid)])["state"] == "crashed" and s)
    assert st["exit_code"] == 1
    errs = authed.get(f"/api/projects/{pid}/errors").json()["errors"]
    assert errs and errs[0]["exc_type"] == "KeyError" and "DB_URL_YOK" in errs[0]["message"]


def test_uvicorn_port_detection_and_conflict(authed, code_root, python_exe):
    port = free_port()
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", port=port, python=python_exe)
    assert authed.post(f"/api/projects/{pid}/start").status_code == 200
    wait_for(lambda: authed.get("/api/state").json()["runtimes"][str(pid)]["state"] == "running", timeout=30)
    wait_for(lambda: port in authed.get("/api/state").json()["runtimes"][str(pid)]["ports"], timeout=10)

    # Aynı portu kullanan ikinci proje çakışma bilgisiyle reddedilir
    pid2 = add_project(authed, code_root, "flask_site", "flask", "app", port=port, python=python_exe)
    r = authed.post(f"/api/projects/{pid2}/start")
    assert r.status_code == 409 and r.json()["code"] == "port_conflict"
    detail = r.json()["detail"]
    assert detail["managed_project"] == pid and detail["suggestion"] and detail["suggestion"] != port
    assert authed.get("/api/ports/check", params={"port": port}).json()["free"] is False

    authed.post(f"/api/projects/{pid}/stop")
    wait_for(lambda: authed.get("/api/state").json()["runtimes"][str(pid)]["state"] == "stopped")


def test_uvicorn_reload_tree_fully_stopped(authed, code_root, python_exe):
    port = free_port()
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", port=port, python=python_exe,
                      reload=True)
    authed.post(f"/api/projects/{pid}/start")
    wait_for(lambda: port in authed.get("/api/state").json()["runtimes"][str(pid)]["ports"], timeout=30)
    root_pid = authed.get("/api/state").json()["runtimes"][str(pid)]["pid"]
    children = psutil.Process(root_pid).children(recursive=True)
    assert children, "--reload alt süreç oluşturmalı"
    authed.post(f"/api/projects/{pid}/stop")
    wait_for(lambda: authed.get("/api/state").json()["runtimes"][str(pid)]["state"] == "stopped")
    for ch in children:
        assert not ch.is_running() or ch.status() == psutil.STATUS_ZOMBIE
    wait_for(lambda: authed.get("/api/ports/check", params={"port": port}).json()["free"])


def test_metrics_opt_in(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    with authed.websocket_connect("/ws", headers=ws_headers(authed)):  # tarama yalnız istemci bağlıyken çalışır
        time.sleep(1.5)
        assert authed.get("/api/state").json()["runtimes"][str(pid)]["metrics"] is None
        authed.put("/api/settings", json={"metrics_enabled": True})
        m = wait_for(lambda: authed.get("/api/state").json()["runtimes"][str(pid)]["metrics"], timeout=5)
        assert m["rss_mb"] > 0
    authed.post(f"/api/projects/{pid}/stop")


def test_websocket_streams_logs(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    with authed.websocket_connect("/ws", headers=ws_headers(authed)) as ws:
        ws.send_json({"op": "sub", "ids": [pid]})
        time.sleep(0.2)
        authed.post(f"/api/projects/{pid}/start")
        seen_log = seen_state = False
        for _ in range(50):
            for e in ws.receive_json()["events"]:
                seen_log |= e["type"] == "log" and "tick" in e["entry"]["text"]
                seen_state |= e["type"] == "state"
            if seen_log and seen_state:
                break
        assert seen_log and seen_state
    authed.post(f"/api/projects/{pid}/stop")


# ---------------- kategoriler / keşif / toplu işlemler ----------------
def test_categories_crud_and_bulk(authed, code_root, python_exe):
    a = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    b = add_project(authed, code_root, "crasher", "script", "crash.py", python=python_exe)
    parent = authed.post("/api/categories", json={"name": "İş", "color": "#112233"}).json()
    child = authed.post("/api/categories", json={"name": "Botlar", "color": "#445566",
                                                 "parent_id": parent["id"]}).json()
    # iki seviyeden derin hiyerarşi ve geçersiz renk reddedilir
    assert authed.post("/api/categories", json={"name": "X", "parent_id": child["id"]}).status_code == 400
    assert authed.post("/api/categories", json={"name": "Y", "color": "red"}).status_code == 400
    r = authed.post("/api/projects/bulk-update", json={"ids": [a, b], "set_category": True,
                                                       "category_id": child["id"], "add_tags": ["gece"]})
    assert r.status_code == 200
    projects = {p["id"]: p for p in authed.get("/api/state").json()["projects"]}
    assert projects[a]["category_id"] == child["id"] and projects[b]["tags"] == ["gece"]
    authed.put(f"/api/categories/{child['id']}", json={**child, "name": "Gece botları"})
    assert any(c["name"] == "Gece botları" for c in authed.get("/api/state").json()["categories"])
    authed.delete(f"/api/categories/{child['id']}")
    projects = {p["id"]: p for p in authed.get("/api/state").json()["projects"]}
    assert projects[a]["category_id"] is None

    res = authed.post("/api/bulk/start", json={"ids": [a]}).json()["results"]
    assert res[str(a)]["ok"]
    authed.post("/api/bulk/stop", json={"ids": [a]})


def test_discover_and_import(authed, code_root):
    cands = authed.get("/api/discover").json()["candidates"]
    names = {c["name"] for c in cands}
    assert {"fastapi_app", "flask_site", "script_bot", "crasher", "django_like"} <= names
    r = authed.post("/api/discover/import", json={"candidates": cands}).json()
    assert len(r["added"]) == len(cands) and not r["errors"]
    by_name = {p["name"]: p for p in authed.get("/api/state").json()["projects"]}
    cats = {c["id"]: c["name"] for c in authed.get("/api/state").json()["categories"]}
    assert cats[by_name["fastapi_app"]["category_id"]] == "Web API"
    assert all(c["already_added"] for c in authed.get("/api/discover").json()["candidates"])


def test_preview_matches_launch(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", port=8123, python=python_exe)
    prof = authed.get("/api/state").json()["projects"][0]["profiles"][0]
    prof["env"] = [{"key": "API_KEY", "value": "cok-gizli-anahtar", "secret": True}]
    prof["args"] = ["--log-level", "debug"]
    r = authed.post(f"/api/projects/{pid}/preview", json=prof).json()
    assert r["ok"] and r["argv"][-4:] == ["--port", "8123", "--log-level", "debug"]
    assert "cok-gizli-anahtar" not in str(r)


def test_websocket_requires_session(authed):
    from starlette.websockets import WebSocketDisconnect
    headers = ws_headers(authed)
    headers["Cookie"] = "kovan_session=sahte"
    with pytest.raises(WebSocketDisconnect):
        with authed.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_json()


def test_null_origin_rejected(client):
    r = client.post("/auth/setup", data={"password": "parola123", "password2": "parola123"},
                    headers={"Origin": "null"})
    assert r.status_code == 403

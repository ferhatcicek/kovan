"""Entegrasyon testleri için ortak sabitler ve yardımcılar."""
import socket
import time

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}


def add_project(c, root, folder, launcher, target, port=None, python=None, **extra):
    profile = {"launcher": launcher, "target": target, "port": port,  # extra: reload, restart, health …
               "interpreter": {"kind": "custom", "path": python} if python else {"kind": "system"}, **extra}
    r = c.post("/api/projects", json={"name": folder, "path": str(root / folder), "profiles": [profile]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def wait_for(fn, timeout=15.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("zaman aşımı")


def ws_headers(c, origin=BASE):
    """TestClient WebSocket'i base_url ve çerezleri iletmez; tarayıcının göndereceği başlıklar elle verilir."""
    return {"Origin": origin, "Host": f"127.0.0.1:{PORT}", "Cookie": f"kovan_session={c.cookies.get('kovan_session')}"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

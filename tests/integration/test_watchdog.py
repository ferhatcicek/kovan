"""Gerçek süreçlerle gözetim (watchdog) senaryoları: dışarıdan kapatma, öldürme, işçi kaybı, yetim süreçler,
çökme döngüsü, donmuş uygulama, panel yeniden açıldığında devralma ve harici süreç tespiti."""
import os
import signal
import subprocess
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from kovan.config import AppConfig
from kovan.web.app import create_app
from helpers import BASE, ORIGIN, PORT, add_project, free_port, wait_for, ws_headers  # noqa: F401

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX sinyalleri kullanılır")


def rt(c, pid):
    return c.get("/api/state").json()["runtimes"].get(str(pid), {})


def started(c, pid, timeout=15):
    return wait_for(lambda: rt(c, pid).get("state") == "running" and rt(c, pid), timeout=timeout)


def patch_profile(c, pid, **changes):
    p = next(x for x in c.get("/api/state").json()["projects"] if x["id"] == pid)
    for k, v in changes.items():
        if isinstance(v, dict):
            p["profiles"][0][k] = {**p["profiles"][0].get(k, {}), **v}
        else:
            p["profiles"][0][k] = v
    r = c.put(f"/api/projects/{pid}", json=p)
    assert r.status_code == 200, r.text


def test_external_sigterm_detected(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    os_pid = started(authed, pid)["pid"]
    os.kill(os_pid, signal.SIGTERM)  # "üçüncü parti uygulama" nazikçe kapatıyor
    st = wait_for(lambda: rt(authed, pid)["state"] == "external" and rt(authed, pid))
    assert st["end_reason"] == "external_stop" and st["signal"] == "SIGTERM"
    assert "Dışarıdan kapatıldı" in st["reason_text"]
    notes = authed.get("/api/notifications").json()["notifications"]
    assert notes and notes[0]["level"] == "warning" and "script_bot" in notes[0]["title"]
    runs = authed.get(f"/api/projects/{pid}/runs").json()["runs"]
    assert runs[0]["end_reason"] == "external_stop"


def test_external_sigkill_detected(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    os.kill(started(authed, pid)["pid"], signal.SIGKILL)
    st = wait_for(lambda: rt(authed, pid)["state"] == "crashed" and rt(authed, pid))
    assert st["end_reason"] == "killed" and st["signal"] == "SIGKILL"
    assert authed.get("/api/notifications").json()["notifications"][0]["level"] == "error"


def test_user_stop_is_not_reported_as_external(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    started(authed, pid)
    authed.post(f"/api/projects/{pid}/stop")
    st = wait_for(lambda: rt(authed, pid)["state"] == "stopped" and rt(authed, pid))
    assert st["end_reason"] == "user_stop"
    assert not authed.get("/api/notifications").json()["notifications"]


def test_reload_worker_killed_becomes_unhealthy(authed, code_root, python_exe):
    """uvicorn --reload: soket ebeveynde kalır, işçi ölür → TCP hâlâ açık ama HTTP yanıt yok → 'Yanıt vermiyor'."""
    port = free_port()
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", port=port, python=python_exe,
                      reload=True)
    authed.post(f"/api/projects/{pid}/start")
    root = started(authed, pid, timeout=30)["pid"]
    time.sleep(2.5)  # watchdog ağacı en az bir kez görsün
    for w in psutil.Process(root).children(recursive=True):
        w.kill()
    t0 = time.time()
    st = wait_for(lambda: rt(authed, pid)["state"] == "unhealthy" and rt(authed, pid), timeout=20)
    assert time.time() - t0 < 10, "işçi kaybı hızlı algılanmalı"
    assert "İşçi süreç sonlandı" in st["reason_text"] and psutil.pid_exists(root)
    authed.post(f"/api/projects/{pid}/stop")


def test_wrapper_worker_killed_becomes_unhealthy(authed, code_root, python_exe):
    """Sarmalayıcı yaşıyor ama asıl işçi öldü (venv başlatıcısı / uv run modeli) → 'Yanıt vermiyor'."""
    port = free_port()
    pid = add_project(authed, code_root, "wrapper_app", "script", "run.py", port=port, python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    root = started(authed, pid, timeout=30)["pid"]
    wait_for(lambda: authed.get("/api/ports/check", params={"port": port}).json()["free"] is False)
    time.sleep(2.5)
    for w in psutil.Process(root).children(recursive=True):
        w.kill()
    t0 = time.time()
    st = wait_for(lambda: rt(authed, pid)["state"] == "unhealthy" and rt(authed, pid), timeout=20)
    assert time.time() - t0 < 10, "işçi kaybı hızlı algılanmalı"
    assert "İşçi süreç sonlandı" in st["reason_text"] and psutil.pid_exists(root)
    assert any(n["title"].endswith("Yanıt vermiyor") for n in authed.get("/api/notifications").json()["notifications"])
    authed.post(f"/api/projects/{pid}/stop")
    wait_for(lambda: rt(authed, pid)["state"] == "stopped")


def test_root_killed_leaves_orphan_then_cleanup(authed, code_root, python_exe):
    port = free_port()
    pid = add_project(authed, code_root, "fastapi_app", "uvicorn", "main:app", port=port, python=python_exe,
                      reload=True)
    authed.post(f"/api/projects/{pid}/start")
    root = started(authed, pid, timeout=30)["pid"]
    wait_for(lambda: port in rt(authed, pid)["ports"], timeout=15)
    time.sleep(2.5)
    os.kill(root, signal.SIGKILL)  # yalnızca kök öldürülür; işçi portu tutmaya devam eder
    st = wait_for(lambda: rt(authed, pid)["state"] == "orphaned" and rt(authed, pid), timeout=15)
    assert st["orphans"] and "alt süreç hâlâ çalışıyor" in st["reason_text"]
    # Yetimler varken yeniden başlatma güvenli değil: açık hata
    r = authed.post(f"/api/projects/{pid}/start")
    assert r.status_code == 409 and r.json()["code"] == "orphaned"
    assert authed.post(f"/api/projects/{pid}/orphans/kill").status_code == 200
    wait_for(lambda: rt(authed, pid)["state"] == "stopped")
    wait_for(lambda: authed.get("/api/ports/check", params={"port": port}).json()["free"], timeout=10)


def test_on_failure_restarts_then_crash_loop(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "crasher", "script", "crash.py", python=python_exe,
                      restart={"policy": "on-failure", "max_retries": 3, "window_sec": 120})
    authed.post(f"/api/projects/{pid}/start")
    wait_for(lambda: rt(authed, pid)["state"] == "backoff")
    st = wait_for(lambda: rt(authed, pid)["state"] == "crash_loop" and rt(authed, pid), timeout=30)
    assert "Çökme döngüsü" in st["reason_text"]
    runs = authed.get(f"/api/projects/{pid}/runs").json()["runs"]
    assert len(runs) == 4 and {r["trigger"] for r in runs[:3]} == {"auto-restart"}
    assert any("Çökme döngüsü" in n["title"] for n in authed.get("/api/notifications").json()["notifications"])
    authed.post(f"/api/projects/{pid}/stop")
    assert rt(authed, pid)["state"] == "stopped"


def test_user_stop_cancels_pending_restart(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "crasher", "script", "crash.py", python=python_exe,
                      restart={"policy": "always", "max_retries": 10})
    authed.post(f"/api/projects/{pid}/start")
    wait_for(lambda: rt(authed, pid)["state"] == "backoff")
    authed.post(f"/api/projects/{pid}/stop")
    time.sleep(3)
    assert rt(authed, pid)["state"] == "stopped"


def test_hung_server_detected_by_http_health(authed, code_root, python_exe):
    port = free_port()
    pid = add_project(authed, code_root, "hang_server", "script", "server.py", port=port, python=python_exe,
                      health={"type": "http", "interval": 1, "timeout": 1, "failures": 2, "start_grace": 1})
    authed.post(f"/api/projects/{pid}/start")
    started(authed, pid, timeout=20)
    st = wait_for(lambda: rt(authed, pid)["state"] == "unhealthy" and rt(authed, pid), timeout=20)
    assert "HTTP" in st["reason_text"] and st["health"]["ok"] is False
    authed.post(f"/api/projects/{pid}/stop")


def test_panel_restart_adopts_running_process(tmp_path, code_root, python_exe):
    data = tmp_path / "data"
    app = create_app(AppConfig(data_dir=data, port=PORT, poll_interval=0.5))
    repo = app.state.ctx.repo
    repo.set_setting("roots", [str(code_root)])
    from kovan.domain.models import Project, RunProfile
    proj = repo.save_project(Project(name="bot", path=str(code_root / "script_bot"),
                                     profiles=[RunProfile(target="bot.py")]))
    # Önceki oturumda başlatılmış ve panel kapanınca yaşamaya devam etmiş süreç
    proc = subprocess.Popen([python_exe, "bot.py"], cwd=code_root / "script_bot", stdout=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        run_id = repo.start_run(proj.id, "default", "user", "")
        repo.record_live(proj.id, run_id, proc.pid, psutil.Process(proc.pid).create_time(), ["python", "bot.py"], "")
        dead_run = repo.start_run(proj.id, "default", "user", "")  # kaydı olmayan, açık kalmış çalıştırma
        with TestClient(app, base_url=BASE) as c:
            c.post("/auth/setup", data={"password": "parola123", "password2": "parola123"}, headers=ORIGIN)
            csrf = c.get("/").text.split('name="csrf-token" content="')[1].split('"')[0]
            c.headers.update({"X-CSRF-Token": csrf, **ORIGIN})
            st = rt(c, proj.id)
            assert st["state"] == "adopted" and st["pid"] == proc.pid and st["adopted"]
            assert repo.get_run(dead_run)["end_reason"] == "panel_restart"
            c.post(f"/api/projects/{proj.id}/stop")
            wait_for(lambda: proc.poll() is not None, timeout=10)
            assert rt(c, proj.id)["state"] == "stopped"
            assert repo.get_run(run_id)["end_reason"] == "user_stop"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_adopted_process_killed_externally(tmp_path, code_root, python_exe):
    app = create_app(AppConfig(data_dir=tmp_path / "d", port=PORT, poll_interval=0.5))
    repo = app.state.ctx.repo
    repo.set_setting("roots", [str(code_root)])
    from kovan.domain.models import Project, RunProfile
    proj = repo.save_project(Project(name="bot", path=str(code_root / "script_bot"), profiles=[RunProfile(target="bot.py")]))
    proc = subprocess.Popen([python_exe, "bot.py"], cwd=code_root / "script_bot", stdout=subprocess.DEVNULL)
    repo.record_live(proj.id, repo.start_run(proj.id, "default", "user", ""), proc.pid,
                     psutil.Process(proc.pid).create_time(), [], "")
    with TestClient(app, base_url=BASE) as c:
        c.post("/auth/setup", data={"password": "parola123", "password2": "parola123"}, headers=ORIGIN)
        assert rt(c, proj.id)["state"] == "adopted"
        proc.kill()
        proc.wait()
        st = wait_for(lambda: rt(c, proj.id)["state"] == "external" and rt(c, proj.id), timeout=10)
        assert "panel dışında" in st["reason_text"]


def test_external_process_detected_and_stoppable(authed, code_root, python_exe):
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    proc = subprocess.Popen([python_exe, "bot.py"], cwd=code_root / "script_bot", stdout=subprocess.DEVNULL)
    try:
        with authed.websocket_connect("/ws", headers=ws_headers(authed)):  # tarama arayüz açıkken çalışır
            ext = wait_for(lambda: rt(authed, pid).get("external"), timeout=10)
            assert ext[0]["pid"] == proc.pid and ext[0]["same_user"]
            body = {"pid": proc.pid, "create_time": ext[0]["create_time"]}
            assert authed.post(f"/api/projects/{pid}/external/stop", json=body).status_code == 400  # onaysız
            assert authed.post(f"/api/projects/{pid}/external/stop",
                               json={**body, "create_time": 1.0, "confirm": True}).status_code == 404
            r = authed.post(f"/api/projects/{pid}/external/stop", json={**body, "confirm": True})
            assert r.status_code == 200
            wait_for(lambda: proc.poll() is not None, timeout=10)
        assert any(a["action"] == "external_stop" for a in authed.get("/api/audit").json()["entries"])
    finally:
        if proc.poll() is None:
            proc.kill()


def test_merge_import_refused_while_project_busy(authed, code_root, python_exe):
    """İçe aktarma, çalışan veya yeniden başlatma bekleyen projenin komutunu değiştiremez."""
    pid = add_project(authed, code_root, "script_bot", "script", "bot.py", python=python_exe)
    authed.post(f"/api/projects/{pid}/start")
    started(authed, pid)
    exported = authed.post("/api/config/export", json={}).json()
    exported["projects"][0]["profiles"][0]["args"] = ["--yeni-komut"]
    body = {"content": __import__("json").dumps(exported), "password": "parola123"}
    r = authed.post("/api/config/import/apply", json=body)
    assert r.status_code == 409 and r.json()["code"] == "busy"
    authed.post(f"/api/projects/{pid}/stop")
    wait_for(lambda: rt(authed, pid)["state"] == "stopped")
    assert authed.post("/api/config/import/apply", json=body).status_code == 200


def test_auto_restart_uses_profile_from_user_start(authed, code_root, python_exe):
    """Çalışırken profil değişse bile otomatik yeniden başlatma, kullanıcının başlattığı komutu kullanır."""
    pid = add_project(authed, code_root, "crasher", "script", "crash.py", python=python_exe,
                      restart={"policy": "on-failure", "max_retries": 2, "window_sec": 120})
    authed.post(f"/api/projects/{pid}/start")
    wait_for(lambda: rt(authed, pid)["state"] == "backoff")
    patch_profile(authed, pid, args=["--degisti"])
    wait_for(lambda: len(authed.get(f"/api/projects/{pid}/runs").json()["runs"]) >= 2, timeout=15)
    first_lines = [l["text"] for l in authed.get(f"/api/projects/{pid}/logs").json()["lines"] if l["text"].startswith("▶")]
    assert first_lines and all("--degisti" not in t for t in first_lines)
    authed.post(f"/api/projects/{pid}/stop")

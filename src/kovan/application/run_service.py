"""Çalıştırma servisi: proje yaşam döngüsü, durum makinesi, süreç gözetimi (watchdog), port çakışması,
otomatik yeniden başlatma, bildirimler, loglar ve istatistikler.

Sonlanma tespiti iki bağımsız yoldan yapılır:
1. Bekleyici thread: kök sürecin çıkışı anında yakalanır, çıkış nedeni sınıflandırılır.
2. Watchdog (2 sn): süreç ağacı, işçi kaybı, yetim alt süreçler, sağlık kontrolü ve devralınan süreçler.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from ..config import AppConfig
from ..domain.exit_reasons import classify_exit
from ..domain.interpreters import InterpreterError, resolve_interpreter
from ..domain.launchers import LaunchConfigError, build_command
from ..domain.models import ACTIVE_STATES, LauncherType, Project, RunProfile, RunState
from ..domain.paths import PathNotAllowed, resolve_child, resolve_within
from ..domain.restart import RestartTracker, effective_policy, should_restart
from ..infrastructure import inspector, os_adapters
from ..infrastructure.db import Repository
from ..infrastructure.logstore import ProjectLog, Redactor
from ..infrastructure.supervisor import AdoptedProcess, ManagedProcess, TrackedProc, TreeTracker, _alive
from . import external_processes, health
from .event_bus import EventBus

WATCHDOG_INTERVAL = 2.0
IS_WINDOWS = os.name == "nt"


class RunError(Exception):
    def __init__(self, message: str, code: str = "run_error", detail: dict | None = None):
        super().__init__(message)
        self.code, self.detail = code, detail or {}


@dataclass
class Runtime:
    state: RunState = RunState.STOPPED
    process: ManagedProcess | AdoptedProcess | None = None
    run_id: int | None = None
    profile: str | None = None
    started_at: float | None = None
    exit_code: int | None = None
    ports: list[int] = field(default_factory=list)
    metrics: dict | None = None
    stopping: bool = False
    stop_trigger: str = ""
    end_reason: str | None = None
    reason_text: str = ""
    signal: str | None = None
    changed_at: float = field(default_factory=time.time)
    orphans: list[TrackedProc] = field(default_factory=list)
    orphan_final_state: RunState = RunState.STOPPED
    health: health.HealthState = field(default_factory=health.HealthState)
    port_seen: bool = False
    next_restart_at: float | None = None
    tracker: RestartTracker = field(default_factory=RestartTracker)
    restart_task: asyncio.Task | None = None
    external: list[dict] = field(default_factory=list)
    lost_workers: list[str] = field(default_factory=list)
    launch_profile: RunProfile | None = None  # kullanıcının başlattığı andaki profil (otomatik yeniden başlatma bunu kullanır)

    def to_dict(self) -> dict:
        proc = self.process
        return {
            "state": self.state.value, "pid": proc.pid if proc else None, "run_id": self.run_id,
            "profile": self.profile, "started_at": self.started_at, "exit_code": self.exit_code,
            "ports": self.ports, "metrics": self.metrics, "end_reason": self.end_reason,
            "reason_text": self.reason_text, "signal": self.signal, "changed_at": self.changed_at,
            "orphans": [{"pid": o.pid, "name": o.name} for o in self.orphans],
            "health": self.health.to_dict(), "next_restart_at": self.next_restart_at,
            "restart_attempts": len(self.tracker.attempts), "adopted": bool(proc and proc.adopted),
            "external": self.external,
        }


@dataclass
class PreparedLaunch:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    secrets: list[str]
    interpreter: str


class RunService:
    def __init__(self, config: AppConfig, repo: Repository, bus: EventBus):
        self.config, self.repo, self.bus = config, repo, bus
        self.runtimes: dict[int, Runtime] = {}
        self.logs: dict[int, ProjectLog] = {}
        self._sampler = inspector.MetricsSampler()
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------- yardımcılar ----------
    def roots(self) -> list[Path]:
        return [Path(r) for r in self.repo.get_setting("roots", [])]

    def runtime(self, pid: int) -> Runtime:
        return self.runtimes.setdefault(pid, Runtime())

    def log(self, pid: int) -> ProjectLog:
        if pid not in self.logs:
            self.logs[pid] = ProjectLog(self.config.log_buffer_lines)
        return self.logs[pid]

    def _lock(self, pid: int) -> asyncio.Lock:
        return self._locks.setdefault(pid, asyncio.Lock())

    def _set_state(self, pid: int, state: RunState, reason_text: str | None = None) -> None:
        rt = self.runtime(pid)
        rt.state = state
        rt.changed_at = time.time()
        if reason_text is not None:
            rt.reason_text = reason_text
        self.bus.publish({"type": "state", "project_id": pid, **rt.to_dict()})

    def _emit_line(self, pid: int, stream: str, text: str) -> None:
        entry, err = self.log(pid).append(stream, text)
        self.bus.publish({"type": "log", "project_id": pid, "entry": entry.__dict__})
        if err:
            self.bus.publish({"type": "error", "project_id": pid, "error": err.to_dict()})

    def _event(self, pid: int, kind: str, detail: str = "") -> None:
        self.repo.add_event(pid, kind, detail)

    def notify(self, project_id: int, level: str, title: str, body: str = "") -> None:
        """Bildirim kaydeder ve yayınlar. İçerikte log metni bulunmaz (sızıntı önlemi)."""
        project = self.repo.get_project(project_id)
        if project is None:
            return
        enabled = project.notify
        if enabled is None and project.category_id:
            cat = self.repo.get_category(project.category_id)
            enabled = cat.defaults.notify if cat else None
        if enabled is None:
            enabled = bool(self.repo.get_setting("notify_default", True))
        if not enabled:
            return
        n = self.repo.add_notification(project_id, level, f"{project.name}: {title}", body)
        self.bus.publish({"type": "notification", "notification": n})

    # ---------- komut hazırlama (önizleme ve başlatma aynı yolu kullanır) ----------
    def prepare(self, project: Project, profile: RunProfile) -> PreparedLaunch:
        try:
            project_dir = resolve_within(project.path, self.roots())
            if not project_dir.is_dir():
                raise RunError(f"Proje klasörü bulunamadı: {project_dir}", "not_found")
            cwd = resolve_child(project_dir, profile.cwd) if profile.cwd else project_dir
            if not cwd.is_dir():
                raise RunError(f"Çalışma dizini bulunamadı: {cwd}", "not_found")
            if profile.launcher in (LauncherType.SCRIPT, LauncherType.STREAMLIT, LauncherType.DJANGO):
                target = resolve_child(cwd, profile.target or "manage.py")
                if not target.is_file():
                    raise RunError(f"Hedef dosya bulunamadı: {target}", "not_found")
            interp = resolve_interpreter(profile.interpreter, project_dir)
            user_env = {e.key: e.value for e in profile.env}
            cmd = build_command(profile, interp.prefix, user_env)
        except PathNotAllowed as e:
            raise RunError(str(e), "path_not_allowed") from e
        except (InterpreterError, LaunchConfigError) as e:
            raise RunError(str(e), "config") from e
        env = {**os.environ, **cmd.env}
        secrets = [e.value for e in profile.env if e.secret and e.value]
        return PreparedLaunch(cmd.argv, cwd, env, secrets, interp.label)

    def preview(self, project: Project, profile: RunProfile) -> dict:
        try:
            pl = self.prepare(project, profile)
        except RunError as e:
            return {"ok": False, "error": str(e)}
        redact = Redactor(pl.secrets)
        return {"ok": True, "argv": [redact(a) for a in pl.argv], "cwd": str(pl.cwd), "interpreter": pl.interpreter}

    def _owner_of(self, os_pid: int) -> tuple[int | None, str]:
        """Bir OS sürecinin hangi projeye ait olduğunu bulur: (proje_id, 'managed'|'orphan'|'external')."""
        for pid, rt in self.runtimes.items():
            if rt.process and rt.process.pid and (os_pid == rt.process.pid or os_pid in rt.process.tracker.tree
                                                   or os_pid in inspector.tree_pids(rt.process.pid)):
                return pid, "managed"
            if any(o.pid == os_pid for o in rt.orphans):
                return pid, "orphan"
            if any(e["pid"] == os_pid for e in rt.external):
                return pid, "external"
        return None, ""

    def check_port(self, port: int, own_project: int | None = None) -> dict | None:
        if inspector.is_port_free(port):
            return None
        lmap = inspector.listening_map()
        holder = inspector.port_holder(port, lmap)
        owner, kind = self._owner_of(holder.pid) if holder and holder.pid else (None, "")
        rng = self.repo.get_setting("port_range", [8000, 8999])
        used = {p for ports in lmap.values() for p in ports}
        suggestion = inspector.suggest_free_port(max(port + 1, rng[0]), rng[1], used) or \
            inspector.suggest_free_port(rng[0], rng[1], used)
        return {"port": port, "pid": holder.pid if holder else None, "name": holder.name if holder else "",
                "cmdline": holder.cmdline if holder else "",
                "managed_project": owner if kind == "managed" else None,
                "owner_project": owner, "owner_kind": kind, "suggestion": suggestion}

    def _profile_of(self, project_id: int, rt: Runtime) -> RunProfile | None:
        if rt.launch_profile is not None:
            return rt.launch_profile
        project = self.repo.get_project(project_id)
        return project.profile(rt.profile) if project else None

    # ---------- yaşam döngüsü ----------
    def is_idle(self, project_id: int) -> bool:
        """Hiçbir süreç, yetim, bekleyen yeniden başlatma yoksa True."""
        rt = self.runtimes.get(project_id)
        if rt is None:
            return True
        pending = rt.restart_task is not None and not rt.restart_task.done()
        return rt.process is None and not rt.orphans and not pending and rt.state not in (
            RunState.BACKOFF, RunState.ORPHANED) and rt.state not in ACTIVE_STATES

    async def start(self, project_id: int, trigger: str = "user", profile_name: str | None = None,
                    profile_override: RunProfile | None = None) -> Runtime:
        async with self._lock(project_id):
            project = self.repo.get_project(project_id)
            if not project:
                raise RunError("Proje bulunamadı", "not_found")
            rt = self.runtime(project_id)
            if rt.state in ACTIVE_STATES:
                raise RunError("Proje zaten çalışıyor", "already_running")
            if rt.state == RunState.ORPHANED and any(_alive(o) for o in rt.orphans):
                raise RunError("Önceki çalıştırmadan kalan alt süreçler hâlâ çalışıyor; önce 'Yetimleri sonlandır'",
                               "orphaned", {"orphans": [o.pid for o in rt.orphans]})
            if rt.restart_task and trigger != "auto-restart":
                rt.restart_task.cancel()
            if trigger != "auto-restart":
                rt.tracker.reset()
            profile = profile_override or project.profile(profile_name)
            pl = self.prepare(project, profile)
            if profile.port is not None:
                conflict = self.check_port(profile.port, project_id)
                if conflict:
                    raise RunError(f"Port {profile.port} kullanımda", "port_conflict", conflict)

            log = self.log(project_id)
            log.redact = Redactor(pl.secrets)
            log_file = self.config.logs_dir / str(project_id) / f"{time.strftime('%Y%m%d-%H%M%S')}.log"
            rt.run_id = self.repo.start_run(project_id, profile.name, trigger, str(log_file))
            log.open_file(log_file)
            rt.profile, rt.exit_code, rt.ports, rt.metrics, rt.stopping = profile.name, None, [], None, False
            rt.end_reason, rt.signal, rt.orphans, rt.port_seen, rt.next_restart_at = None, None, [], False, None
            rt.health, rt.stop_trigger, rt.restart_task = health.HealthState(), "", None
            rt.launch_profile = profile
            self._emit_line(project_id, "sys", f"▶ {' '.join(pl.argv)}   (dizin: {pl.cwd})")
            self._set_state(project_id, RunState.STARTING, "Başlatılıyor" if trigger != "auto-restart"
                            else f"Otomatik yeniden başlatma ({len(rt.tracker.attempts)}. deneme)")

            run_id = rt.run_id
            mp = ManagedProcess(pl.argv, str(pl.cwd), pl.env,
                                on_line=lambda s, t: self._emit_line(project_id, s, t),
                                on_exit=lambda rc: self._on_exit_thread(project_id, run_id, rc))
            rt.process = mp
            try:
                mp.start()
            except OSError as e:
                rt.process = None
                self._emit_line(project_id, "sys", f"✖ Başlatılamadı: {e}")
                self.repo.end_run(run_id, None, "spawn_failed")
                log.close_file()
                rt.end_reason = "spawn_failed"
                self._set_state(project_id, RunState.CRASHED, f"Başlatılamadı: {e}")
                raise RunError(f"Başlatılamadı: {e}", "spawn_failed") from e
            rt.started_at = mp.started_at
            self.repo.record_live(project_id, run_id, mp.pid, mp.create_time, pl.argv, str(pl.cwd))
            self.repo.audit("start", f"project={project.name} profile={profile.name} trigger={trigger}")
            self._event(project_id, "start", f"{trigger} · {profile.name} · pid {mp.pid}")
            asyncio.get_running_loop().create_task(self._await_ready(project_id, mp, profile.port))
            return rt

    async def _await_ready(self, project_id: int, mp: ManagedProcess, port: int | None) -> None:
        deadline = time.time() + (30 if port else 1.0)
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            rt = self.runtime(project_id)
            if rt.process is not mp or rt.state != RunState.STARTING:
                return
            if port is not None and mp.pid:
                ports = await asyncio.to_thread(lambda: inspector.ports_for(mp.pid, inspector.listening_map()))
                if ports:
                    rt.ports, rt.port_seen = ports, True
                    break
        rt = self.runtime(project_id)
        if rt.process is mp and rt.state == RunState.STARTING and mp.alive():
            self._set_state(project_id, RunState.RUNNING, "")

    def _on_exit_thread(self, project_id: int, run_id: int, rc: int | None) -> None:
        """Bekleyici thread'den çağrılır; işi olay döngüsüne aktarır."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(self._handle_exit(project_id, run_id, rc)))

    async def _handle_exit(self, project_id: int, run_id: int, rc: int | None) -> None:
        rt = self.runtime(project_id)
        mp = rt.process
        if rt.run_id != run_id or mp is None:
            return
        profile = self._profile_of(project_id, rt)
        started = mp.started_at or 0
        had_tb = any(e.last_seen >= started for e in self.log(project_id).errors.values())
        info = classify_exit(rc, stopping=rt.stopping, had_traceback=had_tb,
                             expect_exit=bool(profile and profile.expect_exit), is_windows=IS_WINDOWS,
                             shutdown=rt.stop_trigger == "shutdown")
        orphans = await asyncio.to_thread(mp.orphans)
        if rt.stopping and orphans:  # kullanıcı durdurdu: geride kalanları da temizle
            await asyncio.to_thread(mp.tracker.kill, orphans, True)
            orphans = []

        self._emit_line(project_id, "sys", f"■ Süreç sonlandı (çıkış kodu {rc}) — {info.text}")
        self.repo.end_run(run_id, rc, info.reason, info.signal)
        self.repo.clear_live(project_id)
        self.log(project_id).close_file()
        if mp.pid:
            self._sampler.forget(mp.pid)
        rt.exit_code, rt.ports, rt.metrics, rt.process = rc, [], None, None
        rt.end_reason, rt.signal = info.reason, info.signal
        self._event(project_id, info.reason, f"kod {rc}{' · ' + info.signal if info.signal else ''}")

        if orphans:
            rt.orphans, rt.orphan_final_state = orphans, info.state
            names = ", ".join(f"{o.name}({o.pid})" for o in orphans[:4])
            text = f"Ana süreç sonlandı ({info.text}); {len(orphans)} alt süreç hâlâ çalışıyor: {names}"
            self._set_state(project_id, RunState.ORPHANED, text)
            self._event(project_id, "orphaned", names)
            self.notify(project_id, "warning", "Yetim alt süreç", text)
            return

        self._set_state(project_id, info.state, info.text)
        if info.level in ("warning", "error", "info") and info.reason not in ("user_stop", "panel_shutdown"):
            last_err = next(iter(sorted(self.log(project_id).errors.values(), key=lambda e: -e.last_seen)), None)
            body = info.text + (f" · {last_err.exc_type}" if last_err and had_tb else "")
            self.notify(project_id, info.level, {"external_stop": "Dışarıdan kapatıldı", "killed": "Öldürüldü",
                                                 "crashed": "Çöktü", "exited_ok": "Tamamlandı"}.get(info.reason,
                                                                                                  "Sonlandı"), body)
        if profile:
            self._maybe_restart(project_id, rt, profile, info.reason)

    # ---------- otomatik yeniden başlatma ----------
    def _policy(self, project_id: int, profile: RunProfile):
        project = self.repo.get_project(project_id)
        cat = self.repo.get_category(project.category_id) if project and project.category_id else None
        return effective_policy(profile.restart.policy, cat.defaults.restart_policy if cat else None,
                                self.repo.get_setting("restart_policy"))

    def _maybe_restart(self, project_id: int, rt: Runtime, profile: RunProfile, reason: str,
                       unhealthy: bool = False) -> bool:
        policy = self._policy(project_id, profile)
        if not should_restart(policy, reason, unhealthy, profile.restart.on_unhealthy):
            return False
        delay = rt.tracker.next_delay(time.time(), profile.restart.max_retries, profile.restart.window_sec)
        if delay is None:
            text = (f"Çökme döngüsü: {profile.restart.window_sec} sn içinde {profile.restart.max_retries} deneme "
                    "başarısız — otomatik başlatma durduruldu")
            self._set_state(project_id, RunState.CRASH_LOOP, text)
            self._event(project_id, "crash_loop", text)
            self.notify(project_id, "error", "Çökme döngüsü", text)
            return False
        rt.next_restart_at = time.time() + delay
        self._set_state(project_id, RunState.BACKOFF, f"{delay:.0f} sn sonra yeniden başlatılacak "
                                                      f"({len(rt.tracker.attempts)}/{profile.restart.max_retries})")
        rt.restart_task = asyncio.get_running_loop().create_task(self._delayed_start(project_id, delay))
        return True

    async def _delayed_start(self, project_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        rt = self.runtime(project_id)
        if rt.state != RunState.BACKOFF:
            return
        rt.next_restart_at = None
        try:
            await self.start(project_id, "auto-restart", profile_override=rt.launch_profile)
            self._event(project_id, "auto_restart", f"deneme {len(rt.tracker.attempts)}")
        except RunError as e:
            self._emit_line(project_id, "sys", f"✖ Otomatik yeniden başlatma başarısız: {e}")
            rt.end_reason = "crashed"
            self._set_state(project_id, RunState.CRASHED, f"Otomatik yeniden başlatma başarısız: {e}")
            profile = self._profile_of(project_id, rt)
            if profile:
                self._maybe_restart(project_id, rt, profile, "crashed")

    # ---------- durdurma ----------
    async def stop(self, project_id: int, trigger: str = "user") -> Runtime:
        async with self._lock(project_id):
            rt = self.runtime(project_id)
            if rt.restart_task and rt.restart_task is not asyncio.current_task():
                rt.restart_task.cancel()
                rt.restart_task = None
            if rt.state in (RunState.BACKOFF, RunState.CRASH_LOOP):
                rt.next_restart_at = None
                rt.tracker.reset()
                self._set_state(project_id, RunState.STOPPED, "Otomatik yeniden başlatma iptal edildi")
                return rt
            if rt.state == RunState.ORPHANED:
                await self._kill_orphans(project_id, rt)
                return rt
            mp = rt.process
            if not mp or not mp.alive():
                return rt
            profile = self._profile_of(project_id, rt)
            timeout = profile.stop_timeout if profile else 5.0
            rt.stopping, rt.stop_trigger = True, trigger
            self._set_state(project_id, RunState.STOPPING, "Durduruluyor")
            await mp.stop(timeout)
            if isinstance(mp, AdoptedProcess):
                self._finish_adopted(project_id, rt, "user_stop", "Kullanıcı tarafından durduruldu")
            else:
                await asyncio.to_thread(mp.exited.wait, 5)
            self.repo.audit("stop", f"project_id={project_id} trigger={trigger}")
            return rt

    async def _kill_orphans(self, project_id: int, rt: Runtime) -> None:
        orphans = list(rt.orphans)
        killer = rt.process.tracker if rt.process else TreeTracker()
        await asyncio.to_thread(killer.kill, orphans, False)
        for _ in range(30):
            if not any(_alive(o) for o in orphans):
                break
            await asyncio.sleep(0.1)
        await asyncio.to_thread(killer.kill, orphans, True)
        rt.orphans = []
        self._event(project_id, "orphans_killed", ", ".join(str(o.pid) for o in orphans))
        self.repo.audit("orphans_killed", f"project_id={project_id} pids={[o.pid for o in orphans]}")
        self._set_state(project_id, RunState.STOPPED, "Yetim alt süreçler sonlandırıldı")

    async def restart(self, project_id: int) -> Runtime:
        await self.stop(project_id, "restart")
        for _ in range(50):
            if self.runtime(project_id).state not in (RunState.STOPPING,):
                break
            await asyncio.sleep(0.1)
        return await self.start(project_id, "restart")

    async def stop_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*(self.stop(pid, "shutdown") for pid, rt in list(self.runtimes.items())
                               if rt.process or rt.restart_task), return_exceptions=True)

    # ---------- harici süreçler ----------
    async def stop_external(self, project_id: int, os_pid: int, create_time: float) -> None:
        rt = self.runtime(project_id)
        match = next((e for e in rt.external if e["pid"] == os_pid
                      and abs(e["create_time"] - create_time) < 1.0), None)
        if not match:
            raise RunError("Süreç bu projeye ait harici süreçler arasında bulunamadı (liste güncel olmayabilir)",
                           "not_found")
        if not match["same_user"]:
            raise RunError("Başka bir kullanıcıya ait süreç sonlandırılamaz", "forbidden")
        await asyncio.to_thread(os_adapters.terminate_tree, os_pid, create_time, False)
        for _ in range(50):
            if not _alive(TrackedProc(os_pid, create_time, "")):
                break
            await asyncio.sleep(0.1)
        if _alive(TrackedProc(os_pid, create_time, "")):
            await asyncio.to_thread(os_adapters.terminate_tree, os_pid, create_time, True)
        rt.external = [e for e in rt.external if e["pid"] != os_pid]
        self.repo.audit("external_stop", f"project_id={project_id} pid={os_pid} cmd={match['cmdline'][:200]}")
        self._event(project_id, "external_killed", f"pid {os_pid}")
        self.bus.publish({"type": "state", "project_id": project_id, **rt.to_dict()})

    # ---------- panel yeniden açıldığında kurtarma ----------
    def recover(self) -> dict:
        adopted, closed = [], []
        for rec in self.repo.list_live():
            pid, ct = rec["pid"], rec["create_time"]
            alive = ct is not None and _alive(TrackedProc(pid, ct, ""))
            if alive and self.repo.get_project(rec["project_id"]):
                ap = AdoptedProcess(pid, ct, json.loads(rec["argv"] or "[]"), rec["started"])
                rt = self.runtime(rec["project_id"])
                rt.process, rt.run_id, rt.started_at, rt.port_seen = ap, rec["run_id"], rec["started"], True
                run = self.repo.get_run(rec["run_id"]) if rec["run_id"] else None
                rt.profile = run["profile"] if run else None
                rt.state, rt.reason_text = RunState.ADOPTED, "Panel yeniden açıldığında devralındı — log akışı yok"
                self._emit_line(rec["project_id"], "sys", f"⇄ Önceki oturumdan devralındı (pid {pid}); "
                                                         "bu çalıştırmanın çıktısı görüntülenemez")
                self._event(rec["project_id"], "adopted", f"pid {pid}")
                adopted.append(rec["project_id"])
            else:
                if rec["run_id"]:
                    self.repo.end_run(rec["run_id"], None, "panel_restart")
                self.repo.clear_live(rec["project_id"])
                closed.append(rec["project_id"])
        # Kayıtsız kalmış açık çalıştırmalar (ör. panel çöktü)
        adopted_runs = {self.runtime(p).run_id for p in adopted}
        for row in self.repo._q("SELECT id FROM runs WHERE ended IS NULL"):
            if row["id"] not in adopted_runs:
                self.repo.end_run(row["id"], None, "panel_restart")
        return {"adopted": adopted, "closed": closed}

    def _finish_adopted(self, project_id: int, rt: Runtime, reason: str, text: str) -> None:
        if rt.run_id:
            self.repo.end_run(rt.run_id, None, reason)
        self.repo.clear_live(project_id)
        rt.process, rt.ports, rt.metrics, rt.end_reason = None, [], None, reason
        state = RunState.STOPPED if reason == "user_stop" else RunState.STOPPED_EXTERNALLY
        self._event(project_id, reason, "devralınan süreç")
        self._set_state(project_id, state, text)

    # ---------- watchdog ----------
    def start_polling(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self._tasks:
            self._tasks = [self._loop.create_task(self._watchdog_loop()), self._loop.create_task(self._poll_loop())]

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                findings = await asyncio.to_thread(self._watchdog_scan)
                for f in findings:
                    await self._apply_finding(*f)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - gözetim hatası paneli düşürmemeli
                self.repo.audit("watchdog_error", repr(e)[:500])
            await asyncio.sleep(WATCHDOG_INTERVAL)

    def _watchdog_scan(self) -> list[tuple]:
        """Thread'de çalışır: psutil ve sağlık kontrolleri. Durum değişiklikleri döngüde uygulanır."""
        now = time.time()
        out: list[tuple] = []
        for pid, rt in list(self.runtimes.items()):
            if rt.state == RunState.ORPHANED:
                if not any(_alive(o) for o in rt.orphans):
                    out.append((pid, "orphans_gone", None))
                continue
            mp = rt.process
            if mp is None or rt.state in (RunState.STOPPING, RunState.STARTING):
                continue
            if isinstance(mp, AdoptedProcess):
                if not mp.alive():
                    out.append((pid, "adopted_gone", None))
                    continue
            elif not mp.alive():
                continue  # bekleyici thread çıkışı işleyecek
            mp.refresh_tree()
            profile = self._profile_of(pid, rt)
            if profile is None:
                continue
            failing = rt.health.ok is False
            interval = WATCHDOG_INTERVAL if failing or mp.tracker.lost_workers else profile.health.interval
            if now - rt.health.last_check < interval - 0.1:
                continue
            res = health.run_check(profile)
            if res is None:
                continue
            ok, err = res
            lost = [f"{t.name}({t.pid})" for t in mp.tracker.take_lost()]
            out.append((pid, "health", (now, ok, err, lost)))
        return out

    async def _apply_finding(self, pid: int, kind: str, data) -> None:
        rt = self.runtime(pid)
        if kind == "orphans_gone":
            rt.orphans = []
            self._event(pid, "orphans_gone", "")
            self._set_state(pid, rt.orphan_final_state, (rt.reason_text.split(";")[0] + "; alt süreçler de sonlandı"))
            return
        if kind == "adopted_gone":
            self._finish_adopted(pid, rt, "external_stop", "Devralınan süreç panel dışında sonlandı")
            self.notify(pid, "warning", "Dışarıdan kapatıldı", "Devralınan süreç sonlandı")
            return
        if kind != "health" or rt.process is None:
            return
        now, ok, err, lost = data
        rt.health.record(now, ok, err)
        rt.lost_workers += lost
        profile = self._profile_of(pid, rt)
        if profile is None:
            return
        if ok:
            rt.port_seen = True
            if rt.lost_workers:  # işçi öldü ama uygulama kendini toparladı (ör. reloader yeni işçi başlattı)
                text = f"İşçi süreç sonlandı ve yeniden başlatıldı: {', '.join(rt.lost_workers)}"
                self._emit_line(pid, "sys", f"↻ {text}")
                self._event(pid, "worker_restarted", ", ".join(rt.lost_workers))
                rt.lost_workers = []
            if rt.state == RunState.UNHEALTHY:
                self._event(pid, "recovered", "")
                self._set_state(pid, RunState.RUNNING, "Yeniden yanıt veriyor")
                self.notify(pid, "info", "Düzeldi", "Sağlık kontrolü yeniden başarılı")
            return
        in_grace = (now - (rt.started_at or now)) < profile.health.start_grace and not rt.port_seen
        fast = bool(rt.lost_workers) and rt.port_seen  # işçi öldü ve uygulama yanıt vermiyor: kesin
        if rt.state in (RunState.RUNNING, RunState.ADOPTED) and not in_grace and (
                rt.health.failures >= profile.health.failures or fast):
            lost_txt = ", ".join(rt.lost_workers)
            text = (f"İşçi süreç sonlandı ({lost_txt}); " if lost_txt else "") + f"yanıt vermiyor: {err}"
            self._event(pid, "unhealthy", text)
            self._set_state(pid, RunState.UNHEALTHY, text)
            self.notify(pid, "warning", "Yanıt vermiyor", text)
            policy = self._policy(pid, profile)
            if should_restart(policy, "unhealthy", unhealthy=True, on_unhealthy=profile.restart.on_unhealthy):
                delay = rt.tracker.next_delay(time.time(), profile.restart.max_retries, profile.restart.window_sec)
                if delay is not None:
                    rt.restart_task = asyncio.get_running_loop().create_task(self._restart_unhealthy(pid, delay))

    async def _restart_unhealthy(self, pid: int, delay: float) -> None:
        self._emit_line(pid, "sys", "⟳ Sağlıksız: otomatik yeniden başlatılıyor")
        await self.stop(pid, "unhealthy")
        await asyncio.sleep(delay)
        try:
            await self.start(pid, "auto-restart", profile_override=self.runtime(pid).launch_profile)
        except RunError as e:
            self._emit_line(pid, "sys", f"✖ Otomatik yeniden başlatma başarısız: {e}")

    # ---------- periyodik port / metrik / harici süreç taraması (yalnız arayüz açıkken) ----------
    async def _poll_loop(self) -> None:
        while True:
            try:
                if self.bus.has_subscribers:
                    stats = await asyncio.to_thread(self.collect_stats)
                    self.bus.publish({"type": "stats", "data": stats})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.repo.audit("poll_error", repr(e)[:500])
            await asyncio.sleep(self.config.poll_interval)

    def collect_stats(self) -> dict:
        lmap = inspector.listening_map()
        global_metrics = bool(self.repo.get_setting("metrics_enabled", False))
        out: dict[str, dict] = {}
        managed: set[int] = set()
        for rt in list(self.runtimes.values()):
            mp = rt.process
            if mp and mp.pid:
                managed |= {mp.pid, *mp.tracker.tree, *inspector.tree_pids(mp.pid)}
            managed |= {o.pid for o in rt.orphans}
        folders: dict[int, Path] = {}
        roots = self.roots()
        for p in self.repo.list_projects():
            try:
                folders[p.id] = resolve_within(p.path, roots)
            except PathNotAllowed:
                continue
        try:
            external = external_processes.scan(folders, managed)
        except (psutil.Error, OSError):
            external = {}
        for p_id in folders:
            rt = self.runtime(p_id)
            ext = [e.to_dict() for e in external.get(p_id, [])]
            changed = ext != rt.external
            rt.external = ext
            mp = rt.process
            if mp and mp.pid:
                rt.ports = inspector.ports_for(mp.pid, lmap)
                if rt.ports:
                    rt.port_seen = True
                project = self.repo.get_project(p_id)
                rt.metrics = self._sampler.sample(mp.pid) if (global_metrics or (project and project.metrics_enabled)) \
                    else None
            elif rt.orphans:
                rt.ports = sorted({port for o in rt.orphans for port in lmap.get(o.pid, set())})
            if (mp and mp.pid) or rt.orphans or ext or changed:
                out[str(p_id)] = {"ports": rt.ports, "metrics": rt.metrics, "external": rt.external,
                                  "health": rt.health.to_dict()}
        return out

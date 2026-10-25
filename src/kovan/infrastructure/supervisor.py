"""Alt süreç yönetimi. subprocess.Popen + okuyucu thread'ler kullanılır; böylece Windows'ta
olay döngüsü türünden (Proactor/Selector) bağımsız çalışır. Komut her zaman argv listesidir."""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import psutil

from . import os_adapters

LineCallback = Callable[[str, str], None]
ExitCallback = Callable[[int | None], None]


@dataclass(frozen=True)
class TrackedProc:
    pid: int
    create_time: float
    name: str


def _alive(t: TrackedProc) -> bool:
    try:
        p = psutil.Process(t.pid)
        return abs(p.create_time() - t.create_time) < 1.0 and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


class TreeTracker:
    """Kök + tüm torunların anlık görüntüsü. Kök ölünce torunlar ebeveynsiz kalır; bu yüzden
    son bilinen ağaç saklanır ve 'yetim' tespiti bunun üzerinden yapılır (PID+create_time doğrulamalı)."""

    def __init__(self) -> None:
        self.tree: dict[int, TrackedProc] = {}
        self.max_workers = 0
        self.lost_workers: list[TrackedProc] = []   # bekleyen (henüz raporlanmamış) kayıp işçiler

    def refresh(self, root_pid: int, root_ct: float | None) -> None:
        try:
            root = psutil.Process(root_pid)
            if root_ct is not None and abs(root.create_time() - root_ct) > 1.0:
                return
            current = {}
            for c in root.children(recursive=True):
                try:
                    if c.status() == psutil.STATUS_ZOMBIE:  # öldürülmüş ama ebeveyni beklememiş işçi
                        continue
                    current[c.pid] = TrackedProc(c.pid, c.create_time(), c.name())
                except psutil.Error:
                    continue
        except psutil.Error:
            return
        # Bir önceki turda bilinen ama artık yaşamayan torunlar = kaybedilen işçiler
        self.lost_workers += [t for pid, t in self.tree.items() if pid not in current and not _alive(t)]
        # Ebeveyni değişmiş ama yaşayan (yetimleşmiş) torunları da izlemeye devam et
        for pid, t in self.tree.items():
            if pid not in current and _alive(t):
                current[pid] = t
        self.tree = current
        self.max_workers = max(self.max_workers, len(current))

    def take_lost(self) -> list[TrackedProc]:
        lost, self.lost_workers = self.lost_workers, []
        return lost

    def orphans(self, root_pid: int) -> list[TrackedProc]:
        found = {pid: t for pid, t in self.tree.items() if _alive(t)}
        if os.name != "nt":  # aynı süreç grubundaki (start_new_session) süreçler de bizimdir
            for proc in psutil.process_iter(["pid", "create_time", "name"]):
                try:
                    if proc.pid not in found and proc.pid != root_pid and os.getpgid(proc.pid) == root_pid:
                        found[proc.pid] = TrackedProc(proc.pid, proc.info["create_time"], proc.info["name"])
                except (psutil.Error, OSError):
                    continue
        return list(found.values())

    def kill(self, procs: list[TrackedProc], force: bool) -> None:
        for t in procs:
            if not _alive(t):
                continue
            try:
                p = psutil.Process(t.pid)
                p.kill() if force else p.terminate()
            except psutil.Error:
                pass


class ManagedProcess:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str],
                 on_line: LineCallback, on_exit: ExitCallback):
        self.argv, self.cwd, self.env = argv, cwd, env
        self._on_line, self._on_exit = on_line, on_exit
        self.proc: subprocess.Popen | None = None
        self.create_time: float | None = None
        self.started_at = 0.0
        self._readers: list[threading.Thread] = []
        self.exited = threading.Event()
        self.tracker = TreeTracker()
        self.adopted = False

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def start(self) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 - argv listesi, shell=False
            self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **os_adapters.popen_kwargs())
        self.started_at = time.time()
        os_adapters.attach_to_job(self.proc.pid)
        try:
            self.create_time = psutil.Process(self.proc.pid).create_time()
        except psutil.Error:
            self.create_time = None
        for name, stream in (("out", self.proc.stdout), ("err", self.proc.stderr)):
            t = threading.Thread(target=self._read, args=(name, stream), daemon=True)
            t.start()
            self._readers.append(t)
        threading.Thread(target=self._wait, daemon=True).start()

    def _read(self, name: str, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if "\r" in text:  # ilerleme çubukları: yalnız son hali
                    parts = [p for p in text.split("\r") if p.strip()]
                    text = parts[-1] if parts else ""
                self._on_line(name, text)
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait(self) -> None:
        assert self.proc is not None
        rc = self.proc.wait()
        for t in self._readers:
            t.join(timeout=2)
        self.exited.set()
        self._on_exit(rc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def refresh_tree(self) -> None:
        if self.proc is not None:
            self.tracker.refresh(self.proc.pid, self.create_time)

    def orphans(self) -> list[TrackedProc]:
        return self.tracker.orphans(self.proc.pid) if self.proc else []

    async def stop(self, timeout: float = 5.0) -> None:
        if not self.proc:
            return
        try:
            descendants = psutil.Process(self.proc.pid).children(recursive=True)
        except psutil.Error:
            descendants = []
        os_adapters.request_graceful_stop(self.proc)
        if not await self._wait_exit(timeout):
            os_adapters.terminate_tree(self.proc.pid, self.create_time, force=False)
            if not await self._wait_exit(2.0):
                os_adapters.terminate_tree(self.proc.pid, self.create_time, force=True)
                await self._wait_exit(2.0)
        # Ana süreç çıktıktan sonra yetim kalan alt süreçleri temizle (ör. --reload işçileri)
        for child in descendants:
            try:
                if child.is_running():
                    child.kill()
            except psutil.Error:
                pass

    async def _wait_exit(self, timeout: float) -> bool:
        return await asyncio.to_thread(self.exited.wait, timeout)


class AdoptedProcess:
    """Panel yeniden açıldığında hâlâ çalışan (önceki oturumda başlatılmış) süreç. Çıktı boruları
    kaybolduğu için log yoktur; yaşam durumu PID+create_time ile izlenir ve durdurulabilir."""

    adopted = True

    def __init__(self, pid: int, create_time: float, argv: list[str], started_at: float):
        self.pid, self.create_time, self.argv, self.started_at = pid, create_time, argv, started_at
        self.exited = threading.Event()
        self.tracker = TreeTracker()

    def alive(self) -> bool:
        ok = _alive(TrackedProc(self.pid, self.create_time, ""))
        if not ok:
            self.exited.set()
        return ok

    def refresh_tree(self) -> None:
        self.tracker.refresh(self.pid, self.create_time)

    def orphans(self) -> list[TrackedProc]:
        return self.tracker.orphans(self.pid)

    async def stop(self, timeout: float = 5.0) -> None:
        self.refresh_tree()
        descendants = list(self.tracker.tree.values())
        os_adapters.terminate_tree(self.pid, self.create_time, force=False)
        for _ in range(int(timeout * 10)):
            if not self.alive():
                break
            await asyncio.sleep(0.1)
        if self.alive():
            os_adapters.terminate_tree(self.pid, self.create_time, force=True)
            await asyncio.sleep(0.3)
            self.alive()
        self.tracker.kill(descendants, force=True)

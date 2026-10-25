"""psutil tabanlı port ve kaynak denetimi. Tüm yönetilen süreçler için tek sistem taraması yapılır."""
from __future__ import annotations

import socket
from dataclasses import dataclass

import psutil


@dataclass
class PortHolder:
    port: int
    pid: int | None
    name: str
    cmdline: str


def listening_map() -> dict[int, set[int]]:
    """pid -> dinlenen portlar. Yetki yoksa süreç bazında yedek yönteme düşer."""
    result: dict[int, set[int]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        conns = []
        for proc in psutil.process_iter():
            try:
                for c in proc.net_connections(kind="inet"):
                    conns.append(_Conn(proc.pid, c.status, c.laddr))
            except psutil.Error:
                continue
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.pid and c.laddr:
            result.setdefault(c.pid, set()).add(c.laddr.port)
    return result


@dataclass
class _Conn:
    pid: int
    status: str
    laddr: tuple


def tree_pids(pid: int) -> list[int]:
    try:
        root = psutil.Process(pid)
        return [pid] + [c.pid for c in root.children(recursive=True)]
    except psutil.Error:
        return []


def ports_for(pid: int, lmap: dict[int, set[int]]) -> list[int]:
    ports: set[int] = set()
    for p in tree_pids(pid):
        ports |= lmap.get(p, set())
    return sorted(ports)


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    # 0.0.0.0 yalnızca "başka biri tüm arayüzlerde dinliyor mu" testi için anlık bağlanır
    for h in {host, "0.0.0.0"}:  # noqa: S104
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((h, port))
            except OSError:
                return False
    return True


def port_holder(port: int, lmap: dict[int, set[int]] | None = None) -> PortHolder | None:
    lmap = lmap if lmap is not None else listening_map()
    for pid, ports in lmap.items():
        if port in ports:
            try:
                p = psutil.Process(pid)
                return PortHolder(port, pid, p.name(), " ".join(p.cmdline())[:300])
            except psutil.Error:
                return PortHolder(port, pid, "?", "")
    return None


def suggest_free_port(start: int, end: int, exclude: set[int]) -> int | None:
    for port in range(start, end + 1):
        if port not in exclude and is_port_free(port):
            return port
    return None


class MetricsSampler:
    """Süreç ağacı CPU/RAM toplamı. Process nesneleri önbelleklenir; cpu_percent farka dayanır."""

    def __init__(self) -> None:
        self._cache: dict[int, psutil.Process] = {}

    def sample(self, pid: int) -> dict:
        cpu, rss = 0.0, 0
        alive: set[int] = set()
        for p_pid in tree_pids(pid):
            proc = self._cache.get(p_pid)
            try:
                if proc is None:
                    proc = psutil.Process(p_pid)
                    proc.cpu_percent(None)  # ilk çağrı taban değeri belirler
                    self._cache[p_pid] = proc
                with proc.oneshot():
                    cpu += proc.cpu_percent(None)
                    rss += proc.memory_info().rss
                alive.add(p_pid)
            except psutil.Error:
                self._cache.pop(p_pid, None)
        return {"cpu": round(cpu / max(psutil.cpu_count() or 1, 1), 1), "rss_mb": round(rss / 1048576, 1),
                "procs": len(alive)}

    def forget(self, pid: int) -> None:
        for p in tree_pids(pid) + [pid]:
            self._cache.pop(p, None)

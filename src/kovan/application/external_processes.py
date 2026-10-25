"""Panel dışından başlatılmış (ör. eski bir CLI penceresi) ve bir proje klasöründe çalışan süreçlerin tespiti."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath

import psutil

from ..infrastructure import inspector

_INTERESTING = ("python", "pythonw", "py", "uv", "uvicorn", "gunicorn", "hypercorn", "waitress", "flask",
                "streamlit", "celery", "jupyter", "django-admin")


@dataclass
class ExternalProc:
    pid: int
    create_time: float
    name: str
    cmdline: str
    cwd: str
    ports: list[int]
    same_user: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _interesting(name: str) -> bool:
    base = name.lower().removesuffix(".exe")
    return any(base == n or base.startswith(n) for n in _INTERESTING)


def _within(path: str, base: Path) -> bool:
    try:
        return PurePath(os.path.normcase(os.path.abspath(path))).is_relative_to(os.path.normcase(str(base)))
    except (ValueError, OSError):
        return False


def current_user() -> str | None:
    try:
        return psutil.Process().username()
    except psutil.Error:
        return None


def scan(projects: dict[int, Path], exclude_pids: set[int]) -> dict[int, list[ExternalProc]]:
    """projects: {proje_id: klasör}. Erişim izni olmayan süreçler sessizce atlanır."""
    me, own = current_user(), os.getpid()
    result: dict[int, list[ExternalProc]] = {}
    lmap: dict[int, set[int]] | None = None
    for proc in psutil.process_iter(["pid", "name", "create_time", "username"]):
        try:
            info = proc.info
            if proc.pid in exclude_pids or proc.pid == own or not _interesting(info["name"] or ""):
                continue
            cmd = proc.cmdline()
            try:
                cwd = proc.cwd()
            except psutil.Error:
                cwd = ""
        except psutil.Error:
            continue
        for pid, folder in projects.items():
            paths = [cwd] + [a for a in cmd[1:] if os.path.isabs(a)]
            if any(p and _within(p, folder) for p in paths):
                if lmap is None:
                    lmap = inspector.listening_map()
                result.setdefault(pid, []).append(ExternalProc(
                    proc.pid, info["create_time"], info["name"], " ".join(cmd)[:400], cwd,
                    sorted(lmap.get(proc.pid, set())), info.get("username") == me))
                break
    # Bir harici sürecin harici çocuklarını ayrı ayrı göstermek yerine en üstteki süreci tut
    for pid, procs in result.items():
        pids = {p.pid for p in procs}
        tops = []
        for p in procs:
            try:
                parent = psutil.Process(p.pid).ppid()
            except psutil.Error:
                parent = None
            if parent not in pids:
                try:
                    kids = {c.pid for c in psutil.Process(p.pid).children(recursive=True)}
                except psutil.Error:
                    kids = set()
                child_ports = {port for q in procs if q.pid in kids for port in q.ports}
                p.ports = sorted(set(p.ports) | child_ports)
                tops.append(p)
        result[pid] = tops
    return result

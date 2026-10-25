"""Proje keşfi: dosyaları yalnızca OKUR. Proje kodu asla import edilmez veya çalıştırılmaz."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..domain.interpreters import VENV_DIRS
from ..domain.launchers import LAUNCHERS
from ..domain.models import LauncherType
from ..infrastructure.markdown import first_paragraph

MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "manage.py", "uv.lock")
SKIP_DIRS = {*VENV_DIRS, ".git", "node_modules", "__pycache__", ".idea", ".vscode", "dist", "build",
             ".mypy_cache", ".pytest_cache", "site-packages"}
MAX_FILE = 1024 * 1024
MAX_FILES = 60

_FASTAPI = re.compile(r"^(\w+)\s*(?::\s*\w+\s*)?=\s*(?:fastapi\.)?(?:FastAPI|Starlette)\(", re.M)
_FLASK = re.compile(r"^(\w+)\s*=\s*(?:flask\.)?Flask\(", re.M)
_STREAMLIT = re.compile(r"^\s*import streamlit|^\s*from streamlit", re.M)
_MAIN = re.compile(r"""^if __name__\s*==\s*['"]__main__['"]""", re.M)
_PORT_HINT = re.compile(r"port\s*=\s*(\d{4,5})")


@dataclass
class Candidate:
    name: str
    path: str
    launcher: str
    target: str
    confidence: float
    category: str
    description: str = ""
    port: int | None = None
    has_venv: bool = False
    already_added: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _py_files(project: Path) -> list[Path]:
    files: list[Path] = []
    for sub in ("", "app", "src", "api", "backend"):
        d = project / sub if sub else project
        if d.is_dir():
            files += sorted(p for p in d.glob("*.py") if p.is_file())
            if sub == "src":  # src/<paket>/*.py
                for pkg in sorted(d.iterdir()):
                    if pkg.is_dir() and pkg.name not in SKIP_DIRS:
                        files += sorted(pkg.glob("*.py"))
    return files[:MAX_FILES]


_PREFERRED = ("main.py", "app.py", "server.py", "api.py", "run.py", "bot.py", "streamlit_app.py")


def _rank(p: Path) -> int:
    return _PREFERRED.index(p.name) if p.name in _PREFERRED else len(_PREFERRED)


def detect(project: Path) -> Candidate | None:
    files = _py_files(project)
    has_marker = any((project / m).exists() for m in MARKERS)
    if not files and not has_marker:
        return None
    readme = next((project / n for n in ("README.md", "README.rst", "README.txt", "README", "readme.md")
                   if (project / n).is_file()), None)
    desc = first_paragraph(_read(readme)) if readme else ""
    has_venv = any((project / v).is_dir() for v in VENV_DIRS)

    def cand(lt: LauncherType, target: str, conf: float, reason: str, port: int | None = None) -> Candidate:
        return Candidate(project.name, str(project), lt.value, target, conf, LAUNCHERS[lt].default_category,
                         desc, port, has_venv, reasons=[reason])

    if (project / "manage.py").is_file():
        return cand(LauncherType.DJANGO, "manage.py", 0.95, "manage.py bulundu", 8000)

    found: list[Candidate] = []
    for f in sorted(files, key=_rank):
        src = _read(f)
        if not src:
            continue
        rel = f.relative_to(project)
        mod = ".".join(rel.with_suffix("").parts)
        port_m = _PORT_HINT.search(src)
        port = int(port_m.group(1)) if port_m and 1 <= int(port_m.group(1)) <= 65535 else None
        if m := _FASTAPI.search(src):
            found.append(cand(LauncherType.UVICORN, f"{mod}:{m.group(1)}", 0.9, f"{rel}: FastAPI/Starlette",
                              port or 8000))
        elif m := _FLASK.search(src):
            target = mod if m.group(1) == "app" else f"{mod}:{m.group(1)}"
            found.append(cand(LauncherType.FLASK, target, 0.85, f"{rel}: Flask", port or 5000))
        elif _STREAMLIT.search(src):
            found.append(cand(LauncherType.STREAMLIT, str(rel.as_posix()), 0.85, f"{rel}: streamlit", port or 8501))
        elif _MAIN.search(src):
            found.append(cand(LauncherType.SCRIPT, str(rel.as_posix()), 0.6, f"{rel}: __main__ bloğu", port))
    if found:
        best = max(found, key=lambda c: c.confidence)
        return best
    if files:
        f = sorted(files, key=_rank)[0]
        return cand(LauncherType.SCRIPT, f.relative_to(project).as_posix(), 0.3, "Python dosyası bulundu")
    return cand(LauncherType.CUSTOM, "", 0.1, "Proje dosyası bulundu, giriş noktası belirlenemedi")


def scan_roots(roots: list[Path], depth: int = 2) -> list[Candidate]:
    out: list[Candidate] = []

    def walk(d: Path, level: int) -> None:
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir() and p.name not in SKIP_DIRS
                              and not p.name.startswith("."))
        except OSError:
            return
        for child in children:
            c = detect(child)
            if c:
                out.append(c)
            elif level < depth:
                walk(child, level + 1)

    for root in roots:
        if root.is_dir():
            walk(root, 1)
    return out

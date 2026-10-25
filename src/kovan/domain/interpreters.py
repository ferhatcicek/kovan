"""Yorumlayıcı çözümleyici: profil + proje klasöründen çalıştırılacak Python önekini üretir."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import InterpreterConfig, InterpreterKind

VENV_DIRS = (".venv", "venv", "env")


class InterpreterError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedInterpreter:
    kind: InterpreterKind
    prefix: list[str]  # ör. ["C:/proj/.venv/Scripts/python.exe"] veya ["uv", "run", "python"]
    label: str


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def find_venv(project_dir: Path) -> Path | None:
    for name in VENV_DIRS:
        py = venv_python(project_dir / name)
        if py.exists():
            return py
    return None


def _system_python() -> str | None:
    # Paketlenmiş exe'de sys.executable panelin kendisidir; bu yüzden PATH'e bakılır.
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _validate_custom(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise InterpreterError(f"Yorumlayıcı bulunamadı: {path}")
    if not re.fullmatch(r"python(\d+(\.\d+)?)?w?(\.exe)?", p.name, re.IGNORECASE):
        raise InterpreterError("Özel yorumlayıcı bir python çalıştırılabilir dosyası olmalı")
    # resolve() KULLANILMAZ: venv içindeki python bir symlink'tir; çözülürse venv bağlamı kaybolur
    return str(p.absolute())


def resolve_interpreter(cfg: InterpreterConfig, project_dir: Path) -> ResolvedInterpreter:
    kind = cfg.kind
    if kind == InterpreterKind.AUTO:
        if find_venv(project_dir):
            kind = InterpreterKind.VENV
        elif (project_dir / "uv.lock").exists() and shutil.which("uv"):
            kind = InterpreterKind.UV
        else:
            kind = InterpreterKind.SYSTEM

    if kind == InterpreterKind.VENV:
        py = find_venv(project_dir)
        if not py:
            raise InterpreterError("Proje klasöründe sanal ortam (.venv/venv/env) bulunamadı")
        return ResolvedInterpreter(kind, [str(py)], f"venv ({py.parent.parent.name})")
    if kind == InterpreterKind.UV:
        uv = shutil.which("uv")
        if not uv:
            raise InterpreterError("uv PATH üzerinde bulunamadı")
        return ResolvedInterpreter(kind, [uv, "run", "--project", str(project_dir), "python"], "uv run")
    if kind == InterpreterKind.CUSTOM:
        return ResolvedInterpreter(kind, [_validate_custom(cfg.path)], "özel")
    py = _system_python()
    if not py:
        raise InterpreterError("Sistemde python bulunamadı (PATH)")
    return ResolvedInterpreter(InterpreterKind.SYSTEM, [py], "sistem")

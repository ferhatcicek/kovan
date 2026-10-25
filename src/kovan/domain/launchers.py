"""Launcher stratejileri: her çalıştırma biçimi için argv üretimi ve port enjeksiyonu.

Komutlar daima token listesi olarak üretilir; hiçbir zaman shell kullanılmaz.
Argümanlarda yalnızca `{port}` ve `{host}` yer tutucuları desteklenir.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import LauncherType, RunProfile

FORCED_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "FORCE_COLOR": "1",
}


class LaunchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    argv: list[str]
    env: dict[str, str]


def _substitute(args: list[str], profile: RunProfile) -> list[str]:
    port = "" if profile.port is None else str(profile.port)
    out = []
    for a in args:
        if "{port}" in a and profile.port is None:
            raise LaunchConfigError("Argümanlarda {port} kullanılmış ama port tanımlı değil")
        out.append(a.replace("{port}", port).replace("{host}", profile.host))
    return out


class LauncherStrategy(ABC):
    type: LauncherType
    label: str
    default_category: str
    required_module: str | None = None
    target_hint: str = ""

    def build(self, profile: RunProfile, python: list[str]) -> Command:
        if not profile.target.strip() and self.type != LauncherType.CUSTOM:
            raise LaunchConfigError(f"{self.label} için hedef belirtilmeli ({self.target_hint})")
        argv = self.argv(profile, python) + _substitute(profile.args, profile)
        env = dict(FORCED_ENV)
        if profile.port is not None:
            env["PORT"] = str(profile.port)
        return Command(argv, env)

    @abstractmethod
    def argv(self, profile: RunProfile, python: list[str]) -> list[str]: ...


class ScriptLauncher(LauncherStrategy):
    type, label, default_category, target_hint = LauncherType.SCRIPT, "Python betiği", "Bot/Script", "ör. main.py"

    def argv(self, p, python):
        return [*python, p.target]


class ModuleLauncher(LauncherStrategy):
    type, label, default_category, target_hint = LauncherType.MODULE, "python -m", "Bot/Script", "ör. paket.modul"

    def argv(self, p, python):
        return [*python, "-m", p.target]


class UvicornLauncher(LauncherStrategy):
    type, label, default_category, target_hint = LauncherType.UVICORN, "Uvicorn (ASGI)", "Web API", "ör. main:app"
    required_module = "uvicorn"

    def argv(self, p, python):
        cmd = [*python, "-m", "uvicorn", p.target, "--host", p.host]
        if p.port is not None:
            cmd += ["--port", str(p.port)]
        if p.reload:
            cmd.append("--reload")
        return cmd


class FlaskLauncher(LauncherStrategy):
    type, label, default_category, target_hint = LauncherType.FLASK, "Flask", "Web API", "ör. app veya app:create_app"
    required_module = "flask"

    def argv(self, p, python):
        cmd = [*python, "-m", "flask", "--app", p.target, "run", "--host", p.host]
        if p.port is not None:
            cmd += ["--port", str(p.port)]
        if p.reload:
            cmd.append("--debug")
        return cmd


class DjangoLauncher(LauncherStrategy):
    type, label, default_category, target_hint = LauncherType.DJANGO, "Django runserver", "Web API", "ör. manage.py"
    required_module = "django"

    def argv(self, p, python):
        cmd = [*python, p.target or "manage.py", "runserver"]
        if p.port is not None:
            cmd.append(f"{p.host}:{p.port}")
        if not p.reload:
            cmd.append("--noreload")
        return cmd


class StreamlitLauncher(LauncherStrategy):
    type, label, default_category, target_hint = LauncherType.STREAMLIT, "Streamlit", "Web UI", "ör. app.py"
    required_module = "streamlit"

    def argv(self, p, python):
        cmd = [*python, "-m", "streamlit", "run", p.target,
               "--server.headless", "true", "--server.address", p.host]
        if p.port is not None:
            cmd += ["--server.port", str(p.port)]
        return cmd


class CustomLauncher(LauncherStrategy):
    """Kullanıcı argv'si. `{python}` token'ı seçili yorumlayıcı önekine genişler."""

    type, label, default_category, target_hint = LauncherType.CUSTOM, "Özel komut", "", "argümanlara komut yazın"

    def build(self, profile: RunProfile, python: list[str]) -> Command:
        if not profile.args:
            raise LaunchConfigError("Özel komut için en az bir argüman gerekli (ör. {python} -m myapp)")
        argv: list[str] = []
        for tok in _substitute(profile.args, profile):
            argv.extend(python if tok == "{python}" else [tok])
        env = dict(FORCED_ENV)
        if profile.port is not None:
            env["PORT"] = str(profile.port)
        return Command(argv, env)

    def argv(self, p, python):  # pragma: no cover - build() geçersiz kılındı
        return []


LAUNCHERS: dict[LauncherType, LauncherStrategy] = {
    launcher.type: launcher
    for launcher in (ScriptLauncher(), ModuleLauncher(), UvicornLauncher(), FlaskLauncher(),
              DjangoLauncher(), StreamlitLauncher(), CustomLauncher())
}


def build_command(profile: RunProfile, python: list[str], extra_env: dict[str, str] | None = None) -> Command:
    cmd = LAUNCHERS[profile.launcher].build(profile, python)
    env = {**(extra_env or {}), **cmd.env}
    return Command(cmd.argv, env)


def launcher_catalog() -> list[dict]:
    return [
        {"type": x.type.value, "label": x.label, "hint": x.target_hint, "category": x.default_category}
        for x in LAUNCHERS.values()
    ]

from pathlib import Path

import pytest

from kovan.domain.interpreters import InterpreterError, resolve_interpreter
from kovan.domain.launchers import LaunchConfigError, build_command
from kovan.domain.models import EnvVar, InterpreterConfig, InterpreterKind, LauncherType, Project, RunProfile
from kovan.domain.paths import PathNotAllowed, resolve_child, resolve_within

PY = ["/venv/bin/python"]


@pytest.mark.parametrize("profile, expected", [
    (RunProfile(launcher=LauncherType.SCRIPT, target="x.py", args=["--v"]), [*PY, "x.py", "--v"]),
    (RunProfile(launcher=LauncherType.MODULE, target="pkg.cli"), [*PY, "-m", "pkg.cli"]),
    (RunProfile(launcher=LauncherType.UVICORN, target="main:app", port=8001, reload=True),
     [*PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8001", "--reload"]),
    (RunProfile(launcher=LauncherType.FLASK, target="app", port=5001),
     [*PY, "-m", "flask", "--app", "app", "run", "--host", "127.0.0.1", "--port", "5001"]),
    (RunProfile(launcher=LauncherType.DJANGO, target="manage.py", port=8002),
     [*PY, "manage.py", "runserver", "127.0.0.1:8002", "--noreload"]),
    (RunProfile(launcher=LauncherType.STREAMLIT, target="ui.py", port=8501),
     [*PY, "-m", "streamlit", "run", "ui.py", "--server.headless", "true", "--server.address", "127.0.0.1",
      "--server.port", "8501"]),
    (RunProfile(launcher=LauncherType.CUSTOM, args=["{python}", "-m", "http.server", "{port}"], port=9000),
     [*PY, "-m", "http.server", "9000"]),
    (RunProfile(launcher=LauncherType.SCRIPT, target="bot.py", args=["--port={port}"], port=7000),
     [*PY, "bot.py", "--port=7000"]),
])
def test_launcher_argv(profile, expected):
    cmd = build_command(profile, PY)
    assert cmd.argv == expected
    assert cmd.env["PYTHONUNBUFFERED"] == "1"
    if profile.port:
        assert cmd.env["PORT"] == str(profile.port)


def test_shell_metacharacters_stay_single_tokens():
    cmd = build_command(RunProfile(target="x.py", args=["a & del *", "$(rm -rf /)"]), PY)
    assert cmd.argv[-2:] == ["a & del *", "$(rm -rf /)"]


def test_port_placeholder_requires_port():
    with pytest.raises(LaunchConfigError):
        build_command(RunProfile(target="x.py", args=["{port}"]), PY)


def test_target_required():
    with pytest.raises(LaunchConfigError):
        build_command(RunProfile(launcher=LauncherType.UVICORN), PY)


def test_forced_env_overrides_user_env():
    cmd = build_command(RunProfile(target="x.py"), PY, {"PYTHONUNBUFFERED": "0", "X": "1"})
    assert cmd.env["PYTHONUNBUFFERED"] == "1" and cmd.env["X"] == "1"


def test_model_validation():
    with pytest.raises(ValueError):
        RunProfile(port=70000)
    with pytest.raises(ValueError):
        EnvVar(key="BAD KEY")
    with pytest.raises(ValueError):
        RunProfile(host="1.1.1.1; rm")
    p = Project(name="x", path="proje", tags=["#a", "a", " b "])
    assert p.tags == ["a", "b"]


def test_resolve_within(tmp_path):
    root = tmp_path / "code"
    (root / "p").mkdir(parents=True)
    assert resolve_within(root / "p", [root]) == (root / "p").resolve()
    with pytest.raises(PathNotAllowed):
        resolve_within(tmp_path, [root])
    with pytest.raises(PathNotAllowed):
        resolve_within(root / ".." / "x", [root])


def test_resolve_within_symlink_escape(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    try:
        (root / "link").symlink_to(tmp_path)
    except OSError:
        pytest.skip("Bu sistemde sembolik bağ oluşturma izni yok")
    with pytest.raises(PathNotAllowed):
        resolve_within(root / "link", [root])


def test_resolve_child(tmp_path):
    assert resolve_child(tmp_path, "a/b.py") == (tmp_path / "a" / "b.py").resolve()
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(PathNotAllowed):
            resolve_child(tmp_path, bad)


def test_interpreter_resolution(tmp_path, python_exe):
    with pytest.raises(InterpreterError):
        resolve_interpreter(InterpreterConfig(kind=InterpreterKind.VENV), tmp_path)
    custom = resolve_interpreter(InterpreterConfig(kind=InterpreterKind.CUSTOM, path=python_exe), tmp_path)
    assert custom.prefix == [str(Path(python_exe).absolute())]
    fake = tmp_path / "notepad.exe"
    fake.write_text("")
    with pytest.raises(InterpreterError):
        resolve_interpreter(InterpreterConfig(kind=InterpreterKind.CUSTOM, path=str(fake)), tmp_path)


def test_auto_prefers_venv(tmp_path):
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    r = resolve_interpreter(InterpreterConfig(), tmp_path)
    assert r.kind == InterpreterKind.VENV and r.prefix == [str(py)]

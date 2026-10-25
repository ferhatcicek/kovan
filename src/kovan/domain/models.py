"""Domain modelleri: saf veri yapıları, I/O içermez."""
from __future__ import annotations

import re
import uuid
from enum import Enum

from pydantic import BaseModel as _PydanticBase
from pydantic import ConfigDict, Field, field_validator


class BaseModel(_PydanticBase):
    """Tüm domain modelleri bilinmeyen alanları yok sayar: daha yeni/eski sürümün verisi okunabilir kalır."""

    model_config = ConfigDict(extra="ignore")


def new_uid() -> str:
    return uuid.uuid4().hex


class LauncherType(str, Enum):
    SCRIPT = "script"
    MODULE = "module"
    UVICORN = "uvicorn"
    FLASK = "flask"
    DJANGO = "django"
    STREAMLIT = "streamlit"
    CUSTOM = "custom"


class InterpreterKind(str, Enum):
    AUTO = "auto"
    VENV = "venv"
    SYSTEM = "system"
    UV = "uv"
    CUSTOM = "custom"


class RunState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"          # canlı ama port/sağlık kontrolü yanıt vermiyor
    STOPPING = "stopping"
    STOPPED_EXTERNALLY = "external"  # panel dışından kapatıldı
    CRASHED = "crashed"              # çöktü veya öldürüldü (end_reason ayırt eder)
    ORPHANED = "orphaned"            # ana süreç bitti, alt süreçler yaşıyor
    BACKOFF = "backoff"              # otomatik yeniden başlatma bekleniyor
    CRASH_LOOP = "crash_loop"        # deneme sınırı aşıldı
    ADOPTED = "adopted"              # panel yeniden açıldığında devralındı (log yok)


ACTIVE_STATES = {RunState.STARTING, RunState.RUNNING, RunState.UNHEALTHY, RunState.STOPPING, RunState.ADOPTED}


class HealthType(str, Enum):
    NONE = "none"
    AUTO = "auto"   # portlu profilde TCP, değilse yok
    TCP = "tcp"
    HTTP = "http"


class RestartPolicy(str, Enum):
    NEVER = "never"
    ON_FAILURE = "on-failure"
    ALWAYS = "always"


class EnvVar(BaseModel):
    key: str
    value: str = ""
    secret: bool = False

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):
            raise ValueError(f"Geçersiz ortam değişkeni adı: {v!r}")
        return v


class InterpreterConfig(BaseModel):
    kind: InterpreterKind = InterpreterKind.AUTO
    path: str = ""  # yalnız CUSTOM için


class HealthConfig(BaseModel):
    type: HealthType = HealthType.AUTO
    path: str = "/"            # HTTP için yol; host daima 127.0.0.1
    interval: float = 5.0
    timeout: float = 3.0
    start_grace: float = 20.0  # başlangıçta bu süre boyunca başarısızlık sayılmaz
    failures: int = 3          # ardışık başarısızlık eşiği

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or "://" in v or any(c in v for c in "\r\n\x00 "):
            raise ValueError("Sağlık yolu '/' ile başlamalı ve yalnızca yol içermeli")
        return v

    @field_validator("interval", "timeout", "start_grace")
    @classmethod
    def _pos(cls, v: float) -> float:
        if not 0.5 <= v <= 600:
            raise ValueError("Süre 0.5-600 sn aralığında olmalı")
        return v


class RestartConfig(BaseModel):
    policy: RestartPolicy | None = None  # None = kategori/global varsayılanı kullan
    max_retries: int = 5
    window_sec: int = 300
    on_unhealthy: bool = False

    @field_validator("max_retries")
    @classmethod
    def _retries(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("Deneme sınırı 1-100 olmalı")
        return v


class RunProfile(BaseModel):
    """Bir projenin nasıl çalıştırılacağı. argv her zaman token listesi olarak tutulur (shell yok)."""

    name: str = "default"
    launcher: LauncherType = LauncherType.SCRIPT
    target: str = ""  # x.py | paket.modul | main:app | manage.py
    args: list[str] = Field(default_factory=list)
    port: int | None = None
    host: str = "127.0.0.1"
    reload: bool = False
    cwd: str = ""  # proje klasörüne göre göreli; boş = proje kökü
    env: list[EnvVar] = Field(default_factory=list)
    interpreter: InterpreterConfig = Field(default_factory=InterpreterConfig)
    stop_timeout: float = 5.0
    expect_exit: bool = False  # kısa ömürlü betik: kod 0 ile bitmesi "tamamlandı" sayılır
    health: HealthConfig = Field(default_factory=HealthConfig)
    restart: RestartConfig = Field(default_factory=RestartConfig)

    @field_validator("port")
    @classmethod
    def _valid_port(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError("Port 1-65535 aralığında olmalı")
        return v

    @field_validator("host")
    @classmethod
    def _valid_host(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]+", v):
            raise ValueError("Geçersiz host")
        return v

    @field_validator("args")
    @classmethod
    def _no_nul(cls, v: list[str]) -> list[str]:
        if any("\x00" in a for a in v):
            raise ValueError("Argümanlarda NUL karakteri olamaz")
        return v


class CategoryDefaults(BaseModel):
    restart_policy: RestartPolicy | None = None
    notify: bool | None = None


class Category(BaseModel):
    id: int | None = None
    uid: str = Field(default_factory=new_uid)
    name: str
    color: str = "#6366f1"
    icon: str = ""
    parent_id: int | None = None
    sort: int = 0
    description: str = ""
    defaults: CategoryDefaults = Field(default_factory=CategoryDefaults)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 60:
            raise ValueError("Kategori adı 1-60 karakter olmalı")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("Renk #RRGGBB biçiminde olmalı")
        return v


class Project(BaseModel):
    id: int | None = None
    uid: str = Field(default_factory=new_uid)
    name: str
    path: str
    category_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    favorite: bool = False
    notes: str = ""
    metrics_enabled: bool = False
    notify: bool | None = None  # None = kategori/global varsayılanı; False = sessize alındı
    profiles: list[RunProfile] = Field(default_factory=lambda: [RunProfile()])
    active_profile: str = "default"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 80:
            raise ValueError("Proje adı 1-80 karakter olmalı")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = t.strip().lstrip("#")
            if t and t not in out:
                if len(t) > 30:
                    raise ValueError("Etiket en fazla 30 karakter")
                out.append(t)
        return out

    def profile(self, name: str | None = None) -> RunProfile:
        wanted = name or self.active_profile
        for p in self.profiles:
            if p.name == wanted:
                return p
        return self.profiles[0]

"""Sağlık kontrolleri. Hedef her zaman 127.0.0.1'dir (SSRF koruması); yönlendirmeler takip edilmez."""
from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass, field

from ..domain.models import HealthConfig, HealthType, LauncherType, RunProfile

HISTORY = 20


@dataclass
class HealthState:
    ok: bool | None = None
    failures: int = 0
    last_check: float = 0.0
    last_error: str = ""
    history: list[tuple[float, bool]] = field(default_factory=list)

    def record(self, ts: float, ok: bool, error: str = "") -> None:
        self.ok, self.last_check, self.last_error = ok, ts, error
        self.failures = 0 if ok else self.failures + 1
        self.history = (self.history + [(ts, ok)])[-HISTORY:]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "failures": self.failures, "last_check": self.last_check,
                "last_error": self.last_error, "history": self.history}


# Bu sunucularda dinleme soketi ebeveyn süreçte olabilir (ör. uvicorn --reload); işçi ölse de TCP bağlantısı
# kabul edilir. Bu yüzden otomatik modda HTTP isteği ile gerçek yanıt aranır.
_WEB = {LauncherType.UVICORN, LauncherType.FLASK, LauncherType.DJANGO, LauncherType.STREAMLIT}


def effective_type(profile: RunProfile) -> HealthType:
    t = profile.health.type
    if t == HealthType.AUTO:
        if not profile.port:
            return HealthType.NONE
        return HealthType.HTTP if profile.launcher in _WEB else HealthType.TCP
    if t in (HealthType.TCP, HealthType.HTTP) and not profile.port:
        return HealthType.NONE
    return t


def check_tcp(port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True, ""
    except OSError as e:
        return False, f":{port} bağlantı kabul etmiyor ({e.__class__.__name__})"


def check_http(port: int, cfg: HealthConfig) -> tuple[bool, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=cfg.timeout)
    try:
        conn.request("GET", cfg.path, headers={"User-Agent": "Kovan-Health"})
        resp = conn.getresponse()
        resp.read(1024)
        if resp.status >= 500:
            return False, f"HTTP {resp.status}"
        return True, ""
    except (OSError, http.client.HTTPException) as e:
        return False, f"HTTP isteği başarısız ({e.__class__.__name__})"
    finally:
        conn.close()


def run_check(profile: RunProfile) -> tuple[bool, str] | None:
    t = effective_type(profile)
    if t == HealthType.NONE or profile.port is None:
        return None
    if t == HealthType.HTTP:
        return check_http(profile.port, profile.health)
    return check_tcp(profile.port, profile.health.timeout)

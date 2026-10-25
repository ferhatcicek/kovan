"""Süreç çıkışının nedenini sınıflandırır (saf fonksiyon, platformdan bağımsız test edilebilir).

Ayrımlar: kullanıcı durdurdu / tamamlandı / dışarıdan kapatıldı / öldürüldü / çöktü.
"""
from __future__ import annotations

import signal as _signal
from dataclasses import dataclass

from .models import RunState

# Windows NTSTATUS kodları (işaretsiz)
STATUS_CONTROL_C_EXIT = 0xC000013A   # Ctrl+C / Ctrl+Break / konsol kapatma
STATUS_ACCESS_VIOLATION = 0xC0000005
STATUS_STACK_OVERFLOW = 0xC00000FD

_GRACEFUL_SIGNALS = {"SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"}


@dataclass(frozen=True)
class ExitInfo:
    reason: str          # user_stop | exited_ok | external_stop | killed | crashed | panel_shutdown
    state: RunState
    signal: str | None
    level: str           # none | info | warning | error
    text: str            # kullanıcıya gösterilecek kısa açıklama

    @property
    def is_failure(self) -> bool:
        return self.reason in ("killed", "crashed")


def _signal_name(num: int) -> str:
    try:
        return _signal.Signals(num).name
    except ValueError:
        return f"SIG{num}"


def classify_exit(rc: int | None, *, stopping: bool, had_traceback: bool, expect_exit: bool,
                  is_windows: bool, shutdown: bool = False) -> ExitInfo:
    if stopping:
        if shutdown:
            return ExitInfo("panel_shutdown", RunState.STOPPED, None, "none", "Panel kapanırken durduruldu")
        return ExitInfo("user_stop", RunState.STOPPED, None, "none", "Kullanıcı tarafından durduruldu")

    if rc is None:
        return ExitInfo("killed", RunState.CRASHED, None, "error", "Süreç beklenmedik şekilde kayboldu")

    if not is_windows and rc < 0:
        name = _signal_name(-rc)
        if name in _GRACEFUL_SIGNALS:
            return ExitInfo("external_stop", RunState.STOPPED_EXTERNALLY, name, "warning",
                            f"Dışarıdan kapatıldı ({name})")
        if name == "SIGKILL":
            return ExitInfo("killed", RunState.CRASHED, name, "error", "Zorla sonlandırıldı (SIGKILL)")
        return ExitInfo("crashed", RunState.CRASHED, name, "error", f"Sinyal ile çöktü ({name})")

    if not is_windows and rc in (128 + 15, 128 + 2, 128 + 1):  # kabuk sarmalayıcıları: 143/130/129
        name = _signal_name(rc - 128)
        return ExitInfo("external_stop", RunState.STOPPED_EXTERNALLY, name, "warning",
                        f"Dışarıdan kapatıldı ({name})")

    if is_windows:
        urc = rc & 0xFFFFFFFF
        if urc == STATUS_CONTROL_C_EXIT:
            return ExitInfo("external_stop", RunState.STOPPED_EXTERNALLY, "CTRL_C", "warning",
                            "Dışarıdan kapatıldı (Ctrl+C / konsol kapandı)")
        if urc == STATUS_ACCESS_VIOLATION:
            return ExitInfo("crashed", RunState.CRASHED, None, "error", "Bellek erişim hatasıyla çöktü (0xC0000005)")
        if urc == STATUS_STACK_OVERFLOW:
            return ExitInfo("crashed", RunState.CRASHED, None, "error", "Yığın taşmasıyla çöktü (0xC00000FD)")
        if rc == 1 and not had_traceback:
            # Görev Yöneticisi / taskkill /F → TerminateProcess(…, 1); Python hatası olsaydı traceback görülürdü
            return ExitInfo("killed", RunState.CRASHED, None, "error",
                            "Beklenmedik sonlanma (kod 1) — dışarıdan sonlandırılmış olabilir")

    if rc == 0:
        if expect_exit:
            return ExitInfo("exited_ok", RunState.STOPPED, None, "info", "Başarıyla tamamlandı")
        return ExitInfo("external_stop", RunState.STOPPED_EXTERNALLY, None, "warning",
                        "Kendiliğinden veya dışarıdan kapandı (kod 0)")

    if had_traceback:
        return ExitInfo("crashed", RunState.CRASHED, None, "error", f"Hata ile çöktü (kod {rc})")
    return ExitInfo("crashed", RunState.CRASHED, None, "error", f"Beklenmedik şekilde sonlandı (kod {rc})")

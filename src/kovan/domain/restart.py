"""Otomatik yeniden başlatma kararı ve çökme döngüsü takibi (saf mantık)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import RestartPolicy

MAX_BACKOFF = 60.0


def effective_policy(profile_policy: RestartPolicy | None, category_policy: RestartPolicy | None,
                     global_policy: str | None) -> RestartPolicy:
    """Öncelik: profil > kategori > global > never."""
    for p in (profile_policy, category_policy):
        if p is not None:
            return RestartPolicy(p)
    try:
        return RestartPolicy(global_policy) if global_policy else RestartPolicy.NEVER
    except ValueError:
        return RestartPolicy.NEVER


def should_restart(policy: RestartPolicy, reason: str, unhealthy: bool = False, on_unhealthy: bool = False) -> bool:
    if reason in ("user_stop", "panel_shutdown"):
        return False
    if policy == RestartPolicy.ALWAYS:
        return True
    if policy == RestartPolicy.ON_FAILURE:
        return reason in ("crashed", "killed") or (unhealthy and on_unhealthy)
    return False


@dataclass
class RestartTracker:
    """Belirli bir zaman penceresindeki deneme sayısını tutar; üstel geri çekilme süresini hesaplar."""

    attempts: list[float] = field(default_factory=list)

    def next_delay(self, now: float, max_retries: int, window_sec: float) -> float | None:
        """Yeni bir deneme kaydeder; izin yoksa None (çökme döngüsü)."""
        self.attempts = [t for t in self.attempts if now - t < window_sec]
        if len(self.attempts) >= max_retries:
            return None
        self.attempts.append(now)
        return min(MAX_BACKOFF, 2.0 ** (len(self.attempts) - 1))

    def reset(self) -> None:
        self.attempts.clear()

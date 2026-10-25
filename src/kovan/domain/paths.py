"""İzinli kök klasör denetimi (path traversal koruması)."""
from __future__ import annotations

from pathlib import Path


class PathNotAllowed(ValueError):
    pass


def resolve_within(path: str | Path, roots: list[Path]) -> Path:
    """Yolu çözümler (symlink dahil) ve izinli köklerden birinin içinde değilse hata verir."""
    p = Path(path).expanduser().resolve()
    for root in roots:
        r = Path(root).expanduser().resolve()
        if p == r or p.is_relative_to(r):
            return p
    raise PathNotAllowed(f"Yol izinli kök klasörlerin dışında: {p}")


def resolve_child(base: Path, relative: str) -> Path:
    """`base` altında göreli bir yolu çözümler; dışarı çıkılırsa hata verir."""
    if Path(relative).is_absolute():
        raise PathNotAllowed("Mutlak yol kabul edilmez")
    base = base.resolve()
    p = (base / relative).resolve()
    if p != base and not p.is_relative_to(base):
        raise PathNotAllowed(f"Yol proje klasörünün dışında: {relative}")
    return p

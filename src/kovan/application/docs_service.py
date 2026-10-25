"""Proje dokümanlarını güvenli şekilde listeleme ve okuma."""
from __future__ import annotations

import fnmatch
import html
import mimetypes
from pathlib import Path

from ..domain.paths import PathNotAllowed, resolve_child
from ..infrastructure.markdown import render_markdown

DOC_PATTERNS = ("readme*", "changelog*", "contributing*", "license*", "*.md", "*.rst", "*.txt",
                ".env.example", ".env.sample", ".env.template")
NEVER = (".env", ".env.local", ".env.prod", ".env.production", "*.key", "*.pem", "secrets*")
SCAN_DIRS = ("", "docs", "doc")
MAX_DOC = 2 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # SVG bilinçli olarak dışarıda


def _allowed(name: str) -> bool:
    low = name.lower()
    if any(fnmatch.fnmatch(low, p) for p in NEVER):
        return False
    return any(fnmatch.fnmatch(low, p) for p in DOC_PATTERNS)


def list_docs(project_dir: Path) -> list[dict]:
    out = []
    for sub in SCAN_DIRS:
        d = project_dir / sub if sub else project_dir
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir(), key=lambda x: (not x.name.lower().startswith("readme"), x.name.lower())):
            if p.is_file() and _allowed(p.name) and p.name.lower() not in ("requirements.txt",):
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                out.append({"path": p.relative_to(project_dir).as_posix(), "size": size})
    return out


def read_doc(project_dir: Path, rel: str) -> dict:
    p = resolve_child(project_dir, rel)
    if not p.is_file() or not _allowed(p.name):
        raise PathNotAllowed("Bu dosya doküman olarak görüntülenemez")
    if p.stat().st_size > MAX_DOC:
        raise PathNotAllowed("Dosya çok büyük (2 MB sınırı)")
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".md" or p.name.lower() in ("readme", "readme.md"):
        body = render_markdown(text)
    else:
        body = f"<pre class=\"plain\">{html.escape(text)}</pre>"
    env_keys = []
    if p.name.lower().startswith(".env."):
        env_keys = [line.split("=", 1)[0].strip() for line in text.splitlines()
                    if "=" in line and not line.lstrip().startswith("#")]
    return {"path": rel, "html": body, "env_keys": env_keys}


def read_image(project_dir: Path, rel: str) -> tuple[bytes, str]:
    p = resolve_child(project_dir, rel)
    mime = mimetypes.guess_type(p.name)[0] or ""
    if not p.is_file() or mime not in IMAGE_TYPES or p.stat().st_size > 5 * 1024 * 1024:
        raise PathNotAllowed("Görsel bulunamadı veya izin verilmeyen tür")
    return p.read_bytes(), mime

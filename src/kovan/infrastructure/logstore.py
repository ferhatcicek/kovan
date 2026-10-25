"""Log hattı: redaksiyon, traceback gruplama, bellek halka tamponu ve çalıştırma başına dosya."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_LINE = 16 * 1024

_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)([^\s'\",;]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]{8,}=*)"),
    re.compile(r"()(AKIA[0-9A-Z]{16})"),
]


class Redactor:
    def __init__(self, secrets: list[str] | None = None):
        self._secrets = sorted({s for s in (secrets or []) if len(s) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        for pat in _SECRET_PATTERNS:
            text = pat.sub(lambda m: m.group(1) + "***", text)
        return text


@dataclass
class LogEntry:
    seq: int
    ts: float
    stream: str  # "out" | "err" | "sys"
    text: str


@dataclass
class ErrorEvent:
    signature: str
    exc_type: str
    message: str
    text: str
    frames: list[tuple[str, int]] = field(default_factory=list)
    count: int = 1
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_FRAME = re.compile(r'^\s*File "(.+?)", line (\d+)')
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault)?)(?::\s*(.*))?$")


class TracebackGrouper:
    """`Traceback (most recent call last):` ile başlayan blokları tek hata olayına çevirir."""

    def __init__(self) -> None:
        self._buf: list[str] | None = None

    def feed(self, line: str) -> ErrorEvent | None:
        line = _ANSI.sub("", line)  # Python 3.13+ FORCE_COLOR ile renkli traceback basar
        if line.startswith("Traceback (most recent call last):"):
            self._buf = [line]
            return None
        if self._buf is None:
            return None
        if line.startswith((" ", "\t")) or not line.strip() or line.startswith(
                ("During handling", "The above exception")) or line.startswith("Traceback ("):
            self._buf.append(line)
            if len(self._buf) > 400:
                self._buf = None
            return None
        m = _EXC_LINE.match(line)
        buf, self._buf = self._buf, None
        if not m:
            return None
        buf.append(line)
        frames = [(fm.group(1), int(fm.group(2))) for fm in map(_FRAME.match, buf) if fm]
        last = frames[-1] if frames else ("", 0)
        exc_type = m.group(1)
        return ErrorEvent(signature=f"{exc_type}@{last[0]}:{last[1]}", exc_type=exc_type,
                          message=(m.group(2) or "")[:500], text="\n".join(buf), frames=frames)


class ProjectLog:
    """Bir projenin canlı log durumu (tüm çalıştırmalar boyunca tek tampon)."""

    def __init__(self, maxlen: int):
        self._lock = threading.Lock()
        self.lines: deque[LogEntry] = deque(maxlen=maxlen)
        self.errors: dict[str, ErrorEvent] = {}
        self._seq = 0
        self._file = None
        self._groupers = {"out": TracebackGrouper(), "err": TracebackGrouper()}
        self.redact = Redactor()

    def open_file(self, path: Path) -> None:
        self.close_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8", errors="replace")

    def close_file(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def append(self, stream: str, text: str) -> tuple[LogEntry, ErrorEvent | None]:
        text = self.redact(text[:MAX_LINE])
        with self._lock:
            self._seq += 1
            entry = LogEntry(self._seq, time.time(), stream, text)
            self.lines.append(entry)
            if self._file:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.ts))
                self._file.write(f"{stamp} [{stream}] {text}\n")
                self._file.flush()
            err = None
            if stream in self._groupers:
                ev = self._groupers[stream].feed(text)
                if ev:
                    if ev.signature in self.errors:
                        prev = self.errors[ev.signature]
                        prev.count += 1
                        prev.last_seen = ev.last_seen
                        prev.text, prev.message = ev.text, ev.message
                        err = prev
                    else:
                        if len(self.errors) >= 50:
                            oldest = min(self.errors.values(), key=lambda e: e.last_seen)
                            del self.errors[oldest.signature]
                        self.errors[ev.signature] = ev
                        err = ev
            return entry, err

    def snapshot(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in self.lines if e.seq > after]

    def clear_errors(self) -> None:
        with self._lock:
            self.errors.clear()


def enforce_retention(logs_dir: Path, days: int, quota_mb: int) -> int:
    """Eski log dosyalarını siler; toplam boyut kotayı aşarsa en eskilerden başlar."""
    if not logs_dir.exists():
        return 0
    files = sorted((p for p in logs_dir.rglob("*.log") if p.is_file()), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - days * 86400
    removed, total = 0, sum(p.stat().st_size for p in files)
    for p in files:
        size = p.stat().st_size
        if p.stat().st_mtime < cutoff or total > quota_mb * 1024 * 1024:
            try:
                p.unlink()
                removed += 1
                total -= size
            except OSError:
                pass
    return removed

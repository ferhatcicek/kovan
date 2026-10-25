from kovan.application.discovery import detect, scan_roots
from kovan.application.docs_service import list_docs, read_doc
from kovan.domain.paths import PathNotAllowed
from kovan.infrastructure.logstore import ProjectLog, Redactor, TracebackGrouper, enforce_retention
from kovan.infrastructure.markdown import first_paragraph, render_markdown

import pytest

TB = """Traceback (most recent call last):
  File "/p/app.py", line 10, in <module>
    main()
  File "/p/app.py", line 5, in main
    os.environ["X"]
KeyError: 'X'""".splitlines()


def test_traceback_grouping():
    g = TracebackGrouper()
    events = [g.feed(line) for line in ["başka satır", *TB, "sonraki"]]
    ev = [e for e in events if e][0]
    assert ev.exc_type == "KeyError" and ev.message == "'X'"
    assert ev.frames[-1] == ("/p/app.py", 5)


def test_colored_traceback():
    g = TracebackGrouper()
    lines = ["\x1b[35mTraceback (most recent call last):\x1b[0m",
             '  File \x1b[35m"/p/a.py"\x1b[0m, line \x1b[35m3\x1b[0m, in \x1b[35m<module>\x1b[0m',
             "\x1b[1;35mKeyError\x1b[0m: \x1b[35m'X'\x1b[0m"]
    ev = [e for e in map(g.feed, lines) if e][0]
    assert ev.exc_type == "KeyError" and ev.message == "'X'" and ev.frames == [("/p/a.py", 3)]


def test_chained_traceback_is_single_event():
    g = TracebackGrouper()
    lines = TB[:-1] + ["KeyError: 'X'", "", "During handling of the above exception, another exception occurred:",
                       "", *TB[:-1], "ValueError: bad"]
    events = [e for e in map(g.feed, lines) if e]
    # ilk istisna satırı bloğu kapatır, ikinci zincir ayrı olay olarak gruplanır
    assert [e.exc_type for e in events][-1] == "ValueError"


def test_project_log_dedupes_errors(tmp_path):
    log = ProjectLog(100)
    for _ in range(3):
        for line in TB:
            log.append("err", line)
    assert len(log.errors) == 1 and next(iter(log.errors.values())).count == 3


def test_redaction():
    r = Redactor(["supersecretvalue"])
    out = r("conn supersecretvalue password=hunter22 Authorization: Bearer abcdefghijkl")
    assert "supersecretvalue" not in out and "hunter22" not in out and "abcdefghijkl" not in out


def test_log_file_and_ring(tmp_path):
    log = ProjectLog(5)
    log.open_file(tmp_path / "r.log")
    for i in range(10):
        log.append("out", f"satır {i}")
    log.close_file()
    assert [e["text"] for e in log.snapshot()] == [f"satır {i}" for i in range(5, 10)]
    assert (tmp_path / "r.log").read_text().count("satır") == 10


def test_retention(tmp_path):
    import os
    import time
    old = tmp_path / "1" / "old.log"
    old.parent.mkdir()
    old.write_text("x")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    (tmp_path / "1" / "new.log").write_text("y")
    assert enforce_retention(tmp_path, 14, 100) == 1
    assert not old.exists()


def test_markdown_is_sanitized():
    html = render_markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n<img src=x onerror=alert(1)>\n\n**ok**")
    from html.parser import HTMLParser

    class Collect(HTMLParser):
        tags, attrs = [], []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            self.attrs.extend(attrs)

    c = Collect()
    c.feed(html)
    assert "script" not in c.tags and "img" not in c.tags
    assert not any(k.startswith("on") or "javascript:" in (v or "") for k, v in c.attrs)
    assert "<strong>ok</strong>" in html
    raw = render_markdown('[x](javascript:alert(1)) <a href="javascript:alert(1)">y</a> [ok](https://e.com)')
    c2 = Collect()
    c2.attrs = []
    c2.feed(raw)
    hrefs = [v for k, v in c2.attrs if k == "href"]
    assert hrefs == ["https://e.com"]


def test_first_paragraph():
    md = "# Başlık\n\n![badge](x)\n\n```\nkod\n```\n\nAsıl açıklama burada.\n\nDiğer"
    assert first_paragraph(md) == "Asıl açıklama burada."


def test_discovery(code_root):
    found = {c.name: c for c in scan_roots([code_root])}
    assert found["fastapi_app"].launcher == "uvicorn" and found["fastapi_app"].target == "main:app"
    assert found["fastapi_app"].category == "Web API"
    assert "Sipariş servisi" in found["fastapi_app"].description
    assert found["flask_site"].launcher == "flask" and found["flask_site"].target == "app"
    assert found["script_bot"].launcher == "script" and found["script_bot"].target == "bot.py"
    assert found["django_like"].launcher == "django"
    assert "notes_only" not in found


def test_discovery_never_executes_code(code_root):
    marker = code_root / "pwned"
    evil = code_root / "evil"
    evil.mkdir()
    (evil / "main.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nif __name__ == '__main__': pass\n")
    detect(evil)
    assert not marker.exists()


def test_docs_listing_hides_real_env(code_root):
    paths = [d["path"] for d in list_docs(code_root / "fastapi_app")]
    assert "README.md" in paths and ".env.example" in paths and ".env" not in paths
    with pytest.raises(PathNotAllowed):
        read_doc(code_root / "fastapi_app", ".env")
    with pytest.raises(PathNotAllowed):
        read_doc(code_root / "fastapi_app", "../flask_site/README.md")
    with pytest.raises(PathNotAllowed):
        read_doc(code_root / "fastapi_app", "main.py")
    d = read_doc(code_root / "fastapi_app", ".env.example")
    assert d["env_keys"] == ["DB_URL", "SECRET_KEY"]

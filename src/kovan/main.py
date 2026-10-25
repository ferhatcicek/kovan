"""Giriş noktası: paneli 127.0.0.1 üzerinde başlatır ve tarayıcıyı otomatik oturumla açar.

`kovan [--data-dir D] [serve] [--port P] [--root R]` paneli başlatır; `kovan export|import|backup|restore|doctor`
yapılandırma/yedek komutlarıdır (bkz. cli.py).
"""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import cli
from .config import AppConfig, default_roots
from .web.app import create_app


def serve(cfg: AppConfig, roots: list[str]) -> None:
    cfg.ensure_dirs()
    cli.acquire_lock(cfg.data_dir)
    try:
        app = create_app(cfg)
        ctx = getattr(app.state, "ctx", None)
        if ctx is not None:
            if roots:
                current = list(ctx.repo.get_setting("roots", []))
                for r in roots:
                    p = str(Path(r).expanduser().resolve())
                    if Path(p).is_dir() and p not in current:
                        current.append(p)
                ctx.repo.set_setting("roots", current)
            elif ctx.repo.get_setting("roots") is None:
                ctx.repo.set_setting("roots", default_roots())

        if cfg.open_browser:
            def _open() -> None:
                time.sleep(1.2)
                url = f"http://127.0.0.1:{cfg.port}/"
                if ctx is not None and ctx.auth.has_password:
                    url += f"auth/once?token={ctx.auth.issue_once_token()}"
                webbrowser.open(url)
            threading.Thread(target=_open, daemon=True).start()

        print(f"Kovan çalışıyor: http://127.0.0.1:{cfg.port}/  (veri: {cfg.data_dir}; durdurmak için Ctrl+C)")
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", access_log=False)
    finally:
        cli.release_lock(cfg.data_dir)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Geriye uyumluluk: alt komut verilmezse 'serve' varsayılır (ör. kovan --root D:\code)
    if not any(a in cli.COMMANDS or a == "serve" for a in argv) and not {"-h", "--help"} & set(argv):
        argv = ["serve", *argv]
    args = cli.build_parser().parse_args(argv)
    if args.command == "serve":
        cfg = AppConfig(port=args.port, open_browser=not args.no_browser)
        if args.data_dir:
            cfg.data_dir = args.data_dir
        serve(cfg, args.root)
        return
    if args.command is None:
        cli.build_parser().print_help()
        return
    raise SystemExit(cli.run(argv))


if __name__ == "__main__":
    main()

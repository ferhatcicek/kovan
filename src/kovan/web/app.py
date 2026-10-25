"""FastAPI uygulaması: sayfalar, REST API ve WebSocket."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path

from fastapi import Body, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..application import discovery, portability
from ..application.backup_service import BackupError, BackupService
from ..application.docs_service import list_docs, read_doc, read_image
from ..application.event_bus import EventBus
from ..application.run_service import RunError, RunService
from ..config import AppConfig
from ..domain.models import RestartPolicy
from ..domain.launchers import launcher_catalog
from ..domain.models import Category, LauncherType, Project, RunProfile
from ..domain.paths import PathNotAllowed, resolve_within
from ..infrastructure import migrations
from ..infrastructure.db import Repository
from ..infrastructure.logstore import enforce_retention
from .security import SESSION_COOKIE, AuthManager, SecurityMiddleware, allowed_hosts, origin_ok, set_session_cookie

WEB_DIR = Path(__file__).parent


class Context:
    def __init__(self, config: AppConfig):
        config.ensure_dirs()
        self.config = config
        self.repo = Repository(config.db_path, config.backups_dir)
        self.backups = BackupService(self.repo, config.data_dir)
        self.repo.on_change = self.backups.schedule_mirror
        self.bus = EventBus()
        self.runs = RunService(config, self.repo, self.bus)
        self.auth = AuthManager(self.repo)


def _error_app(message: str) -> FastAPI:
    """Veri açılamadığında (ör. daha yeni sürümün veritabanı) veriye dokunmadan hata gösteren uygulama."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def blocked(path: str):
        body = f"""<!doctype html><meta charset="utf-8"><title>Kovan</title>
        <body style="font-family:system-ui;max-width:640px;margin:10vh auto;padding:0 16px">
        <h1>Kovan açılamadı</h1><p>{message}</p>
        <p>Verileriniz değiştirilmedi.</p></body>"""
        return HTMLResponse(body, status_code=503)

    return app


# ---------- API şemaları ----------
class BulkRequest(BaseModel):
    ids: list[int]


class BulkUpdate(BaseModel):
    ids: list[int]
    category_id: int | None = None
    set_category: bool = False
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    favorite: bool | None = None
    metrics_enabled: bool | None = None


class SettingsUpdate(BaseModel):
    roots: list[str] | None = None
    port_range: tuple[int, int] | None = None
    metrics_enabled: bool | None = None
    restart_policy: RestartPolicy | None = None
    notify_default: bool | None = None


class ExternalStop(BaseModel):
    pid: int
    create_time: float
    confirm: bool = False


class ExportRequest(BaseModel):
    include_secrets: bool = False
    passphrase: str = ""


class ImportPayload(BaseModel):
    content: str = Field(max_length=portability.MAX_BYTES)
    root_map: dict[str, str] = Field(default_factory=dict)
    passphrase: str = ""
    mode: str = "merge"
    password: str = ""
    keep_watch: bool = False  # dosyadaki otomatik yeniden başlatma ayarlarını da al (açık onay)


class RestoreRequest(BaseModel):
    name: str
    password: str = ""


class ImportRequest(BaseModel):
    candidates: list[dict]


def create_app(config: AppConfig) -> FastAPI:
    try:
        ctx = Context(config)
    except migrations.SchemaTooNew as e:
        return _error_app(str(e))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.bus.bind_loop(asyncio.get_running_loop())
        recovered = ctx.runs.recover()
        ctx.runs.start_polling()
        await asyncio.to_thread(enforce_retention, config.logs_dir, config.log_retention_days, config.log_quota_mb)
        if config.legacy_migration:
            ctx.repo.audit("legacy_data_migrated", config.legacy_migration)
        if ctx.repo.migration.get("backup"):
            ctx.repo.audit("schema_migrated", json.dumps(ctx.repo.migration))
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ctx.backups.daily_if_due)
        await asyncio.to_thread(ctx.backups.write_mirror)
        ctx.repo.audit("panel_start", f"{__version__} adopted={recovered['adopted']}")
        yield
        await ctx.runs.stop_all()
        ctx.backups.cancel()
        ctx.backups.write_mirror()
        ctx.repo.audit("panel_stop", "")

    app = FastAPI(title="Kovan", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware, auth=ctx.auth, port=config.port)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    def err(status: int, message: str, **extra) -> JSONResponse:
        return JSONResponse({"error": message, **extra}, status_code=status)

    def get_project(pid: int) -> Project:
        p = ctx.repo.get_project(pid)
        if not p:
            raise HTTPException(404, "Proje bulunamadı")
        return p

    def project_dir(p: Project) -> Path:
        try:
            return resolve_within(p.path, ctx.runs.roots())
        except PathNotAllowed as e:
            raise HTTPException(403, str(e)) from e

    def run_error(e: RunError) -> JSONResponse:
        status = {"not_found": 404, "already_running": 409, "port_conflict": 409, "orphaned": 409,
                  "path_not_allowed": 403, "forbidden": 403}.get(e.code, 400)
        return err(status, str(e), code=e.code, detail=e.detail)

    # ---------- sayfalar ----------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not ctx.auth.has_password:
            return templates.TemplateResponse(request, "setup.html", {"error": None})
        session = request.state.session
        if session is None:
            return templates.TemplateResponse(request, "login.html", {"error": None})
        return templates.TemplateResponse(request, "index.html", {"csrf": session.csrf, "version": __version__})

    @app.post("/auth/setup")
    async def setup(request: Request, password: str = Form(...), password2: str = Form(...)):
        if ctx.auth.has_password:
            return RedirectResponse("/", status_code=303)
        error = None
        if password != password2:
            error = "Parolalar eşleşmiyor"
        else:
            try:
                ctx.auth.set_password(password)
            except ValueError as e:
                error = str(e)
        if error:
            return templates.TemplateResponse(request, "setup.html", {"error": error}, status_code=400)
        ctx.repo.audit("password_set", "")
        sid, _ = ctx.auth.create_session()
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp, sid)
        return resp

    @app.post("/auth/login")
    async def login(request: Request, password: str = Form(...)):
        if ctx.auth.verify(password):
            ctx.repo.audit("login", "ok")
            sid, _ = ctx.auth.create_session()
            resp = RedirectResponse("/", status_code=303)
            set_session_cookie(resp, sid)
            return resp
        ctx.repo.audit("login", "failed")
        locked = ctx.auth.locked_seconds
        msg = f"Çok fazla deneme. {locked} sn bekleyin." if locked else "Parola hatalı"
        return templates.TemplateResponse(request, "login.html", {"error": msg}, status_code=401)

    @app.get("/auth/once")
    async def once(token: str = ""):
        resp = RedirectResponse("/", status_code=303)
        if ctx.auth.has_password and ctx.auth.consume_once_token(token):
            sid, _ = ctx.auth.create_session()
            set_session_cookie(resp, sid)
        return resp

    @app.post("/auth/logout")
    async def logout(request: Request):
        ctx.auth.destroy(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ---------- durum ----------
    @app.get("/api/state")
    async def state():
        return {
            "version": __version__,
            "projects": [p.model_dump() for p in ctx.repo.list_projects()],
            "categories": [c.model_dump() for c in ctx.repo.list_categories()],
            "runtimes": {str(pid): rt.to_dict() for pid, rt in ctx.runs.runtimes.items()},
            "settings": {
                "roots": ctx.repo.get_setting("roots", []),
                "port_range": ctx.repo.get_setting("port_range", [8000, 8999]),
                "metrics_enabled": ctx.repo.get_setting("metrics_enabled", False),
                "restart_policy": ctx.repo.get_setting("restart_policy", "never"),
                "notify_default": ctx.repo.get_setting("notify_default", True),
            },
            "unread_notifications": sum(1 for n in ctx.repo.list_notifications(200) if not n["read"]),
            "quarantine": len(ctx.repo.list_quarantine()),
            "launchers": launcher_catalog(),
        }

    # ---------- ayarlar ----------
    @app.put("/api/settings")
    async def update_settings(body: SettingsUpdate):
        if body.roots is not None:
            roots = []
            for r in body.roots:
                p = Path(r).expanduser()
                if not r.strip():
                    continue
                if not p.is_absolute() or not p.is_dir():
                    return err(400, f"Kök klasör bulunamadı veya mutlak değil: {r}")
                roots.append(str(p.resolve()))
            ctx.repo.set_setting("roots", roots)
            ctx.repo.audit("settings_roots", ";".join(roots))
        if body.port_range is not None:
            a, b = body.port_range
            if not (1 <= a <= b <= 65535):
                return err(400, "Geçersiz port aralığı")
            ctx.repo.set_setting("port_range", [a, b])
        if body.metrics_enabled is not None:
            ctx.repo.set_setting("metrics_enabled", body.metrics_enabled)
        if body.restart_policy is not None:
            ctx.repo.set_setting("restart_policy", body.restart_policy.value)
        if body.notify_default is not None:
            ctx.repo.set_setting("notify_default", body.notify_default)
        return {"ok": True}

    # ---------- projeler ----------
    def _validate_project(p: Project) -> Project:
        d = resolve_within(p.path, ctx.runs.roots())
        if not d.is_dir():
            raise HTTPException(400, "Proje klasörü bulunamadı")
        if p.category_id is not None and not ctx.repo.get_category(p.category_id):
            raise HTTPException(400, "Kategori bulunamadı")
        names = [pr.name for pr in p.profiles]
        if not names or len(set(names)) != len(names):
            raise HTTPException(400, "Profil adları benzersiz olmalı ve en az bir profil bulunmalı")
        if p.active_profile not in names:
            p = p.model_copy(update={"active_profile": names[0]})
        return p.model_copy(update={"path": str(d)})

    @app.post("/api/projects")
    async def create_project(body: dict = Body(...)):
        try:
            p = _validate_project(Project(**{**body, "id": None}))
        except (ValidationError, PathNotAllowed) as e:
            return err(400, str(e))
        if ctx.repo.find_project_by_path(p.path):
            return err(409, "Bu klasör zaten ekli")
        p = ctx.repo.save_project(p)
        ctx.repo.audit("project_create", f"{p.name} {p.path}")
        return p.model_dump()

    @app.put("/api/projects/{pid}")
    async def update_project(pid: int, body: dict = Body(...)):
        old = get_project(pid)
        try:
            p = _validate_project(Project(**{**body, "id": pid}))
        except (ValidationError, PathNotAllowed) as e:
            return err(400, str(e))
        other = ctx.repo.find_project_by_path(p.path)
        if other and other.id != pid:
            return err(409, "Bu klasör başka bir projede kayıtlı")
        ctx.repo.save_project(p)
        if old.profiles != p.profiles or old.path != p.path:
            ctx.repo.audit("project_config", f"{p.name}")
        return p.model_dump()

    @app.delete("/api/projects/{pid}")
    async def delete_project(pid: int):
        p = get_project(pid)
        await ctx.runs.stop(pid, "delete")
        ctx.repo.delete_project(pid)
        ctx.runs.runtimes.pop(pid, None)
        ctx.repo.audit("project_delete", p.name)
        return {"ok": True}

    @app.post("/api/projects/bulk-update")
    async def bulk_update(body: BulkUpdate):
        if body.set_category and body.category_id is not None and not ctx.repo.get_category(body.category_id):
            return err(400, "Kategori bulunamadı")
        for pid in body.ids:
            p = ctx.repo.get_project(pid)
            if not p:
                continue
            upd: dict = {}
            if body.set_category:
                upd["category_id"] = body.category_id
            if body.add_tags or body.remove_tags:
                tags = [t for t in p.tags if t not in body.remove_tags] + body.add_tags
                upd["tags"] = tags
            if body.favorite is not None:
                upd["favorite"] = body.favorite
            if body.metrics_enabled is not None:
                upd["metrics_enabled"] = body.metrics_enabled
            try:
                ctx.repo.save_project(Project(**{**p.model_dump(), **upd}))
            except ValidationError as e:
                return err(400, str(e))
        return {"ok": True}

    @app.post("/api/projects/{pid}/preview")
    async def preview(pid: int, body: dict = Body(...)):
        p = get_project(pid)
        try:
            profile = RunProfile(**body)
        except ValidationError as e:
            return {"ok": False, "error": "; ".join(x["msg"] for x in e.errors())}
        return ctx.runs.preview(p, profile)

    @app.post("/api/projects/{pid}/{action}")
    async def project_action(pid: int, action: str, body: dict | None = Body(None)):
        get_project(pid)
        try:
            if action == "start":
                rt = await ctx.runs.start(pid, "user", (body or {}).get("profile"))
            elif action == "stop":
                rt = await ctx.runs.stop(pid)
            elif action == "restart":
                rt = await ctx.runs.restart(pid)
            else:
                return err(404, "Bilinmeyen işlem")
        except RunError as e:
            return run_error(e)
        return rt.to_dict()

    @app.post("/api/bulk/{action}")
    async def bulk_action(action: str, body: BulkRequest):
        if action not in ("start", "stop", "restart"):
            return err(404, "Bilinmeyen işlem")
        results = {}
        for pid in body.ids:
            try:
                fn = {"start": lambda i: ctx.runs.start(i, "bulk"), "stop": lambda i: ctx.runs.stop(i, "bulk"),
                      "restart": ctx.runs.restart}[action]
                await fn(pid)
                results[str(pid)] = {"ok": True}
            except RunError as e:
                results[str(pid)] = {"ok": False, "error": str(e), "code": e.code}
        return {"results": results}

    # ---------- loglar / hatalar / geçmiş ----------
    @app.get("/api/projects/{pid}/logs")
    async def logs(pid: int, after: int = 0):
        get_project(pid)
        return {"lines": ctx.runs.log(pid).snapshot(after)}

    @app.get("/api/projects/{pid}/errors")
    async def errors(pid: int):
        get_project(pid)
        errs = sorted(ctx.runs.log(pid).errors.values(), key=lambda e: e.last_seen, reverse=True)
        return {"errors": [e.to_dict() for e in errs]}

    @app.delete("/api/projects/{pid}/errors")
    async def clear_errors(pid: int):
        ctx.runs.log(pid).clear_errors()
        return {"ok": True}

    @app.get("/api/projects/{pid}/runs")
    async def runs(pid: int):
        get_project(pid)
        return {"runs": ctx.repo.list_runs(pid)}

    @app.get("/api/projects/{pid}/runs/{run_id}/log")
    async def run_log(pid: int, run_id: int):
        run = ctx.repo.get_run(run_id)
        if not run or run["project_id"] != pid or not run["log_file"]:
            raise HTTPException(404, "Kayıt bulunamadı")
        f = Path(run["log_file"]).resolve()
        if not f.is_relative_to(config.logs_dir.resolve()) or not f.is_file():
            raise HTTPException(404, "Log dosyası bulunamadı")
        return FileResponse(f, media_type="text/plain; charset=utf-8", filename=f"{pid}-{f.name}")

    # ---------- dokümanlar ----------
    @app.get("/api/projects/{pid}/docs")
    async def docs(pid: int):
        return {"docs": list_docs(project_dir(get_project(pid)))}

    @app.get("/api/projects/{pid}/docs/view")
    async def doc_view(pid: int, path: str):
        try:
            return read_doc(project_dir(get_project(pid)), path)
        except (PathNotAllowed, OSError) as e:
            return err(403, str(e))

    @app.get("/api/projects/{pid}/docs/image")
    async def doc_image(pid: int, path: str):
        try:
            data, mime = read_image(project_dir(get_project(pid)), path)
        except (PathNotAllowed, OSError) as e:
            return err(404, str(e))
        return Response(data, media_type=mime)

    # ---------- kategoriler ----------
    def _check_parent(cid: int | None, parent: int | None) -> None:
        if parent is None:
            return
        par = ctx.repo.get_category(parent)
        if not par:
            raise HTTPException(400, "Üst kategori bulunamadı")
        if par.parent_id is not None:
            raise HTTPException(400, "Yalnızca bir seviye alt kategori desteklenir")
        if cid is not None and parent == cid:
            raise HTTPException(400, "Kategori kendisinin üstü olamaz")
        if cid is not None and any(c.parent_id == cid for c in ctx.repo.list_categories()):
            raise HTTPException(400, "Alt kategorisi olan kategori başka bir kategorinin altına taşınamaz")

    @app.post("/api/categories")
    async def create_category(body: dict = Body(...)):
        try:
            c = Category(**{**body, "id": None})
        except ValidationError as e:
            return err(400, "; ".join(x["msg"] for x in e.errors()))
        _check_parent(None, c.parent_id)
        c = ctx.repo.save_category(c)
        ctx.repo.audit("category_create", c.name)
        return c.model_dump()

    @app.put("/api/categories/{cid}")
    async def update_category(cid: int, body: dict = Body(...)):
        if not ctx.repo.get_category(cid):
            raise HTTPException(404, "Kategori bulunamadı")
        try:
            c = Category(**{**body, "id": cid})
        except ValidationError as e:
            return err(400, "; ".join(x["msg"] for x in e.errors()))
        _check_parent(cid, c.parent_id)
        ctx.repo.save_category(c)
        return c.model_dump()

    @app.put("/api/categories-order")
    async def reorder_categories(body: list[int] = Body(...)):
        for i, cid in enumerate(body):
            c = ctx.repo.get_category(cid)
            if c:
                ctx.repo.save_category(c.model_copy(update={"sort": i}))
        return {"ok": True}

    @app.delete("/api/categories/{cid}")
    async def delete_category(cid: int):
        c = ctx.repo.get_category(cid)
        if not c:
            raise HTTPException(404, "Kategori bulunamadı")
        ctx.repo.delete_category(cid)
        ctx.repo.audit("category_delete", c.name)
        return {"ok": True}

    # ---------- keşif ----------
    @app.get("/api/discover")
    async def discover():
        roots = ctx.runs.roots()
        if not roots:
            return err(400, "Önce Ayarlar'dan en az bir kök klasör ekleyin")
        cands = await asyncio.to_thread(discovery.scan_roots, roots)
        existing = {p.path for p in ctx.repo.list_projects()}
        for c in cands:
            c.already_added = str(Path(c.path).resolve()) in existing
        return {"candidates": [c.to_dict() for c in cands]}

    @app.post("/api/discover/import")
    async def discover_import(body: ImportRequest):
        cats = {c.name: c.id for c in ctx.repo.list_categories()}
        added, errors_ = [], []
        for c in body.candidates:
            try:
                launcher = LauncherType(c.get("launcher", "script"))
                profile = RunProfile(launcher=launcher, target=c.get("target", ""), port=c.get("port"))
                p = _validate_project(Project(
                    name=c.get("name") or Path(c["path"]).name, path=c["path"],
                    category_id=c.get("category_id", cats.get(c.get("category", ""))),
                    description=c.get("description", "")[:300], profiles=[profile]))
            except (ValidationError, PathNotAllowed, HTTPException, KeyError, ValueError) as e:
                errors_.append({"path": c.get("path"), "error": str(getattr(e, "detail", e))})
                continue
            if ctx.repo.find_project_by_path(p.path):
                continue
            added.append(ctx.repo.save_project(p).model_dump())
        ctx.repo.audit("discover_import", f"{len(added)} proje")
        return {"added": added, "errors": errors_}

    # ---------- port ----------
    @app.get("/api/ports/check")
    async def port_check(port: int):
        if not 1 <= port <= 65535:
            return err(400, "Geçersiz port")
        conflict = await asyncio.to_thread(ctx.runs.check_port, port)
        return {"free": conflict is None, "conflict": conflict}

    @app.get("/api/audit")
    async def audit():
        return {"entries": ctx.repo.list_audit()}

    # ---------- gözetim: yetimler, harici süreçler, olaylar, bildirimler ----------
    @app.post("/api/projects/{pid}/orphans/kill")
    async def kill_orphans(pid: int):
        get_project(pid)
        return (await ctx.runs.stop(pid, "orphans")).to_dict()

    @app.post("/api/projects/{pid}/external/stop")
    async def stop_external(pid: int, body: ExternalStop):
        get_project(pid)
        if not body.confirm:
            return err(400, "Harici süreci durdurmak için açık onay gerekli")
        try:
            await ctx.runs.stop_external(pid, body.pid, body.create_time)
        except RunError as e:
            return run_error(e)
        return {"ok": True}

    @app.get("/api/projects/{pid}/events")
    async def events(pid: int):
        get_project(pid)
        return {"events": ctx.repo.list_events(pid)}

    @app.get("/api/notifications")
    async def notifications():
        return {"notifications": ctx.repo.list_notifications()}

    @app.post("/api/notifications/read")
    async def notifications_read():
        ctx.repo.mark_notifications_read()
        return {"ok": True}

    # ---------- yedekler ----------
    def reauth(password: str) -> JSONResponse | None:
        if not password or not ctx.auth.verify(password):
            ctx.repo.audit("reauth_failed", "")
            return err(403, "Bu işlem için panel parolanızı doğru girmelisiniz", code="reauth")
        return None

    @app.get("/api/backups")
    async def backups():
        return {"backups": ctx.backups.list(), "dir": str(ctx.backups.dir)}

    @app.post("/api/backups")
    async def create_backup():
        return await asyncio.to_thread(ctx.backups.create, "manual")

    @app.post("/api/backups/restore")
    async def restore_backup(body: RestoreRequest):
        if (denied := reauth(body.password)) is not None:
            return denied
        if not all(ctx.runs.is_idle(pid) for pid in list(ctx.runs.runtimes)):
            return err(409, "Geri yüklemeden önce tüm projeleri durdurun (yetim süreçler ve bekleyen yeniden "
                            "başlatmalar dahil)")
        try:
            res = await asyncio.to_thread(ctx.backups.restore, body.name)
        except BackupError as e:
            return err(400, str(e))
        ctx.runs.runtimes.clear()
        return res

    # ---------- dışa / içe aktarma ----------
    @app.post("/api/config/export")
    async def export_config(body: ExportRequest):
        try:
            doc = portability.export_config(ctx.repo, body.include_secrets, body.passphrase or None)
        except ValueError as e:
            return err(400, str(e))
        ctx.repo.audit("config_export", f"secrets={body.include_secrets and not doc['secrets_omitted']}")
        name = f"kovan-config-{time.strftime('%Y%m%d-%H%M')}.json"
        return Response(json.dumps(doc, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def _plan(body: ImportPayload):
        doc = portability.parse_import(body.content)
        plan = portability.plan_import(ctx.repo, doc, body.root_map, body.passphrase or None, body.keep_watch)
        return doc, plan

    @app.post("/api/config/import/preview")
    async def import_preview(body: ImportPayload):
        try:
            _, plan = await asyncio.to_thread(_plan, body)
        except portability.ImportError_ as e:
            return err(400, str(e))
        plan.pop("_secrets", None)
        return plan

    @app.post("/api/config/import/apply")
    async def import_apply(body: ImportPayload):
        if body.mode not in ("merge", "replace"):
            return err(400, "Geçersiz mod")
        if (denied := reauth(body.password)) is not None:
            return denied
        if body.mode == "replace" and not all(ctx.runs.is_idle(pid) for pid in list(ctx.runs.runtimes)):
            return err(409, "'Değiştir' modundan önce tüm projeleri durdurun (yetim süreçler ve bekleyen yeniden "
                            "başlatmalar dahil)")
        try:
            doc, plan = await asyncio.to_thread(_plan, body)
            busy = [i["name"] for i in plan["projects"] if i["action"] == "update" and i["local_id"]
                    and not ctx.runs.is_idle(i["local_id"])]
            if busy:
                return err(409, "Güncellenecek projeler çalışıyor veya yeniden başlatma bekliyor; önce durdurun: "
                                + ", ".join(busy), code="busy", projects=busy)
            backup = await asyncio.to_thread(ctx.backups.create, "pre-import")
            result = await asyncio.to_thread(portability.apply_import, ctx.repo, doc, plan, body.mode)
        except portability.ImportError_ as e:
            return err(400, str(e))
        if body.mode == "replace":
            ctx.runs.runtimes.clear()
        ctx.repo.audit("config_import", json.dumps({**result, "backup": backup["name"]}))
        return {**result, "backup": backup["name"]}

    @app.get("/api/diagnostics")
    async def diagnostics():
        db = config.db_path
        return {
            "version": __version__, "data_dir": str(config.data_dir), "portable": config.portable,
            "schema_version": ctx.repo.schema_version, "schema_supported": migrations.LATEST,
            "db_size": db.stat().st_size if db.exists() else 0,
            "integrity_ok": await asyncio.to_thread(ctx.repo.integrity_ok),
            "quarantine": ctx.repo.list_quarantine()[:20], "backups": len(ctx.backups.list()),
            "mirror": str(ctx.backups.mirror_path), "migration": ctx.repo.migration,
        }

    # ---------- WebSocket ----------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        session = ctx.auth.get_session(websocket.cookies.get(SESSION_COOKIE))
        if host not in allowed_hosts(config.port) or origin is None or not origin_ok(origin, config.port) \
                or session is None:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        sub = ctx.bus.subscribe()

        async def sender():
            while True:
                first = await sub.queue.get()
                await asyncio.sleep(0.05)  # 50 ms toplu gönderim
                batch = [first]
                while not sub.queue.empty() and len(batch) < 2000:
                    batch.append(sub.queue.get_nowait())
                msg = {"events": batch}
                if sub.dropped:
                    msg["dropped"], sub.dropped = sub.dropped, 0
                await websocket.send_json(msg)

        task = asyncio.create_task(sender())
        try:
            while True:
                data = await websocket.receive_json()
                if ctx.auth.get_session(websocket.cookies.get(SESSION_COOKIE)) is None:
                    break
                ids = {int(i) for i in data.get("ids", []) if str(i).isdigit()}
                if data.get("op") == "sub":
                    sub.log_projects |= ids
                elif data.get("op") == "unsub":
                    sub.log_projects -= ids
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            task.cancel()
            ctx.bus.unsubscribe(sub)
            with contextlib.suppress(Exception):
                await websocket.close()

    return app

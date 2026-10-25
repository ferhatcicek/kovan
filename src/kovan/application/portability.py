"""Taşınabilir yapılandırma: dışa aktarma, içe aktarma önizlemesi (fark) ve uygulama.

Dosya biçimi `kovan-config` (JSON). Yollar kök klasöre göre göreli de saklanır; böylece yapılandırma
farklı bir kök klasörü olan başka bir bilgisayara taşınabilir (kök eşleme). İçe aktarılan dosya
GÜVENİLMEYEN girdidir: yalnızca şema doğrulamasından geçen veri kullanılır, hiçbir proje otomatik başlatılmaz.
"""
from __future__ import annotations

import json
import ntpath
import os
import platform
import posixpath
import time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from .. import __version__
from ..domain.launchers import LaunchConfigError, build_command
from ..domain.models import BaseModel, Category, CategoryDefaults, Project, RestartConfig, RunProfile
from ..domain.paths import PathNotAllowed, resolve_within
from ..infrastructure import crypto
from ..infrastructure.db import Repository

FORMAT = "kovan-config"
LEGACY_FORMATS = ("pydeck-config",)
FORMAT_VERSION = 1
MAX_BYTES = 5 * 1024 * 1024


class ImportError_(ValueError):
    pass


# ---------------------------------------------------------------- dosya şeması
class XRoot(BaseModel):
    id: str = Field(max_length=20)
    path: str = Field(max_length=1000)


def is_unc(path: str) -> bool:
    r"""\\sunucu\paylaşım veya //sunucu/… — Windows'ta bu yollara dokunmak NTLM özetini sızdırabilir."""
    return path.startswith(("\\\\", "//")) or path.startswith("\\??\\")


class XCategory(BaseModel):
    uid: str = Field(min_length=1, max_length=64)
    name: str
    color: str = "#6366f1"
    icon: str = Field("", max_length=8)
    parent_uid: str | None = None
    sort: int = 0
    description: str = Field("", max_length=500)
    defaults: CategoryDefaults = Field(default_factory=CategoryDefaults)

    @model_validator(mode="after")
    def _as_category(self):
        Category(name=self.name, color=self.color)  # aynı ad/renk kuralları
        return self


class XProject(BaseModel):
    uid: str = Field(min_length=1, max_length=64)
    name: str
    root: str | None = None
    rel_path: str | None = None
    abs_path: str = Field(max_length=1000)
    category_uid: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=50)
    description: str = Field("", max_length=1000)
    favorite: bool = False
    notes: str = Field("", max_length=20000)
    metrics_enabled: bool = False
    notify: bool | None = None
    profiles: list[RunProfile] = Field(min_length=1, max_length=20)
    active_profile: str = "default"

    @field_validator("rel_path")
    @classmethod
    def _rel(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Hem POSIX hem Windows yorumuyla kontrol: sürücü, kök, ters bölü ve '..' yasak
        if "\x00" in v or "\\" in v or ":" in v:
            raise ValueError("rel_path yalnızca '/' ile ayrılmış göreli bir yol olabilir")
        for flavour in (PurePosixPath, PureWindowsPath):
            fp = flavour(v)
            if fp.is_absolute() or fp.anchor or ".." in fp.parts:
                raise ValueError("rel_path göreli olmalı ve '..' içeremez")
        return v

    @field_validator("abs_path")
    @classmethod
    def _abs(cls, v: str) -> str:
        if "\x00" in v or is_unc(v):
            raise ValueError("Ağ (UNC) yolları desteklenmez")
        return v


class XSettings(BaseModel):
    port_range: tuple[int, int] = (8000, 8999)
    metrics_enabled: bool = False

    @field_validator("port_range")
    @classmethod
    def _ports(cls, v: tuple[int, int]) -> tuple[int, int]:
        if not 1 <= v[0] <= v[1] <= 65535:
            raise ValueError("Geçersiz port aralığı")
        return v


class ExportDoc(BaseModel):
    format: Literal["kovan-config"]
    format_version: int
    app_version: str = ""
    exported_at: float = 0
    source: dict = Field(default_factory=dict)
    roots: list[XRoot] = Field(default_factory=list, max_length=50)
    settings: XSettings = Field(default_factory=XSettings)
    categories: list[XCategory] = Field(default_factory=list, max_length=500)
    projects: list[XProject] = Field(default_factory=list, max_length=2000)
    secrets: dict | None = None  # şifreli blok: {project_uid: {profil: {anahtar: değer}}}

    @model_validator(mode="after")
    def _consistency(self):
        for label, items in (("proje", [p.uid for p in self.projects]), ("kategori", [c.uid for c in self.categories]),
                             ("proje yolu", [p.abs_path.casefold() for p in self.projects])):
            if len(items) != len(set(items)):
                raise ValueError(f"Yinelenen {label} kimliği")
        cats = {c.uid: c for c in self.categories}
        for c in self.categories:
            if c.parent_uid:
                parent = cats.get(c.parent_uid)
                if parent is None or parent.parent_uid or parent.uid == c.uid:
                    raise ValueError("Kategoriler yalnızca bir seviye iç içe olabilir")
        for p in self.projects:
            names = [pr.name for pr in p.profiles]
            if len(names) != len(set(names)):
                raise ValueError(f"'{p.name}' projesinde yinelenen profil adı")
        return self


# ---------------------------------------------------------------- dışa aktarma
def _norm(path: str) -> str:
    return str(PurePath(path))


def _split_root(path: str, roots: list[XRoot]) -> tuple[str | None, str | None]:
    """Yolu içeren en uzun kökü bulur; (kök_id, göreli_yol) döner. Windows/Posix yolları için çalışır."""
    flavours = (PureWindowsPath, PurePosixPath)
    best: tuple[str | None, str | None, int] = (None, None, -1)
    for r in roots:
        for F in flavours:
            p, root = F(path), F(r.path)
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if len(root.parts) > best[2]:
                best = (r.id, PurePosixPath(*rel.parts).as_posix() if rel.parts else ".", len(root.parts))
            break
    return best[0], best[1]


def export_config(repo: Repository, include_secrets: bool = False, passphrase: str | None = None) -> dict:
    roots = [XRoot(id=f"r{i + 1}", path=p) for i, p in enumerate(repo.get_setting("roots", []))]
    cats = repo.list_categories()
    by_id = {c.id: c for c in cats}
    secrets: dict = {}
    projects = []
    for p in repo.list_projects():
        root_id, rel = _split_root(p.path, roots)
        profiles = []
        for prof in p.profiles:
            env = []
            for e in prof.env:
                if e.secret and e.value:
                    secrets.setdefault(p.uid, {}).setdefault(prof.name, {})[e.key] = e.value
                    e = e.model_copy(update={"value": ""})
                env.append(e)
            profiles.append(prof.model_copy(update={"env": env}))
        projects.append(XProject(
            uid=p.uid, name=p.name, root=root_id, rel_path=rel, abs_path=p.path,
            category_uid=by_id[p.category_id].uid if p.category_id in by_id else None,
            tags=p.tags, description=p.description, favorite=p.favorite, notes=p.notes,
            metrics_enabled=p.metrics_enabled, notify=p.notify, profiles=profiles, active_profile=p.active_profile))
    doc = ExportDoc(
        format=FORMAT, format_version=FORMAT_VERSION, app_version=__version__, exported_at=time.time(),
        source={"host": platform.node(), "os": platform.system()},
        roots=roots,
        settings=XSettings(port_range=tuple(repo.get_setting("port_range", [8000, 8999])),
                           metrics_enabled=bool(repo.get_setting("metrics_enabled", False))),
        categories=[XCategory(uid=c.uid, name=c.name, color=c.color, icon=c.icon,
                              parent_uid=by_id[c.parent_id].uid if c.parent_id in by_id else None,
                              sort=c.sort, description=c.description, defaults=c.defaults) for c in cats],
        projects=projects)
    out = doc.model_dump(mode="json")
    out["secrets"] = None
    out["secrets_omitted"] = bool(secrets) and not include_secrets
    if include_secrets and secrets:
        if not passphrase:
            raise ValueError("Gizli değerleri dahil etmek için şifreleme parolası gerekli")
        out["secrets"] = crypto.encrypt_json(secrets, passphrase)
    return out


# ---------------------------------------------------------------- içe aktarma
def parse_import(raw: bytes | str) -> ExportDoc:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ImportError_("Dosya çok büyük (en fazla 5 MB)")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise ImportError_("Geçerli bir JSON dosyası değil") from e
    if isinstance(data, dict) and data.get("format") in LEGACY_FORMATS:
        data["format"] = FORMAT  # önceki adla (PyDeck) dışa aktarılmış dosyalar
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ImportError_("Bu bir Kovan yapılandırma dosyası değil")
    fv = data.get("format_version")
    if not isinstance(fv, int) or fv < 1:
        raise ImportError_("Geçersiz biçim sürümü")
    if fv > FORMAT_VERSION:
        raise ImportError_(f"Dosya daha yeni bir Kovan sürümüyle oluşturulmuş (biçim v{fv}); lütfen güncelleyin")
    try:
        return ExportDoc(**data)
    except RecursionError as e:
        raise ImportError_("Dosya yapısı çok derin") from e
    except ValidationError as e:
        first = e.errors()[0]
        raise ImportError_(f"Dosya doğrulanamadı: {'.'.join(map(str, first['loc']))}: {first['msg']}") from e


def _target_path(xp: XProject, root_map: dict[str, str]) -> str:
    if xp.root and xp.rel_path is not None and xp.root in root_map and root_map[xp.root]:
        base = Path(root_map[xp.root])
        return str(base if xp.rel_path == "." else base.joinpath(*PurePosixPath(xp.rel_path).parts))
    return xp.abs_path


DANGEROUS_ENV = ("LD_", "DYLD_", "PYTHON", "PATH", "PATHEXT", "COMSPEC", "NODE_OPTIONS", "PERL5", "RUBYOPT")


def _command_preview(profile: RunProfile, keep_watch: bool) -> dict:
    """İçe aktarılan dosya güvenilmeyen girdidir: çalıştırılacak her şey eksiksiz gösterilir."""
    interp = profile.interpreter.kind.value + (f" ({profile.interpreter.path})" if profile.interpreter.path else "")
    try:
        argv = " ".join(build_command(profile, ["<python>"]).argv)
    except LaunchConfigError as e:
        argv = f"(geçersiz: {e})"
    env = [f"{e.key}=***" if e.secret else f"{e.key}={e.value}" for e in profile.env]
    danger = [e.key for e in profile.env if e.key.upper().startswith(DANGEROUS_ENV)]
    if profile.interpreter.kind.value == "custom":
        danger.append("özel yorumlayıcı yolu")
    restart = profile.restart.policy.value if (keep_watch and profile.restart.policy) else "varsayılan"
    return {"profile": profile.name, "argv": argv, "interpreter": interp, "cwd": profile.cwd or ".",
            "env": env, "danger": danger, "restart": restart}


def _textual_within(path: str, roots: list[str]) -> bool:
    """Dosya sistemine DOKUNMADAN (resolve/stat yok) yolun izinli kökler içinde olup olmadığını denetler."""
    if is_unc(path):
        return False
    mod = ntpath if os.name == "nt" else posixpath
    if not mod.isabs(path):
        return False
    norm = mod.normcase(mod.normpath(path))
    for r in roots:
        rn = mod.normcase(mod.normpath(r))
        if norm == rn or norm.startswith(rn.rstrip(mod.sep) + mod.sep):
            return True
    return False


def _comparable(p: Project) -> dict:
    d = p.model_dump(mode="json", exclude={"id", "uid"})
    for prof in d["profiles"]:
        for e in prof["env"]:
            if e["secret"]:
                e["value"] = ""
    return d


def plan_import(repo: Repository, doc: ExportDoc, root_map: dict[str, str] | None = None,
                passphrase: str | None = None, keep_watch: bool = False) -> dict:
    """Uygulamadan önce ne olacağını hesaplar. Veritabanına yazmaz."""
    root_map = {k: v for k, v in (root_map or {}).items() if v}
    errors: list[str] = []
    new_roots: list[str] = []
    for rid, path in root_map.items():
        if rid not in {r.id for r in doc.roots}:
            errors.append(f"Bilinmeyen kök: {rid}")
        elif is_unc(path) or not Path(path).is_absolute() or not Path(path).is_dir():
            errors.append(f"Eşlenen kök klasör bulunamadı: {path}")
        else:
            new_roots.append(str(Path(path).resolve()))
    roots = list(dict.fromkeys([*repo.get_setting("roots", []), *new_roots]))
    root_paths = [Path(r) for r in roots]

    secrets: dict = {}
    if doc.secrets:
        if passphrase:
            try:
                secrets = crypto.decrypt_json(doc.secrets, passphrase)
            except crypto.DecryptError as e:
                errors.append(str(e))
        else:
            errors.append("Dosya şifreli gizli değerler içeriyor; içe aktarmak için parolayı girin "
                          "(parolasız devam ederseniz gizli değerler içe aktarılmaz)")

    existing = repo.list_projects()
    by_uid = {p.uid: p for p in existing}
    by_path = {_norm(p.path): p for p in existing}
    cats_local = repo.list_categories()
    cat_by_uid = {c.uid: c for c in cats_local}
    cat_by_name = {c.name.lower(): c for c in cats_local}

    cat_items = []
    for xc in doc.categories:
        local = cat_by_uid.get(xc.uid) or cat_by_name.get(xc.name.lower())
        action = "add" if not local else ("unchanged" if (local.name, local.color, local.icon) ==
                                          (xc.name, xc.color, xc.icon) else "update")
        cat_items.append({"uid": xc.uid, "name": xc.name, "action": action, "local_id": local.id if local else None})
    cat_local_id = {ci["uid"]: ci["local_id"] for ci in cat_items if ci["local_id"]}

    items = []
    for xp in doc.projects:
        target = _target_path(xp, root_map)
        item = {"uid": xp.uid, "name": xp.name, "path": target, "source_path": xp.abs_path, "exists": False,
                "commands": [_command_preview(pr, keep_watch) for pr in xp.profiles],
                "local_id": None, "action": "add", "reason": ""}
        try:
            # Önce yalnızca metin üzerinden denetim: kök dışı/UNC yollara dosya sistemi erişimi hiç yapılmaz
            if not _textual_within(target, roots):
                raise PathNotAllowed(target)
            resolved = str(resolve_within(target, root_paths))
            item["path"], item["exists"] = resolved, Path(resolved).is_dir()
        except PathNotAllowed:
            item.update(action="skip", reason="İzinli kök klasörlerin dışında (kök eşlemesi yapın)")
            items.append(item)
            continue
        local = by_uid.get(xp.uid) or by_path.get(_norm(resolved))
        if local:
            item["local_id"] = local.id
            local_cat = cat_local_id.get(xp.category_uid) if xp.category_uid else None
            category_changed = (xp.category_uid is not None and local_cat is None) or local_cat != local.category_id
            candidate = _to_project(xp, resolved, local.category_id, local, {}, keep_watch)
            same = not category_changed and _comparable(candidate) == _comparable(local)
            item["action"] = "unchanged" if same else "update"
        if not item["exists"]:
            item["reason"] = "Klasör bu bilgisayarda yok; yapılandırma yine de saklanır"
        items.append(item)

    summary = {k: sum(1 for i in items if i["action"] == k) for k in ("add", "update", "unchanged", "skip")}
    return {"roots": [r.model_dump() for r in doc.roots], "root_map": root_map, "new_roots": new_roots,
            "categories": cat_items, "projects": items, "summary": summary, "errors": errors,
            "has_secrets": bool(doc.secrets), "secrets_ok": bool(secrets), "source": doc.source,
            "app_version": doc.app_version, "keep_watch": keep_watch, "_secrets": secrets}


def _to_project(xp: XProject, path: str, category_id: int | None, local: Project | None,
                secrets: dict, keep_watch: bool = False) -> Project:
    """keep_watch=False: dosyadaki otomatik yeniden başlatma ayarları yok sayılır (yerel ayar korunur), böylece
    içe aktarılan bir dosya kendiliğinden yeniden başlatma yoluyla komut çalıştıramaz."""
    local_restart = {pr.name: pr.restart for pr in local.profiles} if local else {}
    profiles = []
    local_secret = {}
    if local:
        for prof in local.profiles:
            for e in prof.env:
                if e.secret:
                    local_secret[(prof.name, e.key)] = e.value
    for prof in xp.profiles:
        env = []
        for e in prof.env:
            if e.secret:
                value = secrets.get(xp.uid, {}).get(prof.name, {}).get(e.key) \
                    or local_secret.get((prof.name, e.key), "")  # parolasız içe aktarmada yerel gizli korunur
                e = e.model_copy(update={"value": value})
            env.append(e)
        update = {"env": env}
        if not keep_watch:
            update["restart"] = local_restart.get(prof.name, RestartConfig())
        profiles.append(prof.model_copy(update=update))
    return Project(id=local.id if local else None, uid=xp.uid, name=xp.name, path=path, category_id=category_id,
                   tags=xp.tags, description=xp.description, favorite=xp.favorite, notes=xp.notes,
                   metrics_enabled=xp.metrics_enabled, notify=xp.notify, profiles=profiles,
                   active_profile=xp.active_profile)


def apply_import(repo: Repository, doc: ExportDoc, plan: dict, mode: Literal["merge", "replace"]) -> dict:
    if plan["errors"] and not (len(plan["errors"]) == 1 and plan["has_secrets"] and not plan["secrets_ok"]
                               and "parolayı girin" in plan["errors"][0]):
        raise ImportError_("; ".join(plan["errors"]))
    secrets = plan.get("_secrets") or {}
    roots = list(dict.fromkeys([*repo.get_setting("roots", []), *plan["new_roots"]]))
    xcats = {c.uid: c for c in doc.categories}
    items = {i["uid"]: i for i in plan["projects"]}

    if mode == "replace":
        cats: list[Category] = []
        uid_to_id: dict[str, int] = {}
        for i, xc in enumerate(doc.categories, start=1):
            uid_to_id[xc.uid] = i
        for xc in doc.categories:
            parent = uid_to_id.get(xc.parent_uid) if xc.parent_uid else None
            cats.append(Category(id=uid_to_id[xc.uid], uid=xc.uid, name=xc.name, color=xc.color, icon=xc.icon,
                                 parent_id=parent, sort=xc.sort, description=xc.description, defaults=xc.defaults))
        projects = []
        pid = 0
        for xp in doc.projects:
            it = items[xp.uid]
            if it["action"] == "skip":
                continue
            pid += 1
            p = _to_project(xp, it["path"], uid_to_id.get(xp.category_uid or ""), None, secrets, plan["keep_watch"])
            projects.append(p.model_copy(update={"id": pid}))
        repo.replace_configuration(cats, projects, {
            "roots": roots, "port_range": list(doc.settings.port_range),
            "metrics_enabled": doc.settings.metrics_enabled})
        return {"mode": mode, "categories": len(cats), "projects": len(projects)}

    # ---- birleştir (tek transaction: hata olursa yarım içe aktarma kalmaz)
    with repo.transaction():
        return _merge(repo, doc, plan, xcats, items, secrets, roots, mode)


def _merge(repo, doc, plan, xcats, items, secrets, roots, mode) -> dict:
    uid_to_local: dict[str, int] = {}
    for ci in plan["categories"]:
        xc = xcats[ci["uid"]]
        local = repo.get_category(ci["local_id"]) if ci["local_id"] else None
        base = local or Category(name=xc.name)
        saved = repo.save_category(base.model_copy(update={
            "name": xc.name, "color": xc.color, "icon": xc.icon, "description": xc.description,
            "defaults": xc.defaults, "uid": base.uid if local else xc.uid}))
        uid_to_local[xc.uid] = saved.id
    for xc in doc.categories:  # ikinci geçiş: üst kategori bağları
        if xc.parent_uid and xc.parent_uid in uid_to_local:
            c = repo.get_category(uid_to_local[xc.uid])
            if c and c.parent_id != uid_to_local[xc.parent_uid]:
                repo.save_category(c.model_copy(update={"parent_id": uid_to_local[xc.parent_uid]}))
    counts = {"add": 0, "update": 0}
    for xp in doc.projects:
        it = items[xp.uid]
        if it["action"] not in ("add", "update"):
            continue
        local = repo.get_project(it["local_id"]) if it["local_id"] else None
        p = _to_project(xp, it["path"], uid_to_local.get(xp.category_uid or ""), local, secrets, plan["keep_watch"])
        if local:
            p = p.model_copy(update={"uid": local.uid})
        repo.save_project(p)
        counts[it["action"]] += 1
    repo.set_setting("roots", roots)
    return {"mode": mode, **counts}

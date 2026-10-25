"""Kimlik doğrulama, oturumlar, CSRF, Host/Origin denetimi ve güvenlik başlıkları."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from ..infrastructure.db import Repository

SESSION_COOKIE = "kovan_session"
SESSION_TTL = 8 * 3600
ONCE_TTL = 60
MIN_PASSWORD = 8

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@dataclass
class Session:
    csrf: str
    expires: float


class AuthManager:
    def __init__(self, repo: Repository):
        self.repo = repo
        self._ph = PasswordHasher()
        self._sessions: dict[str, Session] = {}
        self._once: dict[str, float] = {}
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._lock = threading.Lock()

    # --- parola ---
    @property
    def has_password(self) -> bool:
        return bool(self.repo.get_setting("password_hash"))

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Parola en az {MIN_PASSWORD} karakter olmalı")
        self.repo.set_setting("password_hash", self._ph.hash(password))
        with self._lock:
            self._sessions.clear()  # parola değişince tüm oturumlar düşer

    def verify(self, password: str) -> bool:
        now = time.time()
        with self._lock:
            if now < self._locked_until:
                return False
        stored = self.repo.get_setting("password_hash") or ""
        try:
            ok = self._ph.verify(stored, password)
        except (VerificationError, InvalidHashError):
            ok = False
        with self._lock:
            if ok:
                self._fails.clear()
                return True
            self._fails = [t for t in self._fails if now - t < 60] + [now]
            if len(self._fails) >= 5:
                self._locked_until = now + 60 * (len(self._fails) - 4)
        return False

    @property
    def locked_seconds(self) -> int:
        return max(0, int(self._locked_until - time.time()))

    # --- oturumlar ---
    def create_session(self) -> tuple[str, Session]:
        sid = secrets.token_urlsafe(32)
        s = Session(csrf=secrets.token_urlsafe(32), expires=time.time() + SESSION_TTL)
        with self._lock:
            self._sessions[sid] = s
        return sid, s

    def get_session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s and s.expires < time.time():
                del self._sessions[sid]
                return None
            return s

    def destroy(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)

    # --- tek kullanımlık otomatik giriş belirteci (tarayıcıyı panel açar) ---
    def issue_once_token(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self._once[tok] = time.time() + ONCE_TTL
        return tok

    def consume_once_token(self, tok: str) -> bool:
        with self._lock:
            exp = self._once.pop(tok, None)
        return exp is not None and exp > time.time()


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def origin_ok(origin: str | None, port: int) -> bool:
    if origin is None:
        return True  # tarayıcı dışı istemci (Origin yok) – oturum + CSRF yine zorunlu
    return origin in {f"http://{h}" for h in allowed_hosts(port)}


PUBLIC_PATHS = ("/static/", "/auth/", "/favicon")


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth: AuthManager, port: int):
        super().__init__(app)
        self.auth, self.port = auth, port

    async def dispatch(self, request: Request, call_next) -> Response:
        # DNS rebinding koruması
        if request.headers.get("host", "") not in allowed_hosts(self.port):
            return PlainTextResponse("Geçersiz Host başlığı", status_code=400)
        path = request.url.path
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and not origin_ok(request.headers.get("origin"), self.port):
            return JSONResponse({"error": "Geçersiz Origin"}, status_code=403)

        session = self.auth.get_session(request.cookies.get(SESSION_COOKIE))
        request.state.session = session
        if path.startswith("/api/"):
            if session is None:
                return JSONResponse({"error": "Oturum gerekli"}, status_code=401)
            if unsafe:
                token = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(token, session.csrf):
                    return JSONResponse({"error": "CSRF doğrulaması başarısız"}, status_code=403)

        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"  # no-referrer, form POST'larında Origin: null gönderilmesine yol açar
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if path.startswith(("/api/", "/auth/")) or path == "/":
            h["Cache-Control"] = "no-store"
        return response


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="strict", path="/")

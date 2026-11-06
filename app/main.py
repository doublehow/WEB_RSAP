"""RSAP — RCD 監控錄影上傳與回放系統 進入點。

啟動:  .\\.venv\\Scripts\\python.exe run.py(Web UI 與上傳 API 共用埠,預設 5001,
       由 config.json "web_port" / 環境變數 RSAP_WEB_PORT 調整)
"""
import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import archiver, recordings, scanner, thumbs
from app.audit import audit
from app.auth import ROLE_LABELS, admin_password_problem, roles_for_session
from app.config import (INITIAL_ADMIN_PW_FILE, ensure_session_secret, migrate_plaintext_secrets,
                        save_settings, settings)
from app.database import SessionLocal, init_db
from app.routes import auth, logs_routes, settings_routes, upload_api, videos

# 免登入路徑:登入頁、健康檢查、上傳 API(Token 驗證,見 routes/upload_api.py)
PUBLIC_PATHS = {"/login", "/favicon.ico", "/health", "/api/upload", "/upload"}
# 僅管理者可進(含 GET)的路徑前綴:設定、稽核、分權
ADMIN_PREFIXES = ("/settings", "/logs", "/roles")

_LOG_FORMAT = "[%(asctime)s: %(levelname)s/%(name)s] %(message)s"


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    fmt = logging.Formatter(_LOG_FORMAT)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.setFormatter(fmt)


_setup_logging()
migrate_plaintext_secrets()


def _bootstrap_local_admin(log: logging.Logger) -> None:
    """本機 admin 密碼開機檢查:空且 AD 未啟用 → 產生隨機初始密碼(密文存 config、明文寫
    data/initial_admin_password.txt)並標 local_admin_initial;不合規 → 登入後強制變更。"""
    if not settings.local_admin_password and not settings.ad_enabled:
        pw = secrets.token_urlsafe(12)
        save_settings({"local_admin_password": pw, "local_admin_initial": True})
        try:
            INITIAL_ADMIN_PW_FILE.parent.mkdir(exist_ok=True)
            fd = os.open(INITIAL_ADMIN_PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pw + "\n")
            log.critical("本機 admin 尚未設定密碼,已產生初始密碼並寫入 %s;"
                         "請以 admin 登入後立即變更(變更後該檔自動刪除)", INITIAL_ADMIN_PW_FILE)
        except OSError as exc:
            log.critical("初始密碼檔寫入失敗(%s),初始密碼:%s —— 請以 admin 登入後立即變更", exc, pw)
    elif settings.local_admin_password and admin_password_problem(settings.local_admin_password):
        log.critical("本機 admin 密碼不符現行規則,登入後將強制變更")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log = logging.getLogger("rsap.main")
    _bootstrap_local_admin(log)
    try:
        recordings.ensure_upload_dir()
    except OSError as exc:
        log.error("錄影目錄無法建立:%s(請於設定頁修正)", exc)
    if not upload_api.api_enabled():
        log.warning("尚未設定上傳 Token,上傳 API 停用;請至設定頁產生")
    elif upload_api.legacy_mode():
        log.warning("上傳 API 為舊版相容模式(未帶 Token 僅憑 IP 白名單 %s 放行),用戶端更新後請關閉",
                    ", ".join(settings.upload_allowed_ips))
    if not thumbs.ffmpeg_path():
        log.info("未偵測到 ffmpeg:縮圖改由瀏覽器懶載入;安裝後於設定頁填路徑即自動補產")
    archiver.reschedule()
    if archiver.enabled():
        log.info("歸檔排程:超過 %d 天 → %s,下次 %s", settings.archive_after_days,
                 settings.archive_path, archiver.status().get("next_run"))
    task = asyncio.create_task(scanner.scanner_loop())
    yield
    task.cancel()


app = FastAPI(title="RSAP — RCD 監控錄影回放系統", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(upload_api.router)
app.include_router(videos.router)
app.include_router(logs_routes.router)
app.include_router(settings_routes.router)


_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}
if settings.behind_tls:
    _STATIC_HEADERS["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def _csp(nonce: str) -> str:
    """全站零 CDN。script 只允許本站與帶本請求 nonce 的內嵌 <script>;media 僅同源。"""
    return (f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'")


def _secured(resp: Response, nonce: str) -> Response:
    resp.headers.setdefault("Content-Security-Policy", _csp(nonce))
    for k, v in _STATIC_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _deny(request: Request, msg: str, status: int = 403) -> Response:
    nonce = request.state.csp_nonce
    if _wants_json(request):
        return _secured(JSONResponse({"ok": False, "error": msg}, status_code=status), nonce)
    return _secured(HTMLResponse(
        f'<div style="font-family:sans-serif;padding:40px;max-width:600px">'
        f'<h2>{status} 權限不足</h2><p>{msg}</p>'
        f'<p><a href="/">← 回首頁</a></p></div>',
        status_code=status), nonce)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """未登入導向 /login;唯讀角色擋非 GET;非 GET 需 CSRF token;統一加安全標頭。
    上傳 API 等 PUBLIC_PATHS 直接放行(各自驗證)。"""
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    path = request.url.path
    if path in PUBLIC_PATHS:
        return _secured(await call_next(request), nonce)
    user = request.session.get("user")
    if not user:
        if _wants_json(request):
            return _secured(JSONResponse({"ok": False, "error": "未登入"}, status_code=401), nonce)
        return _secured(RedirectResponse("/login", status_code=303), nonce)
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)
    if user.get("must_change_pw") and path not in ("/settings", "/logout"):
        if request.method in ("GET", "HEAD") and not _wants_json(request):
            return _secured(RedirectResponse("/settings?error=請先變更本機 admin 密碼,再使用其他功能",
                                             status_code=303), nonce)
        return _deny(request, "本機 admin 須先變更密碼(設定頁),才能執行其他操作。")
    roles = set(await asyncio.to_thread(roles_for_session, user))
    request.state.roles = sorted(roles)
    # 管理頁面(設定 / 稽核 / 分權)連看都只給管理者:唯讀角色只該看到錄影
    if "full_admin" not in roles and path.startswith(ADMIN_PREFIXES):
        label = "、".join(ROLE_LABELS.get(r, r) for r in roles) or "唯讀"
        audit(request, "access_denied", f"{request.method} {path}(角色 {label})")
        return _deny(request, f"此帳號為{label}權限,無法檢視管理頁面。")
    if request.method not in ("GET", "HEAD"):
        if "full_admin" not in roles and path != "/logout":
            label = "、".join(ROLE_LABELS.get(r, r) for r in roles) or "唯讀"
            audit(request, "access_denied", f"{request.method} {path}(角色 {label})")
            return _deny(request, f"此帳號為{label}權限,不可執行變更操作。")
        await request.body()
        form = await request.form()
        token = str(form.get("_csrf") or request.headers.get("X-CSRF-Token") or "")
        if not hmac.compare_digest(token, request.session["csrf"]):
            audit(request, "csrf_rejected", f"{request.method} {path}")
            return _deny(request, "表單驗證失敗(CSRF token 不符或已過期),請重新整理頁面後再試。")
    return _secured(await call_next(request), nonce)


app.add_middleware(SessionMiddleware, secret_key=ensure_session_secret(),
                   same_site="lax", session_cookie="rsap_session",
                   https_only=settings.behind_tls, max_age=8 * 3600)
# gzip 只壓 HTML / JSON:Starlette GZipMiddleware 預設排除 video/* 與 image/jpeg 等已壓縮媒體,
# 且對 206 Partial Content(影片拖曳的 Range 回應)不壓,串流不受影響
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/status")
def status_api():
    """頂欄指示器 30 秒輪詢:錄影數 / 容量 / 磁碟剩餘 / 最後掃描。"""
    with SessionLocal() as db:
        st = recordings.stats(db)
    sc = scanner.status()
    return {
        "count": st["count"], "bytes": st["bytes"],
        "disk_free": st["free"], "disk_total": st["total"],
        "last_scan": sc["last_scan"].isoformat() if sc.get("last_scan") else None,
        "thumb_pending": sc.get("thumb_pending", 0),
        "archive_online": sc.get("archive_online"), "archive_error": sc.get("archive_error", ""),
        "scanning": bool(sc.get("running")), "error": sc.get("error") or "",
        "ffmpeg": thumbs.available(), "upload_api": upload_api.api_enabled(),
        "upload_legacy": upload_api.legacy_mode(),
        "archive_running": bool(archiver.status().get("running")),
        "archive_next": (archiver.status().get("next_run").isoformat()
                         if archiver.status().get("next_run") else None),
    }

"""模板共用:templates 實例、導覽列、render 快捷(注入登入者 / 角色 / CSRF / CSP nonce)、格式化濾鏡。"""
import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import ROLE_LABELS, roles_for_session

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def fmt_size(nbytes) -> str:
    """位元組 → 易讀(MB / GB / TB,最多 1 位小數)。"""
    try:
        v = float(nbytes or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:,.0f} {unit}" if unit in ("B", "KB") else f"{v:,.1f} {unit}"
        v /= 1024
    return "—"


def fmt_duration(seconds) -> str:
    """秒 → m:ss 或 h:mm:ss;None 回空字串(卡片不顯示片長角標)。"""
    if seconds is None:
        return ""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def avatar_hue(name: str) -> int:
    """電腦名 → 0–359 色相(卡片頭像底色;同名同色)。"""
    return int(hashlib.md5((name or "").encode("utf-8")).hexdigest()[:4], 16) % 360


templates.env.filters["size"] = fmt_size
templates.env.filters["duration"] = fmt_duration
templates.env.filters["hue"] = avatar_hue

# (key, url, 標籤, 僅管理者)—— 唯讀角色不顯示稽核與設定;伺服器端另由 main.ADMIN_PREFIXES 強制
NAV = [
    ("videos", "/", "錄影總覽", False),
    ("logs", "/logs", "稽核紀錄", True),
    ("settings", "/settings", "設定", True),
]


def session_user(request: Request) -> dict:
    try:
        return request.session.get("user") or {}
    except Exception:  # noqa: BLE001
        return {}


def _roles(request: Request) -> set[str]:
    """middleware 每請求已在執行緒內解析角色並放 request.state.roles;缺少時才退回同步解析。"""
    roles = getattr(request.state, "roles", None)
    if roles is None:
        roles = roles_for_session(session_user(request))
    return set(roles)


def role_flags(request: Request) -> dict:
    """角色權限旗標(僅供模板顯示;伺服器端於 middleware 強制)。"""
    return {"can_write": "full_admin" in _roles(request)}


def _csrf(request: Request) -> str:
    try:
        return request.session.get("csrf") or ""
    except Exception:  # noqa: BLE001
        return ""


def render(request: Request, name: str, active: str, **ctx):
    user = session_user(request)
    roles = _roles(request)
    label = "、".join(ROLE_LABELS.get(r, r) for r in ROLE_LABELS if r in roles)
    is_admin = "full_admin" in roles
    return templates.TemplateResponse(request, name, {
        "nav": [n for n in NAV if is_admin or not n[3]], "active": active,
        "user_name": user.get("name", ""), "user_id": user.get("id", ""),
        "must_change_pw": bool(user.get("must_change_pw")),
        "role_label": label or "唯讀",
        "csrf_token": _csrf(request),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        **role_flags(request),
        **ctx})


def render_partial(request: Request, name: str, **ctx) -> HTMLResponse:
    """只渲染面板片段(自動更新時 fetch 替換 #panel 內容)。"""
    return templates.TemplateResponse(request, name, ctx)


def like_escape(q: str) -> str:
    """使用者輸入進 SQL LIKE 前跳脫萬用字元(搭配 .like(..., escape="\\"))。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

"""設定頁:錄影儲存 / 上傳 API / AD 登入 / 本機管理員 / 保留 / 帳號分權;含 AD 逐步測試(AJAX)。"""
import asyncio
import ipaddress
import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import archiver, scanner, thumbs
from app.audit import audit
from app.auth import (ADMIN_PASSWORD_MIN_LEN, ROLE_LABELS, admin_password_problem,
                      set_local_admin_password)
from app.config import (SCAN_MAX, SCAN_MIN, UPLOAD_MB_MAX, UPLOAD_MB_MIN, clamp_int, save_settings,
                        settings, upload_dir)
from app.database import get_db
from app.models import AccountRole
from app.webutil import render

router = APIRouter()


def _split_list(raw: str) -> list[str]:
    return [s.strip() for s in raw.replace("，", ",").replace("\n", ",").split(",") if s.strip()]


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db), saved: str = "", error: str = ""):
    roles = db.query(AccountRole).order_by(AccountRole.username).all()
    return render(request, "settings.html", "settings", s=settings, saved=saved, error=error,
                  admin_pw_min=ADMIN_PASSWORD_MIN_LEN, roles=roles, role_labels=ROLE_LABELS,
                  scan_min=SCAN_MIN, scan_max=SCAN_MAX, mb_min=UPLOAD_MB_MIN, mb_max=UPLOAD_MB_MAX,
                  upload_dir=str(upload_dir()), ffmpeg_found=thumbs.ffmpeg_path(),
                  scan=scanner.status(), host=request.url.hostname, port=settings.web_port,
                  archive=archiver.status())


@router.post("/settings")
async def settings_save(
    request: Request,
    upload_folder: str = Form(""),
    retention_days: str = Form("0"),
    scan_interval_seconds: str = Form("300"),
    ffmpeg_path: str = Form(""),
    thumbs_enabled: str = Form(""),
    upload_enabled: str = Form(""),
    upload_token: str = Form(""),
    upload_max_mb: str = Form("4096"),
    upload_allowed_ips: str = Form(""),
    upload_legacy_no_token: str = Form(""),
    ad_enabled: str = Form(""),
    ad_domain: str = Form(""),
    ad_servers: str = Form(""),
    ad_service_user: str = Form(""),
    ad_service_password: str = Form(""),
    ad_allowed_group: str = Form(""),
    ad_base_dn: str = Form(""),
    ad_use_ssl: str = Form(""),
    local_admin_password: str = Form(""),
    default_role: str = Form("readonly"),
    log_retention_days: str = Form("365"),
    archive_enabled: str = Form(""),
    archive_path: str = Form(""),
    archive_user: str = Form(""),
    archive_password: str = Form(""),
    archive_after_days: str = Form("60"),
    archive_time: str = Form("02:00"),
):
    def _reject(msg: str):
        audit(request, "settings_rejected", msg)
        return RedirectResponse(f"/settings?error={msg},其餘欄位未儲存", status_code=303)

    if local_admin_password:
        problem = admin_password_problem(local_admin_password)
        if problem:
            return _reject(f"本機 admin 密碼不符要求:{problem}")

    folder = upload_folder.strip()
    if folder:
        p = Path(folder).expanduser()
        if not p.is_absolute():
            return _reject("錄影資料夾須為絕對路徑")
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _reject(f"錄影資料夾無法建立或存取:{exc}")

    ips = _split_list(upload_allowed_ips)
    for item in ips:
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            return _reject(f"上傳白名單格式錯誤:{item}(需為 IP 或 CIDR)")

    upload_token = upload_token.strip()
    if upload_token and len(upload_token) < 16:
        return _reject("上傳 Token 至少 16 字元")
    if upload_legacy_no_token and not ips:
        return _reject("啟用「舊版用戶端相容(免 Token)」必須填來源 IP 白名單")

    archive_path = archive_path.strip()
    if archive_path and not (archive_path.startswith("\\\\") or Path(archive_path).is_absolute()):
        return _reject("歸檔路徑須為 UNC(\\\\server\\share\\dir)或絕對路徑")
    if archive_enabled and not archive_path:
        return _reject("啟用歸檔搬移須填網路路徑")
    try:
        hh, mm = archive_time.strip().split(":", 1)
        archive_time = f"{int(hh):02d}:{int(mm):02d}"
        if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError
    except ValueError:
        return _reject("歸檔執行時刻格式須為 HH:MM")

    updates = {
        "archive_enabled": bool(archive_enabled),
        "archive_path": archive_path,
        "archive_user": archive_user.strip(),
        "archive_after_days": clamp_int(archive_after_days, 60, 0, 36500),
        "archive_time": archive_time,
        "upload_folder": folder,
        "retention_days": clamp_int(retention_days, 0, 0, 36500),
        "scan_interval_seconds": clamp_int(scan_interval_seconds, 300, SCAN_MIN, SCAN_MAX),
        "ffmpeg_path": ffmpeg_path.strip(),
        "thumbs_enabled": bool(thumbs_enabled),
        "upload_enabled": bool(upload_enabled),
        "upload_max_mb": clamp_int(upload_max_mb, 4096, UPLOAD_MB_MIN, UPLOAD_MB_MAX),
        "upload_allowed_ips": ips,
        "upload_legacy_no_token": bool(upload_legacy_no_token),
        "ad_enabled": bool(ad_enabled),
        "ad_domain": ad_domain.strip(),
        "ad_servers": _split_list(ad_servers),
        "ad_service_user": ad_service_user.strip(),
        "ad_allowed_group": ad_allowed_group.strip(),
        "ad_base_dn": ad_base_dn.strip(),
        "ad_use_ssl": bool(ad_use_ssl),
        "default_role": default_role if default_role in ROLE_LABELS else "readonly",
        "log_retention_days": clamp_int(log_retention_days, 365, 0, 3650),
    }
    # 密碼 / Token 類欄位:留空 = 不變更
    for field, value in (("ad_service_password", ad_service_password),
                         ("upload_token", upload_token),
                         ("archive_password", archive_password)):
        if value:
            updates[field] = value
    folder_changed = updates["upload_folder"] != (settings.upload_folder or "")
    save_settings(updates)
    thumbs.invalidate()
    archiver.reschedule()
    if local_admin_password:
        set_local_admin_password(local_admin_password)
        u = request.session.get("user") or {}
        if u.get("must_change_pw"):
            request.session["user"] = {**u, "must_change_pw": False}
    notes = []
    if local_admin_password:
        notes.append("本機 admin 密碼")
    if upload_token:
        notes.append("上傳 Token")
    if folder_changed:
        notes.append(f"錄影資料夾 → {upload_dir()}")
    audit(request, "settings_save", "更新全域設定" + (f"({'、'.join(notes)})" if notes else ""))
    scanner.request_scan()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/token")
async def token_generate(request: Request):
    """AJAX:產生隨機 Token 並立即儲存;回傳明文一次(之後只顯示「已設定」)。"""
    tok = secrets.token_urlsafe(32)
    save_settings({"upload_token": tok})
    audit(request, "token_rotate", "重新產生上傳 Token")
    return {"ok": True, "token": tok}


@router.post("/settings/archive-test")
async def archive_test(request: Request):
    """AJAX:以表單填入的網路路徑 / 帳密測試連線與寫入,不需先儲存;密碼留空時退回已儲存值
    (此時路徑須與已儲存相同,防止把密碼送往他處)。"""
    try:
        data = json.loads(await request.body())
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "無效的請求格式"}
    path = str(data.get("archive_path", "")).strip()
    user = str(data.get("archive_user", "")).strip()
    password = str(data.get("archive_password", ""))
    if not password and settings.archive_password:
        saved_path = (settings.archive_path or "").strip()
        # UNC 比對共享根;非 UNC(Linux 掛載點 / 本機路徑)比對完整路徑,避免兩個 None 誤判相同
        if (archiver.share_root(path) or path) != (archiver.share_root(saved_path) or saved_path) \
                or user != settings.archive_user:
            audit(request, "archive_test_blocked", f"沿用已存密碼但目標 {path} / {user} 與已儲存不同")
            return {"ok": False, "message": "使用已儲存的密碼時,路徑與帳號須與已儲存值相同;測試其他目標請一併輸入密碼"}
        password = settings.archive_password
    ok, message = await asyncio.to_thread(archiver.test_connection, path, user, password)
    audit(request, "archive_test", f"{path}:{message}")
    return {"ok": ok, "message": message}


@router.post("/settings/archive-run")
async def archive_run(request: Request):
    """立即執行一次歸檔搬移(背景執行緒;進度見設定頁狀態列)。"""
    if not settings.archive_path.strip() or int(settings.archive_after_days or 0) <= 0:
        return RedirectResponse("/settings?error=請先儲存歸檔路徑與天數", status_code=303)
    if archiver.status().get("running"):
        return RedirectResponse("/settings?error=歸檔已在執行中", status_code=303)
    audit(request, "archive_run", "手動觸發歸檔搬移")

    def _bg():
        from app.database import SessionLocal
        with SessionLocal() as db:
            archiver.run(db, force=True)
    import threading
    threading.Thread(target=_bg, name="rsap-archive", daemon=True).start()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/ad-test")
async def ad_test(request: Request):
    """AJAX:以表單填入的 AD 設定逐步測試,不需先儲存。密碼欄留空時退回已儲存值。"""
    try:
        data = json.loads(await request.body())
    except Exception:  # noqa: BLE001
        return {"ok": False, "steps": [], "message": "無效的請求格式"}
    return await asyncio.to_thread(_ad_test_run, data)


def _ad_test_run(data: dict) -> dict:
    """(同步,於 thread 內執行)AD 連線逐步測試主體。"""
    from ldap3 import ALL, NTLM, SUBTREE, Connection, Server
    from ldap3.core.exceptions import LDAPBindError, LDAPException
    from ldap3.utils.conv import escape_filter_chars

    from app.auth import group_match  # 亦觸發 MD4 修補

    server_ips_raw = str(data.get("ad_servers", "")).strip()
    domain = str(data.get("ad_domain", "")).strip()
    svc_account = str(data.get("ad_service_user", "")).strip()
    svc_password = str(data.get("ad_service_password", "")).strip() or settings.ad_service_password
    allowed_group = str(data.get("ad_allowed_group", "")).strip()
    base_dn = str(data.get("ad_base_dn", "")).strip()
    use_ssl = bool(data.get("ad_use_ssl"))
    test_user = str(data.get("test_username", "")).strip()
    test_pass = str(data.get("test_password", ""))

    steps: list[dict] = []
    if not server_ips_raw or not domain:
        return {"ok": False, "steps": steps, "message": "AD Server IP 或 Domain 未填寫"}
    servers = _split_list(server_ips_raw)

    # 防憑證外送:密碼欄留空 = 沿用已存 service 密碼,此時目標必須是已儲存的 AD 伺服器
    if not str(data.get("ad_service_password", "")).strip() and settings.ad_service_password:
        rogue = [ip for ip in servers if ip not in set(settings.ad_servers)]
        if rogue:
            audit(None, "ad_test_blocked", f"AD 測試遭拒:沿用已存密碼但目標 {rogue} 不在已儲存清單")
            return {"ok": False, "steps": steps, "message":
                    "使用已儲存的 Service 密碼時,Server 僅限已儲存的 AD 伺服器"
                    f"({', '.join(settings.ad_servers) or '未設定'});測試其他伺服器請一併輸入密碼"}

    steps.append({"label": "設定解析", "ok": True, "detail": f"Server: {servers},Domain: {domain}"})

    svc_bind_ok = False
    for ip in servers:
        try:
            ldap_server = Server(ip, get_info=ALL, connect_timeout=5, use_ssl=use_ssl)
            if svc_account and svc_password:
                try:
                    conn = Connection(ldap_server, user=f"{domain}\\{svc_account}",
                                      password=svc_password, authentication=NTLM,
                                      auto_bind=True, receive_timeout=8)
                    conn.unbind()
                    steps.append({"label": f"Service Account Bind ({ip})", "ok": True,
                                  "detail": f"{domain}\\{svc_account} bind 成功"})
                    svc_bind_ok = True
                    break
                except LDAPBindError as exc:
                    steps.append({"label": f"Service Account Bind ({ip})", "ok": False, "detail": f"bind 失敗:{exc}"})
                except LDAPException as exc:
                    steps.append({"label": f"Service Account Bind ({ip})", "ok": False, "detail": f"LDAP 例外:{exc}"})
            else:
                try:
                    conn = Connection(ldap_server, receive_timeout=5)
                    conn.open()
                    conn.unbind()
                    steps.append({"label": f"Server 連通 ({ip})", "ok": True, "detail": "Server 可達(未使用 Service Account)"})
                    svc_bind_ok = True
                    break
                except Exception as exc:  # noqa: BLE001
                    steps.append({"label": f"Server 連通 ({ip})", "ok": False, "detail": f"連線失敗:{exc}"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"label": f"Server 連接 ({ip})", "ok": False, "detail": str(exc)})

    if test_user and test_pass:
        for ip in servers:
            try:
                ldap_server = Server(ip, get_info=ALL, connect_timeout=5, use_ssl=use_ssl)
                try:
                    conn = Connection(ldap_server, user=f"{domain}\\{test_user}", password=test_pass,
                                      authentication=NTLM, auto_bind=True, receive_timeout=8)
                    conn.unbind()
                    steps.append({"label": f"使用者認證 ({ip})", "ok": True, "detail": f"{test_user} 認證成功"})
                    if allowed_group and base_dn and svc_account and svc_password:
                        conn2 = Connection(ldap_server, user=f"{domain}\\{svc_account}", password=svc_password,
                                           authentication=NTLM, auto_bind=True, receive_timeout=8)
                        conn2.search(search_base=base_dn,
                                     search_filter=f"(sAMAccountName={escape_filter_chars(test_user)})",
                                     search_scope=SUBTREE, attributes=["memberOf"])
                        if not conn2.entries:
                            steps.append({"label": "群組驗證", "ok": False, "detail": f"在 Base DN 找不到使用者 {test_user}"})
                        else:
                            try:
                                member_of = list(conn2.entries[0].memberOf) or []
                            except Exception:  # noqa: BLE001
                                member_of = []
                            match = group_match(member_of, allowed_group)
                            steps.append({"label": "群組驗證", "ok": match,
                                          "detail": f"共 {len(member_of)} 個群組,{'包含' if match else '不包含'} '{allowed_group}'"})
                        conn2.unbind()
                    break
                except LDAPBindError as exc:
                    steps.append({"label": f"使用者認證 ({ip})", "ok": False, "detail": f"帳號或密碼錯誤:{exc}"})
                    break
                except LDAPException as exc:
                    steps.append({"label": f"使用者認證 ({ip})", "ok": False, "detail": f"LDAP 例外:{exc}"})
            except Exception as exc:  # noqa: BLE001
                steps.append({"label": f"使用者認證 ({ip})", "ok": False, "detail": str(exc)})
    else:
        steps.append({"label": "使用者認證", "ok": None, "detail": "未提供測試帳號密碼,跳過"})

    return {"ok": svc_bind_ok, "steps": steps,
            "message": "測試完成" if svc_bind_ok else "部分測試失敗,請查看詳情"}

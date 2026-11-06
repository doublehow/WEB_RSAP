"""上傳 API(與 Web UI 共用埠,免 session):用戶端以 Token 驗證後 multipart 上傳 .mp4。

    POST /api/upload   (舊版用戶端相容別名:POST /upload)
      標頭:X-Upload-Token: <token>  或  Authorization: Bearer <token>
      內容:multipart/form-data,欄位名 file
      回應:200 {"ok":true,"message":"File uploaded successfully","filename":..,"size":..,"id":..}
            200 {"ok":true,"status":"exists"}   同名且同大小 → 視為重送,不重寫
            400 檔名不合法 / 缺欄位;401 Token 錯;403 來源 IP 不在白名單;409 同名但內容不同;
            413 超過大小上限;429 Token 連續錯誤遭節流;503 Token 未設定(API 停用)

安全:檔名只收純檔名且須符合 SAFE_NAME_RE;寫入先落 <name>.part 再 os.replace(原子);逐塊計數,
超過上限即中止並刪暫存;Token 以 compare_digest 比對;錯誤 Token 走 login_guard 的 IP 節流。
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import shutil

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from app import login_guard, recordings, scanner
from app.audit import audit, client_ip
from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger("rsap.upload")
router = APIRouter()

_CHUNK = 1 << 20


class _TooLarge(Exception):
    pass


def _json(code: int, **body) -> JSONResponse:
    return JSONResponse(body, status_code=code)


def _token_from(request: Request) -> str:
    tok = request.headers.get("X-Upload-Token") or ""
    if not tok:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    return tok


def legacy_mode() -> bool:
    """舊版用戶端相容:未帶 Token 的上傳改以 IP 白名單驗證;白名單為空時不生效(fail-safe)。"""
    return bool(settings.upload_legacy_no_token and settings.upload_allowed_ips)


def api_enabled() -> bool:
    return bool(settings.upload_enabled) and (bool(settings.upload_token) or legacy_mode())


def ip_allowed(ip: str, allowed: list[str]) -> bool:
    """來源 IP 是否在白名單(IP 或 CIDR);白名單空 = 不限;無法解析的項目忽略。"""
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in allowed:
        try:
            if addr in ipaddress.ip_network(item.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def _save_stream(src, dst_tmp: str, max_bytes: int) -> int:
    """(執行緒內)把上傳串流逐塊寫到暫存檔;超過上限丟 _TooLarge。回寫入位元組數。"""
    total = 0
    with open(dst_tmp, "wb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _TooLarge()
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return total


@router.post("/api/upload")
@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    ip = client_ip(request)
    legacy = legacy_mode()
    if not settings.upload_enabled:
        return _json(503, ok=False, error="上傳 API 已停用")
    if not settings.upload_token and not legacy:
        return _json(503, ok=False, error="上傳 API 未啟用(尚未設定 Token)")
    if not ip_allowed(ip, settings.upload_allowed_ips):
        audit(request, "upload_denied", f"來源 {ip} 不在白名單", user="api")
        return _json(403, ok=False, error="來源 IP 不在允許清單")
    wait = login_guard.locked_for("", ip)
    if wait:
        return _json(429, ok=False, error=f"驗證失敗次數過多,請 {wait} 秒後再試")
    token = _token_from(request)
    if token:
        if not settings.upload_token or not hmac.compare_digest(token.encode("utf-8"),
                                                                settings.upload_token.encode("utf-8")):
            login_guard.record_failure("", ip)
            audit(request, "upload_denied", f"來源 {ip} Token 不符", user="api")
            return _json(401, ok=False, error="Token 錯誤")
    elif not legacy:
        login_guard.record_failure("", ip)
        audit(request, "upload_denied", f"來源 {ip} 未帶 Token", user="api")
        return _json(401, ok=False, error="缺少 Token")
    # 走到這裡而 token 為空 = 舊版相容模式:僅憑 IP 白名單放行(稽核註記)

    raw_name = (file.filename or "").strip()
    if "/" in raw_name or "\\" in raw_name or not recordings.is_safe_filename(raw_name):
        audit(request, "upload_rejected", f"檔名不合法:{raw_name[:120]!r} 自 {ip}", user="api")
        return _json(400, ok=False, error="檔名不合法(純檔名、僅英數 . _ @ -,須以 .mp4 結尾)")
    try:
        base = recordings.ensure_upload_dir()
    except OSError as exc:
        logger.error("錄影目錄不可用:%s", exc)
        return _json(500, ok=False, error="錄影目錄不可用")
    final = recordings.safe_path(raw_name)
    if final is None:
        return _json(400, ok=False, error="檔名不合法")

    max_bytes = int(settings.upload_max_mb) * 1048576
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared and declared > max_bytes + 4096:   # multipart 邊界額外開銷
        return _json(413, ok=False, error=f"檔案超過上限 {settings.upload_max_mb} MB")

    tmp = str(base / (raw_name + recordings.PART_SUFFIX))
    try:
        size = await asyncio.to_thread(_save_stream, file.file, tmp, max_bytes)
    except _TooLarge:
        _unlink(tmp)
        audit(request, "upload_rejected", f"{raw_name} 超過 {settings.upload_max_mb} MB", user="api")
        return _json(413, ok=False, error=f"檔案超過上限 {settings.upload_max_mb} MB")
    except OSError as exc:
        _unlink(tmp)
        logger.error("寫入 %s 失敗:%s", raw_name, exc)
        return _json(500, ok=False, error="寫入失敗")
    finally:
        await file.close()

    if size == 0:
        _unlink(tmp)
        return _json(400, ok=False, error="空檔案")

    # 同名處理:同大小視為重送(冪等 200);不同則拒絕,不覆蓋既有錄影
    if final.exists():
        try:
            existing = final.stat().st_size
        except OSError:
            existing = -1
        _unlink(tmp)
        if existing == size:
            return _json(200, ok=True, status="exists", message="File already exists", filename=raw_name, size=size)
        audit(request, "upload_rejected", f"{raw_name} 同名但大小不同({existing} ≠ {size}),拒絕覆蓋", user="api")
        return _json(409, ok=False, error="同名檔案已存在且內容不同")

    try:
        os.replace(tmp, final)
    except OSError as exc:
        _unlink(tmp)
        logger.error("移入 %s 失敗:%s", raw_name, exc)
        return _json(500, ok=False, error="寫入失敗")

    def _register():
        with SessionLocal() as db:
            return recordings.register_upload(db, raw_name, ip).id
    try:
        rec_id = await asyncio.to_thread(_register)
    except Exception:  # noqa: BLE001 —— 檔案已落地,索引交給下一輪掃描補
        logger.exception("上傳入索引失敗:%s", raw_name)
        rec_id = None
    audit(request, "upload", f"{raw_name}({size / 1048576:.1f} MB)自 {ip}"
          + ("(相容模式,無 Token)" if not token else ""), user="api")
    logger.info("收到錄影:%s(%.1f MB)自 %s", raw_name, size / 1048576, ip)
    scanner.request_scan()   # 立刻產縮圖
    return _json(200, ok=True, message="File uploaded successfully", filename=raw_name, size=size, id=rec_id)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@router.get("/health")
async def health():
    """免登入健康檢查(監控 / 舊版相容)。"""
    try:
        d = recordings.upload_dir()
        ok = d.is_dir() and shutil.disk_usage(d).free > 0
    except OSError:
        ok = False
    return {"status": "ok" if ok else "degraded", "upload_api": api_enabled()}

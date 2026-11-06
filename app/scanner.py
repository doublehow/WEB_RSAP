"""背景掃描器:依 settings.scan_interval_seconds 對帳錄影目錄與索引、產縮圖、保留清理、稽核清理。

- 每輪在執行緒內執行(SQLite / 檔案 I/O / ffmpeg 皆為同步阻塞)。
- 上傳完成、設定變更、按「立即掃描」呼叫 request_scan() 喚醒,不必等下一個間隔;
  縮圖尚有待辦時本輪結束立刻再跑(不等間隔)。
- 狀態(最後掃描時間、結果、待辦縮圖數)存記憶體供 /api/status 與設定頁呈現。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import timedelta

from app import archiver, recordings, thumbs
from app.config import SCAN_MAX, SCAN_MIN, local_now, settings
from app.database import SessionLocal
from app.models import AuditLog

logger = logging.getLogger("rsap.scanner")

_wake = asyncio.Event()
_loop: asyncio.AbstractEventLoop | None = None
_last_cleanup: float = 0.0
_state_lock = threading.Lock()
_state: dict = {"last_scan": None, "last_result": {}, "thumb_pending": 0, "running": False,
                "error": ""}


def request_scan() -> None:
    """喚醒掃描迴圈立刻執行一輪。可自任何執行緒呼叫。"""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _wake.set()
    else:
        loop.call_soon_threadsafe(_wake.set)


def status() -> dict:
    with _state_lock:
        return dict(_state)


def _set(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def _cleanup_audit() -> None:
    days = int(settings.log_retention_days or 0)
    if days <= 0:
        return
    cutoff = local_now() - timedelta(days=days)
    try:
        with SessionLocal() as db:
            n = db.query(AuditLog).filter(AuditLog.ts < cutoff).delete(synchronize_session=False)
            db.commit()
        if n:
            logger.info("稽核清理:刪除 %d 筆超過 %d 天的紀錄", n, days)
    except Exception:  # noqa: BLE001
        logger.exception("稽核清理失敗")


def run_round() -> bool:
    """(執行緒內)一輪:對帳 → 保留清理 → 縮圖;回 True 表示縮圖仍有待辦(呼叫端立刻再跑)。"""
    global _last_cleanup
    _set(running=True, error="")
    more = False
    try:
        with SessionLocal() as db:
            result = recordings.sync_index(db)
            if archiver.due():
                archiver.run(db)                     # 先歸檔再做保留清理,避免該搬的被刪
            result["retained"] = recordings.apply_retention(db)
            done, remaining = thumbs.process_pending(db)
            result["thumbs"] = done
            more = remaining > 0
            _set(last_scan=local_now(), last_result=result, thumb_pending=remaining,
                 archive_online=result.get("archive_online"), archive_error=result.get("archive_error", ""))
    except Exception as exc:  # noqa: BLE001
        logger.exception("掃描失敗")
        _set(error=str(exc)[:300])
    finally:
        _set(running=False)
    if time.monotonic() - _last_cleanup > 86400:
        _last_cleanup = time.monotonic()
        _cleanup_audit()
    return more


async def scanner_loop() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    logger.info("掃描器啟動:目錄 %s,間隔 %d 秒", recordings.upload_dir(), settings.scan_interval_seconds)
    while True:
        more = await asyncio.to_thread(run_round)
        if more:
            await asyncio.sleep(0.2)
            continue
        interval = max(SCAN_MIN, min(SCAN_MAX, int(settings.scan_interval_seconds or 300)))
        # 歸檔排程到點前就醒來(最少 5 秒),不必等整個掃描間隔
        timeout = max(5.0, min(float(interval), archiver.seconds_until_due()))
        try:
            await asyncio.wait_for(_wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        _wake.clear()

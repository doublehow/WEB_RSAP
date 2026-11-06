"""歸檔搬移:錄影時間超過 archive_after_days 的檔案依排程搬到網路路徑(archive_path),釋放本機磁碟。

- 排程:每天 archive_time(HH:MM);
  由 scanner 迴圈在到點時呼叫 run(),設定頁「立即搬移」則 force。
- 網路路徑(UNC)的帳密:Windows 以 WNetAddConnection2W(mpr.dll)建立連線,密碼不經命令列;
  已有同目標連線(85 / 1219)視為可用。
- 非 Windows(Linux / 容器):帳密無作用,路徑由主機掛載(CIFS/NFS)後填掛載點絕對路徑;
  未掛載的掛載點仍是存在的空目錄,無法像 UNC 以連線失敗辨識離線,改以標記檔
  ARCHIVE_MARKER 辨識「歸檔根目錄已就緒」——缺標記一律視為離線(索引與搬移都不動),
  設定頁「連線測試」成功時自動建立標記。
- 搬移流程:copy2 到 <name>.part → 比對大小 → os.replace 為正式名 → 刪本機檔 + 縮圖 + 索引。
  目的端已有同名同大小 → 只刪本機(視為已歸檔);同名不同大小 → 略過並記錯誤,不覆蓋不刪。
- 進度 / 結果存記憶體 _state 供設定頁與 /api/status;每次執行一筆稽核。
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import local_now, settings
from app.models import LOC_ARCHIVE, LOC_LOCAL, Recording
from app.recordings import INDEX_LOCK, safe_path

logger = logging.getLogger("rsap.archiver")


_lock = threading.Lock()
_state: dict = {"last_run": None, "last_result": {}, "next_run": None, "running": False,
                "progress": "", "error": ""}
_next_run: datetime | None = None


# ---------------------------------------------------------------- 排程
def _parse_time(value: str) -> tuple[int, int]:
    try:
        h, m = str(value).strip().split(":", 1)
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return 2, 0


def enabled() -> bool:
    return bool(settings.archive_enabled and settings.archive_path.strip()
                and int(settings.archive_after_days or 0) > 0)


def next_due_after(after: datetime) -> datetime:
    """after 之後的下一個執行時刻:每天 archive_time(HH:MM)。"""
    h, m = _parse_time(settings.archive_time)
    cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= after:
        cand += timedelta(days=1)
    return cand


def reschedule() -> None:
    """設定變更 / 啟動 / 每次執行後重新計算下次執行時刻。"""
    global _next_run
    with _lock:
        _next_run = next_due_after(local_now()) if enabled() else None
        _state["next_run"] = _next_run


def due() -> bool:
    if not enabled():
        return False
    if _next_run is None:
        reschedule()
    return _next_run is not None and local_now() >= _next_run and not _state["running"]


def seconds_until_due() -> float:
    if not enabled():
        return float("inf")
    if _next_run is None:
        reschedule()
    return max(0.0, (_next_run - local_now()).total_seconds()) if _next_run else float("inf")


def status() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


# ---------------------------------------------------------------- 網路路徑連線
def share_root(path: str) -> str | None:
    r"""\\server\share\sub → \\server\share;非 UNC 回 None。"""
    p = path.replace("/", "\\").strip()
    if not p.startswith("\\\\"):
        return None
    parts = [x for x in p[2:].split("\\") if x]
    return f"\\\\{parts[0]}\\{parts[1]}" if len(parts) >= 2 else None


ARCHIVE_MARKER = ".rsap-archive-root"   # 非 Windows:歸檔根目錄的就緒標記檔名


def _posix_root_check(path: str, init_marker: bool) -> str:
    """非 Windows 的歸檔根目錄就緒檢查;回錯誤訊息(空 = 就緒)。
    init_marker=True(設定頁連線測試)時目錄存在即自動建立標記。"""
    try:
        d = Path(path)
        if not d.is_dir():
            return f"歸檔路徑不存在或未掛載:{path}"
        marker = d / ARCHIVE_MARKER
        if marker.is_file():
            return ""
        if init_marker:
            marker.write_bytes(b"RSAP archive root\n")
            return ""
        return (f"歸檔根目錄未就緒(缺標記檔 {ARCHIVE_MARKER}):掛載未完成或路徑有誤;"
                "確認掛載後於設定頁執行「連線測試」即可建立標記")
    except OSError as exc:
        return f"歸檔路徑不可達:{exc}"


def connect(path: str, user: str, password: str, *, init_marker: bool = False) -> str:
    """建立到歸檔根目錄的存取;回錯誤訊息(空 = 成功或不需連線)。
    Windows:UNC 以認證連線;非 Windows:驗證掛載與標記檔(帳密無作用,見模組說明)。"""
    root = share_root(path)
    if sys.platform != "win32":
        if root is not None:
            return f"此平台不支援 UNC 路徑 {root}:請先在主機掛載(如 CIFS),再填掛載點絕對路徑"
        return _posix_root_check(path, init_marker)
    if root is None or not user:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class NETRESOURCE(ctypes.Structure):
            _fields_ = [("dwScope", wintypes.DWORD), ("dwType", wintypes.DWORD),
                        ("dwDisplayType", wintypes.DWORD), ("dwUsage", wintypes.DWORD),
                        ("lpLocalName", wintypes.LPWSTR), ("lpRemoteName", wintypes.LPWSTR),
                        ("lpComment", wintypes.LPWSTR), ("lpProvider", wintypes.LPWSTR)]

        nr = NETRESOURCE()
        nr.dwType = 1                       # RESOURCETYPE_DISK
        nr.lpRemoteName = root
        rc = ctypes.windll.mpr.WNetAddConnection2W(ctypes.byref(nr), password or None, user, 0)
        if rc in (0, 85, 1219):             # OK / ERROR_ALREADY_ASSIGNED / 已有其他認證的連線
            return ""
        return f"連線 {root} 失敗({rc}):{ctypes.FormatError(rc).strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"連線 {root} 失敗:{exc}"


def archive_dir() -> Path | None:
    """已設定的歸檔目錄(不檢查可達性);未設定回 None。"""
    p = (settings.archive_path or "").strip()
    return Path(p) if p else None


def ensure_connected() -> str:
    """以已儲存的帳密連到歸檔目錄;回錯誤訊息(空 = 成功或不需連線)。掃描 / 串流前呼叫。"""
    d = archive_dir()
    if d is None:
        return "未設定歸檔路徑"
    return connect(str(d), settings.archive_user, settings.archive_password)


def test_connection(path: str, user: str, password: str) -> tuple[bool, str]:
    """設定頁測試:連線 → 目錄存在 → 可寫(建立並刪除測試檔)。"""
    path = path.strip()
    if not path:
        return False, "未填網路路徑"
    err = connect(path, user, password, init_marker=True)
    if err:
        return False, err
    try:
        d = Path(path)
        if not d.is_dir():
            return False, f"路徑不存在或不是目錄:{path}"
        probe = d / f".rsap_probe_{os.getpid()}_{int(local_now().timestamp())}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return False, f"路徑可達但無法寫入:{exc}"
    free = ""
    try:
        du = shutil.disk_usage(path)
        free = f",剩餘 {du.free / 1024 ** 3:,.1f} GB"
    except OSError:
        pass
    return True, f"連線與寫入測試成功{free}"


# ---------------------------------------------------------------- 搬移
def _move_one(rec: Recording, dest_dir: Path) -> str:
    """搬一部;回錯誤訊息(空 = 成功)。"""
    src = safe_path(rec.filename)
    if src is None or not src.is_file():
        return "本機檔案不存在"
    dst = dest_dir / rec.filename
    src_size = src.stat().st_size
    if dst.exists():
        if dst.stat().st_size == src_size:
            pass                                        # 已歸檔過:只刪本機
        else:
            return f"目的端已有同名但大小不同的檔案({dst.stat().st_size} ≠ {src_size}),略過"
    else:
        tmp = dest_dir / (rec.filename + ".part")
        try:
            shutil.copy2(src, tmp)
            if tmp.stat().st_size != src_size:
                tmp.unlink(missing_ok=True)
                return "複製後大小不符,已取消"
            os.replace(tmp, dst)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return f"複製失敗:{exc}"
    try:
        src.unlink()
    except OSError as exc:
        return f"已複製到目的端但本機檔刪除失敗:{exc}"
    return ""


def run(db: Session, force: bool = False) -> dict:
    """執行一輪歸檔;回 {'moved','bytes','skipped','errors':[...]}。非 force 時僅在啟用且到點才跑。"""
    global _next_run
    from app import thumbs
    from app.audit import audit

    if not force and not enabled():
        return {}
    with _lock:
        if _state["running"]:
            return {"error": "已在執行中"}
        _state.update(running=True, progress="連線中…", error="")
    started = local_now()
    result = {"moved": 0, "bytes": 0, "skipped": 0, "errors": []}
    try:
        dest_dir = Path(settings.archive_path.strip())
        err = connect(str(dest_dir), settings.archive_user, settings.archive_password)
        if err:
            raise RuntimeError(err)
        dest_dir.mkdir(parents=True, exist_ok=True)
        days = int(settings.archive_after_days or 0)
        if days <= 0:
            raise RuntimeError("未設定歸檔天數")
        cutoff = started - timedelta(days=days)
        todo = db.query(Recording).filter(Recording.recorded_at < cutoff,
                                          Recording.location == LOC_LOCAL) \
                 .order_by(Recording.recorded_at.asc()).all()
        total = len(todo)
        for i, rec in enumerate(todo, 1):
            _set(progress=f"搬移中 {i}/{total}:{rec.filename}")
            # 每檔的「搬移 → 改索引 → commit」持 INDEX_LOCK:對帳整輪也持同一把鎖,
            # 否則對帳快照與這裡的改列交錯會 INSERT 重複或誤刪(見 recordings.INDEX_LOCK)。
            with INDEX_LOCK:
                e = _move_one(rec, dest_dir)
                if e:
                    result["skipped"] += 1
                    if len(result["errors"]) < 20:
                        result["errors"].append(f"{rec.filename}:{e}")
                    logger.warning("歸檔略過 %s:%s", rec.filename, e)
                    continue
                result["moved"] += 1
                result["bytes"] += rec.size_bytes
                # 索引改標「歸檔」而非刪除:id / 縮圖 / 片長保留,總覽「來源:歸檔」可直接播放。
                # 歸檔端已有同名索引(先前掃描補入)→ 刪本機這筆避免 (filename, location) 重複。
                dup = db.query(Recording).filter(Recording.filename == rec.filename,
                                                 Recording.location == LOC_ARCHIVE).first()
                if dup is not None:
                    thumbs.remove(rec.id)
                    db.delete(rec)
                else:
                    rec.location = LOC_ARCHIVE
                db.commit()
        msg = (f"歸檔完成:搬移 {result['moved']} 部 / {result['bytes'] / 1024 ** 3:,.2f} GB"
               f"{',略過 ' + str(result['skipped']) + ' 部' if result['skipped'] else ''} → {dest_dir}")
        logger.info(msg)
        audit(None, "archive", msg, user="scheduler" if not force else "")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        result["error"] = msg
        logger.error("歸檔失敗:%s", msg)
        audit(None, "archive_failed", msg, user="scheduler" if not force else "")
    finally:
        with _lock:
            _state.update(running=False, progress="", last_run=started, last_result=result,
                          error=result.get("error", ""))
            _next_run = next_due_after(started) if enabled() else None
            _state["next_run"] = _next_run
    return result

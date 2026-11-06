"""錄影檔與索引:檔名解析、安全路徑、目錄對帳(sync_index)、刪除、保留清理、統計。

- 檔名約定(與舊版用戶端相同):`<電腦名>_<帳號>_<YYYYMMDDHHMMSS>.mp4`;
  解析不到時電腦 / 帳號為空、錄影時間取檔案 mtime,仍列入索引。
- 目錄是事實來源:sync_index() 以 os.scandir 一次列目錄,與 DB 索引三向對帳
  (新增 / 刪除 / 大小或 mtime 變更),每輪一次查詢、一次 commit。
- 所有存取檔案的入口一律經 safe_path():只收純檔名、resolve 後必須仍在 upload_dir 內。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import from_timestamp, local_now, settings, upload_dir
from app.models import LOC_ARCHIVE, LOC_LOCAL, THUMB_PENDING, Recording

# 索引寫入互斥:整輪對帳、上傳入索引、刪除、歸檔每檔的「改名→刪本機→改索引」段都持有它。
# 否則「對帳先取快照 → 他執行緒搬移/上傳 → 對帳再列目錄」會 INSERT 出 (filename, location) 重複,
# 或把剛被改成 archive 的列當成本機消失而誤刪(2026-11-03 容器版「立即搬移」實測撞 UNIQUE)。
INDEX_LOCK = threading.RLock()

logger = logging.getLogger("rsap.recordings")

# <電腦名>_<帳號>_<YYYYMMDDHHMMSS>.mp4;帳號可含底線(非貪婪 + 尾端 14 位數字錨定)
FNAME_RE = re.compile(r"^(?P<computer>[^_]+)_(?P<account>.+?)_(?P<ts>\d{14})\.mp4$", re.IGNORECASE)
# 上傳 API 接受的檔名:英數開頭,英數 . _ @ - 組成,.mp4 結尾(排除路徑分隔與控制字元)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@\-]{0,199}\.mp4$", re.IGNORECASE)
PART_SUFFIX = ".part"              # 上傳中暫存檔(不含 .mp4 結尾,掃描不會納入)
PART_STALE_S = 24 * 3600           # 殘留 .part 超過一天視為中斷上傳,掃描時清掉


def parse_filename(name: str) -> dict:
    """檔名 → {'computer','account','recorded_at'(datetime|None)}。"""
    m = FNAME_RE.match(name)
    if not m:
        return {"computer": "", "account": "", "recorded_at": None}
    try:
        dt = datetime.strptime(m.group("ts"), "%Y%m%d%H%M%S")
    except ValueError:
        dt = None
    return {"computer": m.group("computer"), "account": m.group("account"), "recorded_at": dt}


def is_safe_filename(name: str) -> bool:
    return bool(name) and SAFE_NAME_RE.match(name) is not None and name not in (".", "..")


def ensure_upload_dir() -> Path:
    """回傳錄影目錄並確保存在(建立失敗由呼叫端處理)。"""
    d = upload_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def base_dir(location: str) -> Path | None:
    """location → 根目錄:local = upload_dir();archive = settings.archive_path(未設定回 None)。"""
    if location == LOC_ARCHIVE:
        from app.archiver import archive_dir
        return archive_dir()
    return upload_dir()


def safe_path(filename: str, location: str = LOC_LOCAL) -> Path | None:
    """純檔名 → 該根目錄內的絕對路徑;含路徑成分、逃出目錄或根目錄未設定者回 None。
    歸檔(UNC)不 resolve(會觸發網路往返且可能把 UNC 改寫),改以 Path 組合 + 純檔名檢查。"""
    if not filename or filename != os.path.basename(filename) or "\x00" in filename:
        return None
    base = base_dir(location)
    if base is None:
        return None
    if location == LOC_ARCHIVE:
        return base / filename
    p = (base / filename).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    return p


def _scan_dir(base: Path) -> dict[str, os.stat_result]:
    """目錄內所有 .mp4(不分大小寫)→ {檔名: stat};順便清除殘留的 .part。"""
    found: dict[str, os.stat_result] = {}
    now = time.time()
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if not e.is_file():
                        continue
                    if e.name.lower().endswith(PART_SUFFIX):
                        st = e.stat()
                        if now - st.st_mtime > PART_STALE_S:
                            os.unlink(e.path)
                            logger.info("清除殘留上傳暫存檔:%s", e.name)
                        continue
                    if e.name.lower().endswith(".mp4"):
                        found[e.name] = e.stat()
                except OSError as exc:
                    logger.warning("讀取 %s 失敗:%s", e.name, exc)
    except FileNotFoundError:
        logger.error("錄影目錄不存在:%s", base)
    return found


_BOX_HDR = struct.Struct(">I4s")
_MAX_DEPTH_SCAN = 4096          # moov 內子 box 掃描上限(防畸形檔無限迴圈)


def mp4_duration(path: Path) -> float | None:
    """純 Python 讀 MP4 容器 moov/mvhd 的 timescale 與 duration → 秒;不需 ffmpeg。
    moov 在檔尾(非 faststart)也能處理:只 seek 跳過 mdat,不讀內容。解析失敗回 None。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            pos, hops = 0, 0
            while pos + 8 <= size and hops < _MAX_DEPTH_SCAN:
                hops += 1
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                box_size, box_type = _BOX_HDR.unpack(hdr)
                hdr_len = 8
                if box_size == 1:                       # 64-bit largesize
                    box_size = struct.unpack(">Q", f.read(8))[0]
                    hdr_len = 16
                elif box_size == 0:                     # 到檔尾
                    box_size = size - pos
                if box_size < hdr_len:
                    return None
                if box_type == b"moov":
                    inner, end = pos + hdr_len, min(pos + box_size, size)
                    while inner + 8 <= end:
                        f.seek(inner)
                        h2 = f.read(8)
                        if len(h2) < 8:
                            return None
                        s2, t2 = _BOX_HDR.unpack(h2)
                        if s2 == 1:
                            s2 = struct.unpack(">Q", f.read(8))[0]
                        elif s2 == 0:
                            s2 = end - inner
                        if s2 < 8:
                            return None
                        if t2 == b"mvhd":
                            ver = f.read(1)[0]
                            f.seek(3, 1)                 # flags
                            if ver == 1:
                                f.seek(16, 1)            # creation / modification(8+8)
                                timescale = struct.unpack(">I", f.read(4))[0]
                                dur = struct.unpack(">Q", f.read(8))[0]
                            else:
                                f.seek(8, 1)             # creation / modification(4+4)
                                timescale, dur = struct.unpack(">II", f.read(8))
                            return round(dur / timescale, 1) if timescale else None
                        inner += s2
                    return None
                pos += box_size
    except (OSError, struct.error, IndexError, ZeroDivisionError):
        return None
    return None


def _fill_from_file(rec: Recording, name: str, st: os.stat_result, base: Path) -> None:
    meta = parse_filename(name)
    rec.computer = meta["computer"][:100]
    rec.account = meta["account"][:100]
    rec.recorded_at = meta["recorded_at"] or from_timestamp(st.st_mtime)
    rec.size_bytes = st.st_size
    rec.mtime = st.st_mtime
    rec.duration_s = mp4_duration(base / name)


def _sync_location(db: Session, location: str, base: Path, on_disk: dict[str, os.stat_result]) -> tuple[int, int, int]:
    """單一根目錄的三向對帳;回 (新增, 移除, 更新)。"""
    from app import thumbs
    rows = {r.filename: r for r in db.query(Recording).filter(Recording.location == location).all()}
    added = removed = updated = 0
    for name, st in on_disk.items():
        rec = rows.get(name)
        if rec is None:
            rec = Recording(filename=name, location=location, uploaded_at=local_now(), thumb_state=THUMB_PENDING)
            _fill_from_file(rec, name, st, base)
            db.add(rec)
            added += 1
        elif rec.size_bytes != st.st_size or abs(rec.mtime - st.st_mtime) > 1:
            _fill_from_file(rec, name, st, base)
            rec.thumb_state = THUMB_PENDING      # 內容變了,縮圖重做
            updated += 1
        elif rec.duration_s is None:             # 舊索引缺片長(例如升級前建立)→ 補讀
            rec.duration_s = mp4_duration(base / name)
    for name, rec in rows.items():
        if name not in on_disk:
            thumbs.remove(rec.id)
            db.delete(rec)
            removed += 1
    return added, removed, updated


def sync_index(db: Session) -> dict:
    """本機錄影資料夾 + 歸檔目錄 ↔ 索引對帳;回 {'added','removed','updated','total',
    'archive_online'(None = 未設定歸檔), 'archive_error'}。

    歸檔目錄連不上(NAS 離線 / 帳密錯)時**不動**歸檔那批索引(否則 NAS 重啟一次索引與縮圖就全部重建),
    只回報 archive_online=False 讓畫面標示離線。整輪持有 INDEX_LOCK。"""
    with INDEX_LOCK:
        return _sync_index_locked(db)


def _sync_index_locked(db: Session) -> dict:
    from app import archiver
    base = ensure_upload_dir()
    a, r, u = _sync_location(db, LOC_LOCAL, base, _scan_dir(base))
    total = a + r + u
    result = {"added": a, "removed": r, "updated": u, "archive_online": None, "archive_error": ""}

    adir = archiver.archive_dir()
    if adir is not None:
        err = archiver.ensure_connected()
        online = False
        if err:
            result["archive_error"] = err
        else:
            try:
                online = adir.is_dir()
                if not online:
                    result["archive_error"] = f"歸檔路徑不存在或不可達:{adir}"
            except OSError as exc:
                result["archive_error"] = f"歸檔路徑不可達:{exc}"
        if online:
            try:
                a2, r2, u2 = _sync_location(db, LOC_ARCHIVE, adir, _scan_dir(adir))
                result["added"] += a2; result["removed"] += r2; result["updated"] += u2
                total += a2 + r2 + u2
            except OSError as exc:              # 列舉途中斷線:整批不動
                db.rollback()
                online = False
                result["archive_error"] = f"歸檔目錄讀取失敗:{exc}"
        result["archive_online"] = online
        if not online:
            logger.warning("歸檔目錄離線,保留既有歸檔索引:%s", result["archive_error"])
    db.commit()
    result["total"] = db.query(func.count(Recording.id)).scalar()
    if total:
        logger.info("索引對帳:新增 %d、移除 %d、更新 %d(共 %d)", result["added"], result["removed"],
                    result["updated"], result["total"])
    return result


def register_upload(db: Session, name: str, ip: str) -> Recording:
    """上傳完成後立即入索引(不等下一輪掃描);同名既有列則更新。持 INDEX_LOCK 與對帳互斥。"""
    with INDEX_LOCK:
        return _register_upload_locked(db, name, ip)


def _register_upload_locked(db: Session, name: str, ip: str) -> Recording:
    st = os.stat(safe_path(name))
    rec = db.query(Recording).filter(Recording.filename == name, Recording.location == LOC_LOCAL).first()
    if rec is None:
        rec = Recording(filename=name, location=LOC_LOCAL, uploaded_at=local_now())
        db.add(rec)
    rec.source_ip = ip[:45]
    rec.uploaded_at = local_now()
    rec.thumb_state = THUMB_PENDING
    _fill_from_file(rec, name, st, upload_dir())
    db.commit()
    return rec


def delete_recording(db: Session, rec: Recording) -> str:
    """刪除檔案 + 縮圖 + 索引;回錯誤訊息(空 = 成功)。檔案不存在視為已刪。持 INDEX_LOCK 與對帳互斥。"""
    with INDEX_LOCK:
        return _delete_recording_locked(db, rec)


def _delete_recording_locked(db: Session, rec: Recording) -> str:
    from app import archiver, thumbs
    if rec.location == LOC_ARCHIVE:
        err = archiver.ensure_connected()
        if err:
            return err
    p = safe_path(rec.filename, rec.location)
    if p is not None and p.exists():
        try:
            p.unlink()
        except OSError as exc:   # Windows:播放中 / 同步中的檔案會被鎖
            return f"刪除檔案失敗:{exc}"
    thumbs.remove(rec.id)
    db.delete(rec)
    db.commit()
    return ""


def apply_retention(db: Session) -> int:
    """依 settings.retention_days 刪除過期**本機**錄影(以錄影時間計;0 = 不清理);歸檔端不清理。回刪除筆數。"""
    days = int(settings.retention_days or 0)
    if days <= 0:
        return 0
    cutoff = local_now() - timedelta(days=days)
    old = db.query(Recording).filter(Recording.recorded_at < cutoff, Recording.location == LOC_LOCAL).all()
    n = 0
    for rec in old:
        err = delete_recording(db, rec)
        if err:
            logger.warning("保留清理:%s —— %s", rec.filename, err)
        else:
            n += 1
    if n:
        logger.info("保留清理:刪除 %d 部超過 %d 天的錄影", n, days)
    return n


def date_counts(db: Session, location: str = "") -> list[tuple[str, int]]:
    """[(YYYY-MM-DD, 數量)],新日期在前(日期選單用);location 空 = 全部。"""
    d = func.date(Recording.recorded_at)
    q = db.query(d, func.count(Recording.id))
    if location:
        q = q.filter(Recording.location == location)
    rows = q.group_by(d).order_by(d.desc()).all()
    return [(str(k), int(n)) for k, n in rows if k]


def latest_date(db: Session, location: str = "") -> str:
    """最新一筆錄影的日期 YYYY-MM-DD(總覽預設篩選用);無資料回 ""。location 空 = 全部。"""
    q = db.query(func.max(func.date(Recording.recorded_at)))
    if location:
        q = q.filter(Recording.location == location)
    v = q.scalar()
    return str(v) if v else ""


def stats(db: Session) -> dict:
    """總覽數字:部數、總容量(本機 / 歸檔分列)、磁碟剩餘(錄影目錄所在磁碟)。"""
    count, total = db.query(func.count(Recording.id), func.coalesce(func.sum(Recording.size_bytes), 0)) \
                     .filter(Recording.location == LOC_LOCAL).one()
    a_count, a_total = db.query(func.count(Recording.id), func.coalesce(func.sum(Recording.size_bytes), 0)) \
                         .filter(Recording.location == LOC_ARCHIVE).one()
    try:
        du = shutil.disk_usage(upload_dir())
        disk = {"total": du.total, "free": du.free}
    except OSError:
        disk = {"total": 0, "free": 0}
    return {"count": int(count), "bytes": int(total), "archive_count": int(a_count),
            "archive_bytes": int(a_total), **disk}

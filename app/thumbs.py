"""縮圖:有 ffmpeg 時擷取第 1 秒畫面存 data/thumbs/<id>.jpg(片長主要由 recordings.mp4_duration 讀容器取得,
ffmpeg 輸出的 Duration 只在容器解析不到時補上)。

- ffmpeg 為**選配**:找不到(settings.ffmpeg_path 與 PATH 皆無)時所有索引標 none,
  前端改用 <video preload=metadata> 懶載入第一幀;之後裝了 ffmpeg,下一輪掃描自動把 none 重排 pending。
- 由掃描器每輪在執行緒內呼叫 process_pending(),每輪最多 BATCH 部,尚有待辦即立刻再跑一輪。
- 子程序一律 timeout,單檔失敗標 failed 不重試(避免壞檔每輪卡住)。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import DATA_DIR, settings
from app.models import THUMB_FAILED, THUMB_NONE, THUMB_OK, THUMB_PENDING, Recording
from app.recordings import safe_path

logger = logging.getLogger("rsap.thumbs")

THUMB_DIR = DATA_DIR / "thumbs"
BATCH = 40                  # 每輪最多處理部數
_TIMEOUT_S = 60             # 單檔 ffmpeg 逾時
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")

_ffmpeg_cache: tuple[float, str | None] = (0.0, None)
_FFMPEG_TTL = 60.0


def ffmpeg_path() -> str | None:
    """可用的 ffmpeg 路徑(60 秒快取;設定變更後呼叫 invalidate())。"""
    global _ffmpeg_cache
    now = time.monotonic()
    if now - _ffmpeg_cache[0] < _FFMPEG_TTL:
        return _ffmpeg_cache[1]
    found: str | None = None
    custom = (settings.ffmpeg_path or "").strip()
    if custom:
        p = Path(custom).expanduser()
        if p.is_file():
            found = str(p)
        elif shutil.which(custom):
            found = shutil.which(custom)
    else:
        found = shutil.which("ffmpeg")
    _ffmpeg_cache = (now, found)
    return found


def invalidate() -> None:
    global _ffmpeg_cache
    _ffmpeg_cache = (0.0, None)


def available() -> bool:
    return bool(settings.thumbs_enabled) and ffmpeg_path() is not None


def thumb_path(rec_id: int) -> Path:
    return THUMB_DIR / f"{rec_id}.jpg"


def remove(rec_id: int) -> None:
    try:
        thumb_path(rec_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("刪縮圖 %s 失敗:%s", rec_id, exc)


def _parse_duration(stderr: str) -> float | None:
    m = _DURATION_RE.search(stderr or "")
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return round(h * 3600 + mi * 60 + s, 1)


def generate(rec: Recording) -> tuple[str, float | None]:
    """產生單部縮圖;回 (thumb_state, duration_s)。"""
    exe = ffmpeg_path()
    src = safe_path(rec.filename, rec.location)
    if exe is None:
        return THUMB_NONE, None
    if src is None or not src.exists():
        return THUMB_FAILED, None
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = thumb_path(rec.id)
    tmp = out.with_suffix(".tmp.jpg")
    duration: float | None = None
    # 先試第 1 秒(避開全黑首幀),片長不足再退回第 0 秒
    for ss in ("1", "0"):
        cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-y",
               "-ss", ss, "-i", str(src), "-frames:v", "1",
               "-vf", "scale=480:-2", "-q:v", "4", str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning("縮圖逾時:%s", rec.filename)
            tmp.unlink(missing_ok=True)
            return THUMB_FAILED, None
        except OSError as exc:
            logger.error("無法執行 ffmpeg(%s):%s", exe, exc)
            invalidate()
            return THUMB_PENDING, None    # ffmpeg 壞了:保留待辦,下輪重新偵測
        duration = duration or _parse_duration(r.stderr)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            try:
                tmp.replace(out)
            except OSError as exc:
                logger.warning("縮圖落地失敗 %s:%s", rec.filename, exc)
                return THUMB_FAILED, duration
            return THUMB_OK, duration
    tmp.unlink(missing_ok=True)
    logger.info("縮圖失敗(ffmpeg 無輸出):%s", rec.filename)
    return THUMB_FAILED, duration


def process_pending(db: Session, limit: int = BATCH) -> tuple[int, int]:
    """處理待辦縮圖;回 (本輪處理數, 剩餘待辦數)。無 ffmpeg 時把 pending 全標 none(一次 UPDATE)。"""
    if not available():
        n = db.query(Recording).filter(Recording.thumb_state == THUMB_PENDING) \
              .update({Recording.thumb_state: THUMB_NONE}, synchronize_session=False)
        db.commit()
        if n:
            logger.info("無 ffmpeg,%d 部錄影改以瀏覽器懶載入預覽", n)
        return 0, 0
    # ffmpeg 現在可用:先前標 none 的重排
    db.query(Recording).filter(Recording.thumb_state == THUMB_NONE) \
      .update({Recording.thumb_state: THUMB_PENDING}, synchronize_session=False)
    db.commit()
    todo = db.query(Recording).filter(Recording.thumb_state == THUMB_PENDING) \
             .order_by(Recording.recorded_at.desc()).limit(limit).all()
    done = 0
    for rec in todo:
        state, dur = generate(rec)
        rec.thumb_state = state
        if dur is not None and rec.duration_s is None:   # 容器解析不到時才用 ffmpeg 的值
            rec.duration_s = dur
        done += 1
        if state == THUMB_PENDING:   # ffmpeg 執行失敗:停止本輪,留待下輪
            break
    db.commit()
    remaining = db.query(Recording).filter(Recording.thumb_state == THUMB_PENDING).count()
    return done, remaining


def pending_count(db: Session) -> int:
    return db.query(Recording).filter(Recording.thumb_state == THUMB_PENDING).count()

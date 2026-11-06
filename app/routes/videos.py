"""錄影總覽(YouTube 式卡片格)/ 觀看頁 / 串流 / 下載 / 縮圖 / 刪除 / 立即掃描。

- `/` 接 q(電腦 / 帳號 / 檔名)、date、sort、page;`?partial=1&since=<token>` 只回卡片格片段
  供自動更新(索引未變回 204)。
- `/media/{id}` 以 FileResponse 串流(Starlette 支援 Range,拖曳進度條不必整檔下載);
  `/download/{id}` 同檔以 attachment 下載。
- 刪除 / 立即掃描為 POST,由 middleware 強制管理者 + CSRF。
"""
from __future__ import annotations

import asyncio
import math
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import archiver, recordings, scanner, thumbs
from app.audit import audit
from app.database import SessionLocal, get_db
from app.models import LOC_ARCHIVE, LOC_LOCAL, THUMB_OK, Recording
from app.webutil import like_escape, render, render_partial

router = APIRouter()

PAGE_SIZE = 48


class VideoFileResponse(FileResponse):
    """影片串流用:分塊 1 MB(Starlette 預設 64 KB)。每塊一次 to_thread 往返,塊越小 CPU 與
    threadpool 競爭越高;實測 20 條全速串流下 1 MB 塊可把總覽頁延遲壓回正常。"""
    chunk_size = 1024 * 1024
SORTS = {"newest": Recording.recorded_at.desc(), "oldest": Recording.recorded_at.asc(),
         "largest": Recording.size_bytes.desc()}


def _index_token(db: Session) -> str:
    """索引指紋:筆數 + 最大 id + 已產縮圖數 + 歸檔連線狀態(變動時卡片要重畫)。"""
    n, mx = db.query(func.count(Recording.id), func.coalesce(func.max(Recording.id), 0)).one()
    ok = db.query(func.count(Recording.id)).filter(Recording.thumb_state == THUMB_OK).scalar()
    return f"{n}|{mx}|{ok}|{scanner.status().get('archive_online')}"


LOCS = {"": "全部來源", LOC_LOCAL: "本機", LOC_ARCHIVE: "歸檔"}
BACK_KEYS = ("q", "date", "computer", "loc", "sort", "page")   # 觀看頁 ?back= 允許帶回總覽的篩選參數


def _safe_back(back: str) -> str:
    """把觀看頁 / 刪除表單帶來的 back(總覽 query string)清洗成只含白名單鍵的新 query string。

    一律重新 urlencode,且不收路徑 / 網址,避免開放式轉向與注入;不合法或空 → 回 ""(= 回總覽預設)。
    """
    if not back or len(back) > 400:
        return ""
    try:
        parsed = parse_qs(back.lstrip("?"), keep_blank_values=True, max_num_fields=20)
    except ValueError:
        return ""
    out = []
    for k in BACK_KEYS:
        v = (parsed.get(k) or [""])[0].strip()[:100]
        if not v:
            if k == "date" and k in parsed:
                out.append((k, ""))         # 空 date = 「全部日期」,須明列,否則回總覽會變成預設最新一天
            continue
        if (k == "sort" and v not in SORTS) or (k == "loc" and v not in LOCS) \
                or (k == "page" and (not v.isdigit() or v == "1")):
            continue
        out.append((k, v))
    return urlencode(out)


def _back_url(back: str, **msg: str) -> str:
    """總覽網址:保留原篩選狀態(back)再附上 saved / error 訊息。"""
    parts = [p for p in (_safe_back(back), urlencode({k: v for k, v in msg.items() if v})) if p]
    return "/?" + "&".join(parts) if parts else "/"


def _query(db: Session, q: str, date: str, computer: str, loc: str = ""):
    query = db.query(Recording)
    if loc:
        query = query.filter(Recording.location == loc)
    if date:
        query = query.filter(func.date(Recording.recorded_at) == date)
    if computer:
        query = query.filter(Recording.computer == computer)
    if q:
        like = f"%{like_escape(q)}%"
        query = query.filter(or_(Recording.computer.like(like, escape="\\"),
                                 Recording.account.like(like, escape="\\"),
                                 Recording.filename.like(like, escape="\\")))
    return query


@router.get("/")
def index(request: Request, db: Session = Depends(get_db), q: str = "", date: str | None = None,
          computer: str = "", sort: str = "newest", page: int = 1, partial: int = 0, since: str = "",
          loc: str = "", saved: str = "", error: str = ""):
    q = q.strip()[:100]
    computer = computer.strip()[:100]
    sort = sort if sort in SORTS else "newest"
    loc = loc if loc in LOCS else ""
    page = max(1, page)
    # 日期:未帶參數(首次進入 / 清除)→ 預設最新一天;帶空值(選單「全部日期」)→ 全部
    latest_date = recordings.latest_date(db, loc)
    date = latest_date if date is None else date.strip()[:10]

    query = _query(db, q, date, computer, loc)
    total = query.count()
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    items = query.order_by(SORTS[sort], Recording.id.desc()) \
                 .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    token = _index_token(db)
    # date 一律明列(含空值 = 全部日期):分頁 / back 連結少了 date 會被當成「未帶參數」而跳回最新一天
    base_qs = urlencode({k: v for k, v in (("q", q), ("computer", computer), ("loc", loc),
                                           ("sort", sort)) if v} | {"date": date})
    back_qs = base_qs + (f"{'&' if base_qs else ''}page={page}" if page > 1 else "")   # 卡片 → 觀看頁 ?back=
    ctx = dict(items=items, total=total, page=page, pages=pages, q=q, date=date, computer=computer,
               sort=sort, loc=loc, base_qs=base_qs, back_qs=back_qs, page_size=PAGE_SIZE, saved=saved, error=error,
               latest_date=latest_date,
               archive_online=scanner.status().get("archive_online"))
    if partial:
        if since and since == token:
            return Response(status_code=204, headers={"X-Snapshot": token})
        resp = render_partial(request, "_panel_videos.html", **ctx)
        resp.headers["X-Snapshot"] = token
        return resp
    dates = recordings.date_counts(db, loc)
    computers = [c for (c,) in db.query(Recording.computer).filter(Recording.computer != "")
                 .distinct().order_by(Recording.computer).all()]
    st = recordings.stats(db)
    return render(request, "index.html", "videos", snapshot=token, dates=dates, computers=computers,
                  stats=st, scan=scanner.status(), sorts=SORTS.keys(), locs=LOCS,
                  has_archive=bool(archiver.archive_dir()), **ctx)


def _get_or_404(db: Session, rec_id: int) -> Recording | None:
    return db.get(Recording, rec_id)


@router.get("/watch/{rec_id}")
def watch(request: Request, rec_id: int, db: Session = Depends(get_db), back: str = ""):
    back = _safe_back(back)                 # 來自總覽的篩選狀態,刪除 / 上下一部 / 相關錄影都帶著走
    rec = _get_or_404(db, rec_id)
    if rec is None:
        return RedirectResponse(_back_url(back, error="找不到該錄影(可能已被刪除)"), status_code=303)
    # 同一台電腦的其他錄影(時間相近優先):之前 6 部 + 之後 6 部
    before = db.query(Recording).filter(Recording.computer == rec.computer, Recording.id != rec.id,
                                        Recording.recorded_at <= rec.recorded_at) \
               .order_by(Recording.recorded_at.desc()).limit(6).all()
    after = db.query(Recording).filter(Recording.computer == rec.computer, Recording.id != rec.id,
                                       Recording.recorded_at > rec.recorded_at) \
              .order_by(Recording.recorded_at.asc()).limit(6).all()
    related = list(reversed(after)) + before
    # 全站時間軸上的前後一部(觀看頁「上一部 / 下一部」)
    newer = db.query(Recording).filter(Recording.recorded_at > rec.recorded_at) \
              .order_by(Recording.recorded_at.asc()).first()
    older = db.query(Recording).filter(Recording.recorded_at < rec.recorded_at) \
              .order_by(Recording.recorded_at.desc()).first()
    archive_online = scanner.status().get("archive_online")
    if rec.location == LOC_ARCHIVE and not archive_online:
        exists = False                      # 歸檔離線:不去碰 UNC(會卡住),直接標示
    else:
        p = recordings.safe_path(rec.filename, rec.location)
        exists = bool(p) and p.is_file()
    return render(request, "watch.html", "videos", rec=rec, related=related, newer=newer, older=older,
                  exists=exists, has_thumb=rec.thumb_state == THUMB_OK, archive_online=archive_online,
                  back=back)


def _file_response(rec_id: int, download: bool):
    """串流 / 下載:**不要**用 Depends(get_db) —— yield 相依的收尾在回應送完後才執行,
    一條 300 MB 串流會把 SQLite 連線佔住數分鐘,100 人同時播放即耗盡連線池
    (2026-10-16 實測 QueuePool timeout,總覽頁卡 54 秒)。改為查完立刻關 session。"""
    with SessionLocal() as db:
        rec = db.get(Recording, rec_id)
        filename, location = (rec.filename, rec.location) if rec else (None, LOC_LOCAL)
    if location == LOC_ARCHIVE and not scanner.status().get("archive_online"):
        return Response("archive offline", status_code=503)
    p = recordings.safe_path(filename, location) if filename else None
    if p is None or not p.is_file():
        return Response("not found", status_code=404)
    return VideoFileResponse(str(p), media_type="video/mp4",
                        filename=filename if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/media/{rec_id}")
def media(rec_id: int):
    return _file_response(rec_id, download=False)


@router.get("/download/{rec_id}")
def download(request: Request, rec_id: int):
    with SessionLocal() as db:
        rec = db.get(Recording, rec_id)
        if rec is not None:
            audit(request, "download", rec.filename)
    return _file_response(rec_id, download=True)


@router.get("/thumb/{rec_id}")
def thumb(rec_id: int):
    p = thumbs.thumb_path(rec_id)
    if not p.is_file():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.post("/recordings/{rec_id}/delete")
def delete(request: Request, rec_id: int, db: Session = Depends(get_db), back: str = Form("")):
    back = _safe_back(back)                 # 刪除後回總覽時保留原本的篩選 / 排序 / 頁碼
    rec = _get_or_404(db, rec_id)
    if rec is None:
        return RedirectResponse(_back_url(back, error="找不到該錄影"), status_code=303)
    name = rec.filename
    err = recordings.delete_recording(db, rec)
    if err:
        audit(request, "delete_failed", f"{name}:{err}")
        return RedirectResponse(f"/watch/{rec_id}?" + urlencode({"error": err, "back": back}), status_code=303)
    audit(request, "delete", f"刪除錄影:{name}")
    return RedirectResponse(_back_url(back, saved="已刪除 " + name), status_code=303)


@router.post("/recordings/rescan")
async def rescan(request: Request):
    scanner.request_scan()
    audit(request, "rescan", "手動觸發目錄掃描")
    await asyncio.sleep(0)
    return RedirectResponse("/?saved=已觸發掃描,索引將於背景更新", status_code=303)

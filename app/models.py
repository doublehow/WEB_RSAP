"""ORM 模型:Recording(錄影索引)/ AccountRole(帳號分權)/ AuditLog(稽核)。

錄影檔本體存在 upload_folder,DB 只存索引(檔名、電腦、帳號、錄影時間、大小、片長、縮圖狀態);
索引由 recordings.sync_index() 與目錄對帳,檔案才是事實來源 —— 目錄少了就刪索引,多了就補。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import local_now


class Base(DeclarativeBase):
    pass


# 縮圖狀態:pending 待產生 / ok 已產生 / none 無 ffmpeg(前端改用 <video> 懶載入)/ failed 產生失敗
THUMB_PENDING, THUMB_OK, THUMB_NONE, THUMB_FAILED = "pending", "ok", "none", "failed"
# 檔案所在:local 本機錄影資料夾 / archive 歸檔路徑(settings.archive_path,通常是 NAS)
LOC_LOCAL, LOC_ARCHIVE = "local", "archive"
LOC_LABELS = {LOC_LOCAL: "本機", LOC_ARCHIVE: "歸檔"}


class Recording(Base):
    __tablename__ = "recordings"
    __table_args__ = (
        UniqueConstraint("filename", "location", name="uq_recordings_filename_location"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))                 # 檔名(不含目錄)
    location: Mapped[str] = mapped_column(String(10), default=LOC_LOCAL)   # local / archive
    computer: Mapped[str] = mapped_column(String(100), default="")     # 由檔名解析;解析不到為空
    account: Mapped[str] = mapped_column(String(100), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime)             # 檔名時間戳;無則檔案 mtime
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mtime: Mapped[float] = mapped_column(Float, default=0.0)            # 檔案 mtime(對帳用)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)   # 片長(秒;需 ffmpeg)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=local_now)   # 首次入索引時間
    source_ip: Mapped[str] = mapped_column(String(45), default="")      # 經 API 上傳的來源;掃描補入為空
    thumb_state: Mapped[str] = mapped_column(String(10), default=THUMB_PENDING)

    @property
    def title(self) -> str:
        """卡片標題:帳號(無法解析時顯示檔名)。"""
        return self.account or self.filename

    @property
    def is_archive(self) -> bool:
        return self.location == LOC_ARCHIVE

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / 1048576, 1)

    @property
    def date_key(self) -> str:
        return self.recorded_at.strftime("%Y-%m-%d") if self.recorded_at else ""


class AccountRole(Base):
    __tablename__ = "account_roles"
    __table_args__ = (
        UniqueConstraint("username", "role", name="uq_account_roles_username_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="readonly")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=local_now)
    user: Mapped[str] = mapped_column(String(100), default="")
    ip: Mapped[str] = mapped_column(String(45), default="")
    action: Mapped[str] = mapped_column(String(50), default="")
    detail: Mapped[str] = mapped_column(Text, default="")

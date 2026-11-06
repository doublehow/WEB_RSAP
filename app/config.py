"""應用設定:環境變數 RSAP_* > config.json > .env。

- config.json 由 Web 設定頁(/settings)維護,是設定的權威來源。
- 存檔即就地更新記憶體單例,掃描器 / 上傳 API / 登入等功能立即讀到新值。
- secret 欄位(上傳 Token、AD service 密碼、本機 admin 密碼、session secret)
  落地一律密文(secret_store),記憶體單例持有明文。
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
INITIAL_ADMIN_PW_FILE = DATA_DIR / "initial_admin_password.txt"   # 首次啟動產生,改密碼後刪除
DEFAULT_UPLOAD_DIR = DATA_DIR / "uploads"                          # upload_folder 留空時的落點

# 由設定頁管理、可持久化到 config.json 的欄位
UI_FIELDS = (
    # 錄影儲存 / 掃描
    "upload_folder",
    "retention_days",
    "scan_interval_seconds",
    "ffmpeg_path",
    "thumbs_enabled",
    # 上傳 API
    "upload_enabled",
    "upload_token",
    "upload_max_mb",
    "upload_allowed_ips",
    "upload_legacy_no_token",
    # AD(NTLM)登入
    "ad_enabled",
    "ad_domain",
    "ad_servers",
    "ad_service_user",
    "ad_service_password",
    "ad_allowed_group",
    "ad_base_dn",
    "ad_use_ssl",
    "local_admin_password",
    "default_role",
    # 歸檔搬移(超過天數的錄影搬到網路路徑)
    "archive_enabled",
    "archive_path",
    "archive_user",
    "archive_password",
    "archive_after_days",
    "archive_time",
    # 保留
    "log_retention_days",
)

# 可持久化但不在設定頁顯示的內部欄位
_PERSIST_FIELDS = UI_FIELDS + ("session_secret", "web_port", "timezone", "trusted_proxies",
                               "behind_tls", "local_admin_initial")

# 密碼類欄位:設定頁留空 = 不變更(避免把明碼 render 到 HTML)
SECRET_FIELDS = ("ad_service_password", "local_admin_password", "upload_token", "archive_password")

# config.json 中以密文保存的欄位(記憶體單例一律持有明文)
_ENC_FIELDS = SECRET_FIELDS + ("session_secret",)

SCAN_MIN, SCAN_MAX = 30, 86400
UPLOAD_MB_MIN, UPLOAD_MB_MAX = 1, 65536


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RSAP_",
        env_file=".env",
        env_file_encoding="utf-8",
        json_file=CONFIG_FILE,
        json_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 錄影儲存 / 掃描 ----
    upload_folder: str = ""              # 錄影存放目錄(絕對路徑);留空 = data/uploads
    retention_days: int = 0              # 錄影保留天數(依錄影時間;0 = 永久保留)
    scan_interval_seconds: int = 300     # 背景掃描目錄與 DB 索引對帳的間隔(秒)
    ffmpeg_path: str = ""                # ffmpeg 執行檔;留空 = 於 PATH 自動偵測
    thumbs_enabled: bool = True          # 有 ffmpeg 時產生縮圖 / 讀取片長

    # ---- 上傳 API(與 Web 共用埠;用戶端以 Token 驗證)----
    upload_enabled: bool = True          # 總開關:關閉時上傳 API 一律 503(不影響 Web)
    upload_token: str = ""               # 空且非相容模式 = 上傳 API 停用(503)
    upload_max_mb: int = 4096            # 單檔上限(MB)
    upload_allowed_ips: list[str] = []   # 允許上傳的來源 IP / CIDR;空 = 不限
    upload_legacy_no_token: bool = False # 舊版用戶端相容:未帶 Token 的上傳改以 IP 白名單驗證(白名單必填才生效)

    # ---- AD(NTLM)驗證登入 ----
    ad_enabled: bool = False
    ad_domain: str = ""              # NetBIOS 名,例:CORP
    ad_servers: list[str] = []       # AD 伺服器 IP 清單
    ad_service_user: str = ""        # 查詢使用者用的 service account
    ad_service_password: str = ""
    ad_allowed_group: str = ""       # 允許登入的群組(留空 = 通過驗證即放行)
    ad_base_dn: str = ""             # 留空自動偵測
    ad_use_ssl: bool = False         # LDAPS(636)
    local_admin_password: str = ""   # 本機備援登入;空且 AD 未啟用時啟動自動產生初始密碼(見 main.lifespan)
    local_admin_initial: bool = False   # True = 目前密碼為系統產生的初始密碼,登入後強制變更
    default_role: str = "readonly"        # 未分權帳號的預設角色

    # ---- 歸檔搬移:錄影時間超過 N 天的檔案依排程搬到網路路徑(釋放本機磁碟)----
    archive_enabled: bool = False
    archive_path: str = ""               # UNC(\\server\share\dir)或本機絕對路徑
    archive_user: str = ""               # 存取網路路徑的帳號(DOMAIN\user);留空 = 用服務執行身分
    archive_password: str = ""
    archive_after_days: int = 60         # 錄影時間超過此天數即搬移(0 = 不搬)
    archive_time: str = "02:00"          # daily 的執行時刻(HH:MM)

    # ---- 保留 ----
    log_retention_days: int = 365    # 稽核紀錄保留天數(0 = 不清理)

    # ---- 服務埠 / 時區(不在設定頁,由 config.json / 環境變數 RSAP_* 管理)----
    web_port: int = 5001             # Web UI 與上傳 API 共用
    timezone: str = "Asia/Taipei"
    behind_tls: bool = False         # 前端有 TLS(反向代理)時設 true:cookie Secure + HSTS
    trusted_proxies: list[str] = ["127.0.0.1"]   # 信任 X-Forwarded-For 的代理 / SLB 來源 IP(uvicorn --forwarded-allow-ips)

    # ---- 內部 ----
    session_secret: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 優先序:初始化參數 > 環境變數 RSAP_* > config.json > .env
        return (init_settings, env_settings, JsonConfigSettingsSource(settings_cls),
                dotenv_settings)


settings = Settings()

from app import secret_store  # noqa: E402 —— 置後避免循環匯入疑慮

for _f in _ENC_FIELDS:
    setattr(settings, _f, secret_store.decrypt(getattr(settings, _f)))


def clamp_int(value, default: int, lo: int, hi: int) -> int:
    """表單字串 → 落在 [lo, hi] 的整數;非數值回 default。"""
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def _tz() -> ZoneInfo | None:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 —— timezone 設錯時退回系統時區
        return None


def local_now() -> datetime:
    """settings.timezone 的當下時間(naive,DB 統一格式)。"""
    tz = _tz()
    return (datetime.now(tz) if tz else datetime.now()).replace(tzinfo=None)


def to_local(dt: datetime) -> datetime:
    """aware datetime → settings.timezone 的 naive 本地時間;naive 輸入視為已是本地時間。"""
    if dt.tzinfo is None:
        return dt
    tz = _tz()
    return (dt.astimezone(tz) if tz else dt.astimezone()).replace(tzinfo=None)


def from_timestamp(ts: float) -> datetime:
    """epoch 秒 → 本地 naive datetime(檔案 mtime 用)。"""
    return to_local(datetime.fromtimestamp(ts, tz=timezone.utc))


def upload_dir() -> Path:
    """錄影存放目錄(已 resolve);留空時用 data/uploads。不在此 mkdir,由 recordings.ensure_upload_dir 處理。"""
    raw = (settings.upload_folder or "").strip()
    return Path(raw).expanduser().resolve() if raw else DEFAULT_UPLOAD_DIR.resolve()


def _read_config_file() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


_save_lock = threading.Lock()


def _write_config_file(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def save_settings(updates: dict) -> None:
    """更新記憶體單例並持久化到 config.json(只接受可持久化欄位)。

    讀-改-寫全程持鎖;secret 欄位落地前加密。
    """
    with _save_lock:
        data = _read_config_file()
        for key, value in updates.items():
            if key not in _PERSIST_FIELDS:
                continue
            setattr(settings, key, value)
            data[key] = (secret_store.encrypt(value)
                         if key in _ENC_FIELDS and value else value)
        _write_config_file(data)


def migrate_plaintext_secrets() -> None:
    """啟動時把 config.json 既有明文 secret 就地改寫為密文(冪等)。"""
    with _save_lock:
        data = _read_config_file()
        changed = [f for f in _ENC_FIELDS
                   if data.get(f) and not secret_store.is_encrypted(data[f])]
        for f in changed:
            data[f] = secret_store.encrypt(data[f])
        if changed:
            _write_config_file(data)
    if changed:
        import logging
        logging.getLogger("rsap.secret").info(
            "config.json 明文 secret 已改寫為密文:%s", ", ".join(changed))


def ensure_session_secret() -> str:
    """回傳 session 簽章密鑰;不存在時自動生成並持久化。"""
    if not settings.session_secret:
        save_settings({"session_secret": secrets.token_hex(32)})
    return settings.session_secret

"""SQLite(data/rsap.db)+ SQLAlchemy 2.x 同步 Session。

啟動時 create_all + 輕量遷移(新欄位冪等 ALTER 補進舊表),不用 Alembic。
"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# 連線池放寬(預設 5 + 溢出 10):多人同時開頁時每個同步路由短暫各佔一條;
# 串流路由**不得**持有 session(見 routes/videos._file_response),否則再大的池也會被耗盡
engine = create_engine(
    f"sqlite:///{DATA_DIR / 'rsap.db'}",
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_size=10, max_overflow=30, pool_timeout=10,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from app import models

    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # 掃描器(執行緒)寫入索引 + Web 讀取並行 → WAL 避免互鎖
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_auditlog_ts ON audit_logs (ts)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_recordings_recorded_at ON recordings (recorded_at)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_recordings_computer ON recordings (computer)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_recordings_account ON recordings (account)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_recordings_thumb_state ON recordings (thumb_state)")
    _migrate(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_recordings_location ON recordings (location)")


def _migrate(engine) -> None:
    """輕量遷移:models 新增欄位時補進既有表(create_all 不會 ALTER 舊表)。"""
    migrations: list[tuple[str, str, str]] = [  # (表, 欄位, ALTER 子句)
        ("recordings", "location",
         "ALTER TABLE recordings ADD COLUMN location VARCHAR(10) NOT NULL DEFAULT 'local'"),
    ]
    with engine.begin() as conn:
        for table, col, ddl in migrations:
            cols = {r[1] for r in
                    conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                conn.exec_driver_sql(ddl)
        # 舊表 filename 為單欄 UNIQUE(建表時內嵌,無法 DROP):重建為 (filename, location) 唯一
        # SQLite 對具名 UNIQUE 約束仍建 sqlite_autoindex_*,故以索引欄位判斷:
        # 唯一索引欄位 == [filename] → 舊 schema,需重建;== [filename, location] → 已是新 schema
        idx = conn.exec_driver_sql("PRAGMA index_list(recordings)").fetchall()
        unique_cols = []
        for r in idx:
            if r[2]:   # unique
                cols = [c[2] for c in conn.exec_driver_sql(f"PRAGMA index_info({r[1]})").fetchall()]
                unique_cols.append(cols)
        has_pair = ["filename", "location"] in unique_cols
        legacy_unique = ["filename"] in unique_cols
        if legacy_unique and not has_pair:
            conn.exec_driver_sql("ALTER TABLE recordings RENAME TO recordings_old")
            from app import models
            models.Recording.__table__.create(conn)
            conn.exec_driver_sql(
                "INSERT INTO recordings (id, filename, location, computer, account, recorded_at, size_bytes, "
                "mtime, duration_s, uploaded_at, source_ip, thumb_state) "
                "SELECT id, filename, location, computer, account, recorded_at, size_bytes, "
                "mtime, duration_s, uploaded_at, source_ip, thumb_state FROM recordings_old")
            conn.exec_driver_sql("DROP TABLE recordings_old")
            for ddl in ("CREATE INDEX IF NOT EXISTS ix_recordings_recorded_at ON recordings (recorded_at)",
                        "CREATE INDEX IF NOT EXISTS ix_recordings_computer ON recordings (computer)",
                        "CREATE INDEX IF NOT EXISTS ix_recordings_account ON recordings (account)",
                        "CREATE INDEX IF NOT EXISTS ix_recordings_thumb_state ON recordings (thumb_state)",
                        "CREATE INDEX IF NOT EXISTS ix_recordings_location ON recordings (location)"):
                conn.exec_driver_sql(ddl)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

"""自舊版(RCD_Upload/server_web_v6.py)的 config.json 匯入設定。

用法(專案根目錄):
  python -m app.import_legacy <舊版 config.json>

匯入:upload_folder、AD 連線設定(domain / servers / service_user / service_password /
allowed_group / base_dn;**不自動啟用**,請於設定頁測試後再勾選)。
不匯入:舊版明文本機帳號(不符新版密碼規則;新版本機 admin 由服務產生初始密碼)、舊 secret_key。
匯入後請刪除或妥善保管舊版明文 config.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import save_settings


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    src = Path(argv[0])
    try:
        old = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"讀取失敗:{exc}", file=sys.stderr)
        return 1

    updates: dict = {}
    folder = str(old.get("upload_folder") or "").strip()
    if folder:
        updates["upload_folder"] = folder
    ad = old.get("ad") or {}
    mapping = {"domain": "ad_domain", "servers": "ad_servers", "service_user": "ad_service_user",
               "service_password": "ad_service_password", "allowed_group": "ad_allowed_group",
               "base_dn": "ad_base_dn"}
    for k, field in mapping.items():
        v = ad.get(k)
        if v:
            updates[field] = v if field != "ad_servers" else [str(s).strip() for s in v if str(s).strip()]
    if not updates:
        print("舊設定檔無可匯入欄位。")
        return 0
    updates["ad_enabled"] = False
    save_settings(updates)
    shown = {k: ("******" if k == "ad_service_password" else v) for k, v in updates.items()}
    print("已匯入(AD 未啟用,請至設定頁測試後啟用):")
    for k, v in shown.items():
        print(f"  {k} = {v}")
    print(f"請刪除或妥善保管舊版明文設定檔:{src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

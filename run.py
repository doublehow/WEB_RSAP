r"""啟動 RSAP:Web UI 與上傳 API 共用埠,取自設定(config.json "web_port" / 環境變數 RSAP_WEB_PORT,預設 5001)。
信任 X-Forwarded-For 的代理 / SLB IP 取自 config.json "trusted_proxies"(預設 127.0.0.1)。

用法:.\.venv\Scripts\python.exe run.py
"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.web_port,
                proxy_headers=True,
                forwarded_allow_ips=",".join(settings.trusted_proxies) or "127.0.0.1")

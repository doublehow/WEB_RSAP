# RSAP — RCD 監控錄影上傳與回放系統

用戶端電腦把操作錄影(**RCD**,`.mp4`)經 HTTP API 集中上傳到主機;授權人員以瀏覽器登入後,
用 **YouTube 式卡片牆** 依日期 / 電腦 / 帳號瀏覽、搜尋、播放(拖曳進度、倍速)、下載,
管理者可刪除與觸發掃描;所有登入、上傳、下載、刪除、設定異動皆入稽核。給資安 / 稽核 /
維運人員調閱錄影用。

> 內部代號 `rsap`(沿用舊版 AD 授權群組的命名):環境變數 `RSAP_*`、資料庫檔名
> `rsap.db`、session cookie `rsap_session`、log 名稱 `rsap.*` 皆用此代號。
> 本版為舊版(`RCD_Upload/` 下 Flask 雙程式:`server_API.py` 收檔 5000 埠 + `server_web_v6.py`
> 回放 5001 埠)的全面重製,改採 FastAPI 單體骨架;**上傳 API 與 Web 合併為單一埠**。

## 領域內容

| 對象 | 來源 / 依據 | 說明 |
|---|---|---|
| 錄影檔 | 用戶端 Agent 以 multipart POST 上傳,或直接放入錄影資料夾 | 僅接受 `.mp4`;檔名約定 `<電腦名>_<帳號>_<YYYYMMDDHHMMSS>.mp4`(與舊版用戶端相同) |
| 錄影索引 | 背景掃描器對帳資料夾與 SQLite | 電腦 / 帳號 / 錄影時間自檔名解析;解析不到則電腦 / 帳號為空、時間取檔案 mtime,仍可瀏覽 |
| 片長 | 純 Python 讀 MP4 容器 `moov/mvhd`(不需 ffmpeg) | 入索引時即取得;容器解析不到時退回 ffmpeg 輸出或觀看頁瀏覽器 metadata |
| 縮圖 | ffmpeg(選配) | 有 ffmpeg 時擷取第 1 秒畫面(480px JPEG);沒有則卡片由瀏覽器懶載入影片第一幀 |
| 登入者 | AD(NTLM)+ 本機 admin | AD 群組控制誰能登入;登入後角色(管理者 / 唯讀)由分權表決定 |

## 功能特性

| 頁面 | 說明 |
|---|---|
| 錄影總覽 | YouTube 式卡片格(縮圖、片長 / 大小角標、電腦頭像色、帳號標題、電腦與時間);搜尋電腦 / 帳號 / 檔名、日期(**預設只顯示最新一天**,選「全部日期」看全部)/ 電腦下拉、**來源(本機 / 歸檔)**、排序(最新 / 最舊 / 最大)、每頁 48 部分頁;歸檔(NAS)錄影一併索引、可直接播放,NAS 離線時卡片標示離線且索引保留;**懸停 0.5 秒播放靜音預覽**(可關);30 秒自動更新(索引沒變回 204 不重畫);管理者可「立即掃描」並看掃描狀態 |
| 觀看 | 16:9 播放器(拖曳進度走 HTTP Range)、0.5 / 1 / 1.5 / 2 / 4 倍速(記住選擇)、鍵盤(空白、← →、[ ]、F)、上一部 / 下一部、下載、刪除(管理者)、檔案中繼資料、右欄「同一台電腦的錄影」 |
| 稽核紀錄 | 登入 / 登出 / 上傳(含拒絕原因)/ 下載 / 刪除 / 掃描 / 設定 / Token 更換 / 權限拒絕,含帳號與來源 IP,可篩選 |
| 設定 | 錄影資料夾、保留天數、掃描間隔、ffmpeg 路徑與縮圖開關;上傳 Token(一鍵產生)、單檔上限、來源 IP 白名單;**歸檔搬移**(網路路徑 + 帳密、超過天數、每天定時排程、連線測試、立即搬移、狀態與下次執行);AD(NTLM)登入(逐步連線測試);本機 admin 密碼;稽核保留;帳號分權 |

## 服務埠

| 埠 | 用途 |
|---|---|
| **5001** | Web UI **與** 上傳 API 共用(沿用舊版回放埠;舊版 5000 收檔埠不再使用) |

改埠:環境變數 `RSAP_WEB_PORT` 或 `config.json` 的 `"web_port"`(環境變數優先)後重啟;`start_rsap.cmd` 啟動時讀同一設定。

## 技術架構

| 層 | 選擇 |
|---|---|
| 後端 | Python 3.14 / FastAPI + Uvicorn 單程序;上傳 API 與 Web UI 同一 app、同一埠 |
| 資料庫 | SQLite(WAL)+ SQLAlchemy 2.x;三張表:`recordings`(索引)、`account_roles`、`audit_logs`。**錄影檔本體不入庫**,資料夾是事實來源 |
| 掃描器 | `scanner.py` asyncio 迴圈:每輪在執行緒內 `os.scandir` 一次列目錄與索引三向對帳(新增 / 移除 / 變更)→ 保留清理 → 縮圖批次(每輪 40 部,有待辦即連跑)→ 每日稽核清理;上傳 / 設定變更 / 手動按鈕即時喚醒 |
| 上傳 API | Token(`X-Upload-Token` 或 `Authorization: Bearer`,compare_digest)+ 可選 IP / CIDR 白名單;檔名白名單正則;串流寫入 `<name>.part` 逐塊計量、超限即中止,完成 `os.replace` 原子落地;同名同大小視為重送(200)、同名不同內容拒絕(409)不覆蓋;錯誤 Token 走 IP 節流 |
| 片長 | `recordings.mp4_duration()`:只 seek 讀 box 標頭找 `moov/mvhd`(支援 64-bit size、moov 在檔尾),每檔數次小讀取,千部級秒級完成 |
| 縮圖 | `thumbs.py`:ffmpeg 選配(設定路徑或 PATH 自動偵測,60 秒快取);子程序逾時 60 秒;失敗標記不重試;ffmpeg 後來才裝好會自動補產 |
| 歸檔搬移 | `archiver.py`:排程(每天 HH:MM)由掃描迴圈到點觸發;UNC 帳密以 `WNetAddConnection2W`(mpr.dll)建立連線,密碼不經命令列;Linux / 容器則由主機掛載(CIFS/NFS)、填掛載點路徑且帳密留空,以 `.rsap-archive-root` 標記檔辨識掛載就緒(設定頁連線測試自動建立),未掛載視為離線;每檔 copy2 → 核對大小 → 改名 → 刪本機 + 縮圖 + 索引;目的端同名同大小只刪本機、同名不同大小略過;進度與結果在設定頁,每次執行一筆稽核 |
| 串流 | Starlette `FileResponse`(支援 Range / 206),影片改以 1 MB 分塊(`VideoFileResponse`;實測 20 條全速串流總吞吐 325 → 1,276 MB/s、CPU 減半);gzip 中介層對 `video/*`、JPEG、206 回應不壓縮 |
| 前端 | Jinja2 伺服器端渲染,零前端框架、零 CDN;Veeam 式側欄主控台、亮 / 暗主題;卡片格 / 觀看頁為少量原生 JS(IntersectionObserver 懶載入、懸停預覽、partial 自動更新、只操作 DOM 文字節點) |
| 驗證 | 本機 admin(首次啟動自動產生初始密碼於 `data/initial_admin_password.txt`,登入後強制變更;`python -m app.set_admin_password` 可重設 / 停用)+ AD 網域登入(ldap3,NTLM,可選 LDAPS);角色分權(管理者 / 唯讀),每請求重查(10 秒 TTL);登入失敗節流(同帳號 5 次 / 同 IP 20 次於 15 分鐘 → 鎖 15 分鐘;上傳 Token 錯誤亦計入 IP) |
| Session | Starlette SessionMiddleware(簽名 cookie `rsap_session`,8 小時;`behind_tls: true` 時 Secure + HSTS);非 GET 一律驗 CSRF(`_csrf` / `X-CSRF-Token`);上傳 API 免 session |
| 設定 | `config.json`(pydantic-settings;`RSAP_*` 環境變數優先;敏感欄位 AES-256-GCM 密文,金鑰 `data/secret.key`) |
| 部署 | venv + `run.py`;Windows 以 `start_rsap.cmd` 搭配工作排程器開機啟動 |

## 目錄結構

```
WEB_RSAP/
├── run.py                  # 進入點:uvicorn 起服務(埠取自設定,預設 5001)
├── start_rsap.cmd          # Windows 工作排程器啟動腳本(logs/ 每日 log,保留 30 天)
├── app/
│   ├── main.py             # FastAPI app、登入 / RBAC / CSRF middleware、安全標頭、lifespan、/api/status
│   ├── config.py           # config.json(pydantic-settings)、RSAP_* 覆寫、secret 加解密、local_now()、upload_dir()
│   ├── secret_store.py     # AES-256-GCM 加密欄位(金鑰 data/secret.key)
│   ├── set_admin_password.py  # CLI:建立 / 變更 / 停用本機 admin 密碼
│   ├── import_legacy.py    # 自舊版 RCD_Upload/config.json 匯入錄影資料夾與 AD 設定
│   ├── database.py         # engine、WAL、索引、init_db(create_all + 輕量遷移)
│   ├── models.py           # Recording / AccountRole / AuditLog
│   ├── auth.py             # 本機 admin + AD(NTLM)驗證、resolve_roles()
│   ├── login_guard.py      # 登入 / Token 失敗節流(帳號 / IP,記憶體計數)
│   ├── audit.py            # 稽核紀錄 helper
│   ├── recordings.py       # 檔名解析、safe_path、MP4 片長(mvhd)、sync_index 對帳、刪除、保留清理、統計
│   ├── thumbs.py           # ffmpeg 偵測、縮圖產生、待辦批次
│   ├── scanner.py          # 背景掃描迴圈(含歸檔到點觸發)、request_scan()、狀態
│   ├── archiver.py         # 歸檔搬移:排程計算、UNC 認證連線、搬移流程、連線測試、狀態
│   ├── webutil.py          # templates / render / render_partial、size / duration / hue 濾鏡
│   ├── routes/             # auth(登入、分權)/ upload_api(上傳、health)/ videos(總覽、觀看、串流、刪除)/ logs_routes / settings_routes
│   └── web/templates/      # Jinja2:base + login + index(+ _panel_videos 片段)+ watch + logs + settings
├── data/                   # SQLite(rsap.db)、secret.key、thumbs/、uploads/(預設錄影夾)(gitignore,自動建立)
├── logs/                   # start_rsap.cmd 的執行 log(gitignore)
├── config.json             # 全域設定(gitignore,首次啟動自動生成)
├── RCD_Upload/             # 舊版程式與其明文設定(gitignore;僅供匯入參考,勿部署)
├── requirements.txt        # 相依意圖下限
├── requirements.lock.txt   # 部署用精確鎖定(含間接依賴)
├── Dockerfile              # 容器建置:builder(Nuitka 編譯)→ runtime(無原始碼,見「容器化部署」)
├── docker-compose.yml      # 容器部署:埠 / 資料目錄環境變數覆寫、歸檔 NAS 掛載範例
└── preprod-verify.sh       # Linux 預備驗證:build → 起容器 → 煙霧測試 → 自動清除
```

## 主要路由

Web UI 除登入頁外皆需 session;非 GET 需「管理者」角色 + CSRF token;`/settings`、`/logs`、`/roles` 連 GET 都僅管理者(唯讀角色側欄也不顯示)。

| 路由 | 方法 | 說明 |
|---|---|---|
| `/` | GET | 錄影總覽(`q`、`date`(未帶 = 最新一天、`date=` 空值 = 全部)、`computer`、`sort`、`page`;`?partial=1&since=` 只回卡片格片段) |
| `/watch/{id}` | GET | 觀看頁 |
| `/media/{id}` | GET | 影片串流(inline,支援 Range) |
| `/download/{id}` | GET | 下載(attachment;記稽核) |
| `/thumb/{id}` | GET | 縮圖 JPEG(無則 404,前端退回懶載入) |
| `/recordings/{id}/delete` | POST | 刪除錄影(檔案 + 縮圖 + 索引) |
| `/recordings/rescan` | POST | 立即掃描 |
| `/login` `/logout` | GET/POST | 登入 / 登出(本機或 AD;登出為 POST) |
| `/logs` | GET | 稽核紀錄 |
| `/settings`(`/token`、`/ad-test`、`/archive-test`、`/archive-run`) | GET/POST | 設定、產生上傳 Token、AD 逐步測試、歸檔路徑連線測試、立即搬移 |
| `/roles`、`/roles/{id}/delete` | POST | 帳號分權 |
| `/api/status` | GET | 頂欄指示器(JSON:部數、容量、磁碟剩餘、最後掃描、縮圖待辦) |

**無登入的機器介面**(Token / 無驗證):

| 路由 | 方法 | 說明 |
|---|---|---|
| `/api/upload`(別名 `/upload`) | POST | 上傳:標頭 `X-Upload-Token: <token>`(或 `Authorization: Bearer`),multipart 欄位 `file`。200 成功 / 200 `status=exists` 重送 / 400 檔名不合法 / 401 Token 錯 / 403 IP 不在白名單 / 409 同名不同內容 / 413 超過上限 / 429 節流 / 503 API 關閉或 Token 未設定(設定頁「啟用上傳 API」為總開關) |
| `/health` | GET | `{"status":"ok"|"degraded","upload_api":bool}` |

```powershell
curl.exe -X POST "http://<伺服器>:5001/api/upload" -H "X-Upload-Token: <token>" -F "file=@C:\rec\PC01_jsmith_20260101093000.mp4"
```

## 安裝與啟動

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt   # 部署一律裝 lock
.\.venv\Scripts\python.exe run.py
```

首次啟動服務自動產生本機 admin 初始密碼,明文寫入 `data/initial_admin_password.txt`(啟動 log 亦提示)。
瀏覽 `http://<伺服器>:5001`,以 `admin` + 該密碼登入後會被導到設定頁**強制變更**(至少 12 字元;
變更後初始密碼檔自動刪除)。接著在設定頁:

1. 填「錄影資料夾」(例:`C:\Apps\RCD_Upload\Upload`,既有錄影會被掃描進索引)。
2. 按「產生隨機 Token 並儲存」,把 Token 配給用戶端(必要時填來源 IP 白名單)。
3. (選)填 ffmpeg 路徑或把 ffmpeg 加入 PATH,即自動產縮圖(片長不需 ffmpeg)。
4. (選)設定並測試 AD 登入、帳號分權。

忘記密碼時在伺服器執行:

```powershell
.\.venv\Scripts\python.exe -m app.set_admin_password            # 重設(互動輸入)
.\.venv\Scripts\python.exe -m app.set_admin_password --disable  # 停用本機登入(需已啟用 AD)
```

自舊版搬遷(匯入錄影資料夾與 AD 設定,AD 不自動啟用;舊版明文本機帳號不匯入):

```powershell
.\.venv\Scripts\python.exe -m app.import_legacy RCD_Upload\config.json
```

前端有 TLS 反向代理時在 config.json 設 `"behind_tls": true`。

> 不要加 `--reload`:掃描在背景 task 進行,reload 會中斷;更新程式後手動重啟。

### 容器化部署(Linux)

同一份程式碼亦可容器化部署:builder stage 以 Nuitka 把 Python 編成原生二進位,
runtime image 內無原始碼、無直譯器,非 root(uid 1000)執行,內建 ffmpeg 與 tzdata。
**狀態與程式解耦**:DB / 金鑰 / config.json / 影片全在 host 的 data 目錄(bind mount),
image 重建、升級、砍容器都不影響資料。

```bash
# 把版控內容同步到建置機(Linux x86_64;Nuitka 不能跨 OS 編譯)
git archive --format=tar HEAD | ssh <user>@<建置機> "mkdir -p ~/rsap && tar -x -C ~/rsap"

# 預備驗證:build → 起容器 → /health、/、/login 煙霧測試,臨時環境驗完自刪
cd ~/rsap && ./preprod-verify.sh

# 正式部署(host 資料目錄需屬 uid 1000)
sudo install -d -o 1000 -g 1000 /srv/rsap/data
echo RSAP_PORT=8077 > .env   # 埠,必填(host 與容器內同號;同機多實例各取 8001–8099 一號;未設即拒絕啟動)
docker compose up -d        # 資料目錄亦可用 RSAP_DATA 覆寫;.env 不在版控,同步程式碼時勿刪
```

初始 admin 密碼在 `/srv/rsap/data/initial_admin_password.txt`,其後流程與上方相同
(錄影資料夾留空即用 data/uploads;ffmpeg 已內建,路徑填 `ffmpeg`)。
歸檔 NAS 由 host 掛 CIFS(fstab 加 `_netdev,soft,uid=1000,gid=1000`)再依
docker-compose.yml 內註解掛進容器 `/archive`;設定頁路徑填 `/archive`、帳密留空,
按「連線測試」建立就緒標記檔後生效。
Dockerfile 兩個 stage 的 Debian 版本必須一致(glibc),builder 的 Python 版本
跟隨開發環境(目前 3.14);升級時兩處一起改並重跑 `preprod-verify.sh`。

## 功能頁面

- **錄影總覽**:卡片格瀏覽、搜尋 / 日期 / 電腦 / 排序、懸停預覽、分頁、自動更新、立即掃描。
- **觀看**:播放器、倍速、鍵盤、上下一部、下載、刪除、中繼資料、同電腦錄影側欄。
- **稽核紀錄**:最近 300 筆,帳號 / 詳情搜尋、動作篩選。
- **設定**:錄影儲存與掃描、上傳 API、AD 登入、本機管理員與稽核保留、帳號分權。

## 資料與安全

- 上傳 Token、AD service 密碼、本機 admin 密碼、session secret 一律 AES-256-GCM 加密落地,
  金鑰 `data/secret.key` —— **備份 data/ 務必連同金鑰**,遺失金鑰密文不可復原,只能重新輸入。
- 錄影檔只以純檔名定位且 resolve 後必須仍在錄影資料夾內(`recordings.safe_path`);上傳檔名含路徑成分或不符白名單即拒絕。
- 既有錄影**不覆蓋**:同名同大小視為重送,同名不同內容 409,保護證據完整性;刪除僅管理者且入稽核。
- 登入失敗訊息統一「帳號或密碼錯誤」;同帳號 5 次 / 同 IP 20 次失敗(15 分鐘內)鎖 15 分鐘(上傳 Token 錯誤計入 IP)。
- 回應一律帶 CSP(`script-src` 僅同源 + 每請求 nonce,模板不用內嵌事件屬性;`media-src 'self'`)、
  `X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`;`behind_tls` 時加 HSTS。
- 錄影保留天數(依錄影時間)到期由掃描器刪檔;0 = 永久保留。稽核依保留天數每日清理(預設 365 天)。
- 歸檔搬移在保留清理**之前**執行:「錄影保留天數」請設 0 或大於歸檔天數,否則該搬的會先被刪。
  歸檔路徑的密碼 AES-GCM 密文落地;連線測試沿用已存密碼時,目標共享與帳號須與已儲存值相同。
  搬走的錄影索引改標「歸檔」(id、縮圖、片長保留),總覽「來源:歸檔」可直接自 NAS 播放 / 下載;
  搬移結果(部數 / 容量 / 略過原因)記稽核 `archive`。歸檔路徑離線時既有歸檔索引不會被清掉(只標示離線)。
- AD 連線測試沿用已存密碼時,目標伺服器須與已存清單相同,防止把密碼送往假 AD。

## 運維雜項

- 反向代理 / SLB 部署時,在 config.json 設 `"trusted_proxies": ["<SLB SNAT IP>"]`(`run.py` 與 `start_rsap.cmd` 皆讀此值),uvicorn 才會信任該來源的 `X-Forwarded-For`,稽核與登入節流才記到真實使用者 IP;SLB 需開啟 insert X-Forwarded-For。
- 容量實測(2026-10-16,單一 worker):常駐約 100 MB;每條同時播放的串流約多 2 MB 記憶體(1 MB 分塊 + 傳輸緩衝),
  100 人同時播放(500 KB/s)CPU 約一核的 3%、RSS 約 310 MB,200 人約 530 MB,期間總覽 / 觀看 / 拖曳皆 < 40 ms;
  實際上限在網路頻寬(100 人 ≈ 400 Mbps)與磁碟隨機讀取,不在 CPU。
- 掃描間隔 30–86400 秒(預設 300);大量檔案(千部級)一輪對帳僅一次 scandir + 一次查表,秒級完成;縮圖首次補產約每部 0.5–2 秒。
- 舊版用戶端(URL 寫死 `http://<主機>:5000/upload`、無 Token)不改時:把 `web_port` 設 **5000**、
  停掉舊版兩支程式,並在設定頁開「舊版用戶端相容(免 Token)」且填來源 IP 白名單(必填,否則不生效);
  此模式只認來源 IP,屬過渡措施,用戶端重新打包(改打 `/api/upload` + `X-Upload-Token`)後請關閉。
  回應格式相容(含 `message` / `filename`,200 即用戶端刪本機檔)。
- 舊版的 robocopy 同步到 NAS 不受影響(仍以錄影資料夾為來源);手動刪除 / 搬移的檔案於下一輪掃描反映。
- 無 ffmpeg 時卡片由瀏覽器載入影片 metadata 取第一幀,千部級頁面只載入可視範圍(IntersectionObserver),但每張約需 100–500 KB 流量;建議安裝 ffmpeg(只影響縮圖,片長自容器讀取)。

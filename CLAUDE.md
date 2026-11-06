# CLAUDE.md — RSAP 工作約定

給在此專案工作的 AI 的規範。專案總覽 / 啟動 / 架構見 [README.md](README.md)。

## 語言與文件

- 回答與程式碼註解、docstring 一律**繁體中文**。
- 有意義的變更(新增/移除功能、改用法、改目錄或相依)後,同步更新 README.md、
  CLAUDE.md 與相關 docstring。純格式/暫時除錯不必更新文件。
- 完成一段有意義變更後主動 `git commit`,訊息具體說明「改了什麼、為什麼」,
  一個邏輯變更一個 commit。不推送遠端,除非被要求。

## 誠實原則

- 涉及 API/設定/版本等事實,以官方文件為準並附出處,不憑記憶臆測。不確定就說要查證。
- 主張問題前先用直接檢查確認(verify-before-flag);「看起來對」不算驗證。

## 核心架構約定

- **標準骨架**:FastAPI + SQLAlchemy +
  SQLite、Jinja2 模板(Veeam 式側欄、亮/暗雙主題、零 CDN)、config.json 全域設定、
  AD(NTLM)登入 + RBAC、asyncio 背景迴圈。改版型/配色時整站保持一致;
  錄影總覽的卡片格刻意仿 YouTube(縮圖 16:9、角標、圓形頭像、兩行標題),勿改回表格。
- **資料夾是事實來源,DB 只是索引**:`recordings` 表由 `recordings.sync_index()` 與兩個根目錄對帳
  (`location` = `local` → `upload_dir()`;`archive` → `archiver.archive_dir()`,即歸檔 UNC),
  唯一鍵是 (filename, location);不要在別處手寫索引;上傳完成走 `register_upload()` 立即入索引再
  `scanner.request_scan()` 補縮圖。DB 只有三張表(`recordings` / `account_roles` / `audit_logs`)。
  **歸檔離線保護**:`_sync_location()` 只在該根目錄可達時才對帳;`ensure_connected()` 失敗或 `is_dir()`
  為假 → 回 `archive_online=False`、歸檔那批索引與縮圖一律不動(否則 NAS 重啟一次就全部重建)。
  畫面以 `scanner.status()["archive_online"]` 標示離線,`/media` 對離線歸檔回 503、觀看頁不碰 UNC。
  歸檔搬移成功後把列改為 `location=archive`(保留 id / 縮圖),不是刪列;保留清理只刪 `local`。
- **檔案存取一律經 `recordings.safe_path(filename, location)`**:只收純檔名;本機 resolve 後須仍在
  `upload_dir()` 內,歸檔(UNC)不 resolve 只做純檔名檢查;上傳檔名須過 `is_safe_filename()`(`SAFE_NAME_RE`)且不含 `/` `\`。
  檔名約定 `<電腦>_<帳號>_<YYYYMMDDHHMMSS>.mp4`(`FNAME_RE`,帳號可含底線,尾端 14 位數字錨定);
  解析不到也要能入索引(電腦 / 帳號空、時間取 mtime),不要因檔名不合約定就丟棄。
- **上傳 API 與 Web 共用埠、免 session**:`main.PUBLIC_PATHS` 列 `/api/upload`、`/upload`、
  `/health`、`/login`;新增機器介面一律在 `routes/upload_api.py`、自行驗 Token(`compare_digest`)
  並把失敗餵 `login_guard.record_failure("", ip)`。既有檔案**不覆蓋**(同名同大小 200 `exists`、
  不同 409);寫入先 `.part` 再 `os.replace`;大小以逐塊計量為準,Content-Length 只做前置拒絕。
  上傳串流在 `asyncio.to_thread` 內同步寫,不要在 event loop 內逐塊 await。
  「舊版相容模式」`upload_api.legacy_mode()` = `upload_legacy_no_token` 且白名單非空(fail-safe):
  只對**未帶** Token 的請求放行;帶了 Token 一律驗。設定頁開啟時強制白名單必填,勿放寬。
- **片長不靠 ffmpeg**:`recordings.mp4_duration()` 讀容器 `moov/mvhd`,於 `_fill_from_file()` 入索引時取得,
  舊索引缺片長由 `sync_index()` 補讀;ffmpeg 的 Duration 只在容器解析不到時補上,觀看頁再以瀏覽器 metadata 兜底。
  不要再加「需 ffmpeg」之類提示。
- **縮圖為選配**:`thumbs.available()` 為假時索引標 `none`,模板用
  `<video class="lazy" data-src="/media/{id}#t=1" preload="none">` 由 `rsapBindCards()` 懶載入;
  ffmpeg 可用時 `process_pending()` 自動把 `none` 重排 `pending`。新頁面要顯示縮圖請沿用
  `_panel_videos.html` 的 `{% if r.thumb_state == 'ok' %}<img>{% else %}<video class="lazy">` 寫法。
  ffmpeg 子程序必帶 timeout;失敗標 `failed` 不重試;`settings.ffmpeg_path` 變更後呼叫 `thumbs.invalidate()`。
- **歸檔搬移**(`archiver.py`):排程只在 `next_due_after()` 計算(每天 archive_time),
  `reschedule()` 於啟動、設定儲存、每次執行後呼叫;掃描迴圈以 `seconds_until_due()` 縮短等待並在
  `run_round()` 內 `due()` 時呼叫 `run(db)`,**順序固定為 對帳 → 歸檔 → 保留清理**(先搬再刪)。
  UNC 帳密一律經 `connect()`(WNetAddConnection2W),不要用 `net use` 把密碼放上命令列;
  **非 Windows(容器)**:帳密無作用、路徑由主機掛載,`connect()` 改驗 `ARCHIVE_MARKER`
  (`.rsap-archive-root`)標記檔——未掛載的掛載點是存在的空目錄,缺標記一律視為離線,
  防止對帳把歸檔索引整批誤刪;標記只由設定頁「連線測試」(`init_marker=True`)建立,勿在他處自動補。
  跨平台通則:碰 OS 專屬 API 必加 `sys.platform` 防護,並給另一平台正確的離線/降級行為;
  搬移必須 copy → 核對大小 → `os.replace` → 才刪本機,目的端同名不同大小一律略過不覆蓋。
  手動「立即搬移」走 `/settings/archive-run` 開 daemon thread + `force=True`;`_state["running"]` 互斥。
  `archive_password` 已登記 `_ENC_FIELDS`;連線測試沿用已存密碼時須比對 `share_root()` 與帳號。
- **掃描器**(`scanner.py`):每輪 `run_round()` 在執行緒內做 對帳 → 歸檔(到點)→ 保留清理 → 縮圖批次 → 每日稽核清理;
  縮圖尚有待辦回 True,迴圈立刻再跑不等間隔。狀態只存記憶體 `_state`(`status()` 讀),
  `request_scan()` 可自任何執行緒呼叫(`call_soon_threadsafe`)。
- **總覽 partial 契約**:`/` 接 `?partial=1&since=<token>` 只回 `_panel_videos.html`;token 由
  `videos._index_token()`(筆數 | 最大 id | 已產縮圖數)決定,相同回 204(標頭 X-Snapshot)。
  改動會影響卡片外觀的狀態要納入 token。整頁把 token 放 `#panel[data-snapshot]`,前端
  `rsapAutoRefresh()` 換入後呼叫 `rsapBindCards()` 重綁懶載入 / 懸停預覽。
  **日期預設最新一天**:`date` 參數未帶(`None`)→ `recordings.latest_date()`;帶空值 → 全部日期。
  因此 `base_qs` / `_safe_back()` 一律明列 `date`(含空值),分頁 / back 連結少了它會跳回最新一天。
  **篩選狀態帶著走**:卡片連到 `/watch/{id}?back=<總覽 query string>`(`back_qs`),觀看頁的上下一部 /
  相關錄影 / 刪除表單(hidden `back`)都沿用,刪除後 `_back_url()` 轉回原篩選 / 排序 / 頁碼。
  `back` 一律經 `videos._safe_back()` 以 `BACK_KEYS` 白名單重編碼,不收網址 / 路徑(防開放式轉向);
  新增會離開總覽的連結請同樣帶 `back`。
- **寫入操作三道關**在 `main.require_login` middleware:登入 → 角色(非 GET 需
  full_admin,`/logout` 例外)→ CSRF(`_csrf` 欄位或 `X-CSRF-Token` 標頭)。
  新增表單記得放 `<input type="hidden" name="_csrf" value="{{ csrf_token }}">`,AJAX 帶 `window.RSAP_CSRF`。
  角色由 middleware 每請求以 `auth.roles_for_session()`(執行緒內,10 秒 TTL)解析後放
  `request.state.roles`;分權變更後呼叫 `invalidate_roles_cache()`。本機 admin 以 session
  `local: True` 辨識,AD 帳號即使叫 admin 也走分權表。唯讀角色可瀏覽 / 播放 / 下載,不可刪除、掃描、改設定;
  `main.ADMIN_PREFIXES`(`/settings` `/logs` `/roles`)連 GET 都擋(403 + `access_denied` 稽核),
  `webutil.NAV` 第 4 欄 `admin_only` 讓側欄對唯讀隱藏。新增管理頁同時加這兩處。
- **本機 admin 密碼**:預設空;啟動時空且 AD 未啟用 → `main._bootstrap_local_admin()` 產生隨機初始密碼
  (密文入 config.json、明文寫 `data/initial_admin_password.txt`、標 `local_admin_initial`)。登入時
  `auth.local_admin_must_change()` 為真 → session `must_change_pw`,middleware 只放行 `/settings` 與 `/logout`。
  改密碼一律經 `auth.set_local_admin_password()`。不要用「拒絕啟動」處理弱密碼。
- **CSP script-src 無 'unsafe-inline'**:模板 `<script>` 一律 `<script nonce="{{ csp_nonce }}">`
  (`render()` 注入;login 頁自行從 `request.state.csp_nonce` 傳),禁止 `onclick=` / `onchange=` /
  `onsubmit=` 屬性與 `javascript:` URL,改用 base.html 的事件委派(`data-autosubmit`、`data-confirm`、id 綑綁)。
  `media-src 'self'`:影片 / 縮圖只能同源。
- **secret 落地一律密文**:config.json 的欄位列在 `config._ENC_FIELDS`(上傳 Token 也在);
  新增密碼類欄位務必登記,並在設定頁「留空 = 不變更」(勿把明碼 render 進 HTML)。
  上傳 Token 產生(`/settings/token`)只回傳一次明文,之後只顯示「已設定」。
- **沿用已存密碼的 AD 測試必須綁定目標**(`/settings/ad-test`):密碼欄留空時 Server 須在已儲存清單內,否則拒絕並記 `ad_test_blocked`。
- **回傳 FileResponse 的路由不得用 `Depends(get_db)`**:yield 相依的收尾在回應送完後才跑,一條串流會
  把 SQLite 連線佔住整段播放時間,100 人同時播放即耗盡連線池(QueuePool timeout,其他頁面卡住數十秒,
  2026-10-16 實測)。`/media` `/download` 一律 `with SessionLocal() as db:` 查完即關再回 FileResponse。
- **同步阻塞 I/O 一律丟執行緒**:只查 DB / 渲染模板的路由直接寫成同步 `def`(FastAPI 丟 threadpool);
  需要 `await` 的路由才用 `async def`,其中 ldap3、檔案寫入、SQLite 查表用 `asyncio.to_thread`。
  `audit()` 在 event loop 內自動丟 executor。
- **時間**:取「現在」一律 `config.local_now()`;檔案 mtime 轉本地一律 `config.from_timestamp()`;
  勿混用 `datetime.now()` / `datetime.fromtimestamp()`(系統時區)。
- **設定優先序**:環境變數 `RSAP_*` > config.json > .env;`start_rsap.cmd` 啟動時以同一 `settings` 取埠。
  `upload_folder` 留空 = `data/uploads`,一律經 `config.upload_dir()` 取得,不要自己拼路徑。
- **懸浮提示一律寫 `title`,由 base.html 的 `#vtip` 接管**:`&#10;` 分行、多行時首行粗體、空行渲染為分隔線、
  路徑 / 檔名等寬;partial 換入後呼叫 `window.rsapTipHide()`。不要另寫 tooltip 元件,也不要把 HTML 塞進 title。
- **前端衛生**:伺服器回傳文字進 DOM 一律 `textContent` / 文字節點,只有 `#panel` 的 partial
  (本站 Jinja 自動轉義輸出)可用 innerHTML;不得引用外部 CDN(CSP 也會擋)。懸停預覽 / 懶載入的
  `<video>` 離開時要 `removeAttribute('src'); load()` 釋放連線。

## 容器部署(Dockerfile / docker-compose.yml / preprod-verify.sh)

- builder 以 Nuitka 編譯(`--include-package=uvicorn` 因其動態 import;模板靠
  `--include-data-dir`),runtime 無原始碼。**兩 stage 的 Debian 版本必須一致**(glibc),
  builder 的 Python 版本跟隨開發 venv(目前 3.14),改版兩處一起動並重跑 preprod-verify.sh。
- 狀態全在單一 volume:host `/srv/rsap/data` ↔ 容器 `/opt/rsap/data`;
  config.json 是 dist 根的符號連結 → `data/config.json`,勿改回實體檔。
- **埠 host 與容器內同號、必填**:compose 以 `.env` 的 `RSAP_PORT` 同時設 ports 映射與 `RSAP_WEB_PORT`
  (正式 8077:8077;同機多實例規劃 8001–8099),以 `${RSAP_PORT:?}` 強制必填,未設即拒絕啟動——
  不給預設埠,因為無論容器內或 host 都可能撞埠。Dockerfile 不寫 EXPOSE、HEALTHCHECK 以 shell 形式讀
  `$RSAP_WEB_PORT`。新增實例 = 獨立 .env + `docker compose -p <名>`;勿把容器內埠寫回固定 5001。
- 改動 Dockerfile / compose 後的驗證一律跑 `preprod-verify.sh`(臨時埠 + mktemp,驗完自刪),
  它只驗「活著」,業務邏輯仍靠既有端到端 curl 測試。

## 相依與版本

- `requirements.txt` 只表達下限,部署一律 `pip install -r requirements.lock.txt`;
  升級流程:改下限 → 測試環境驗證 → `pip freeze` 重產 lock → commit。
- Starlette `FileResponse` 自 0.41 起支援 Range(206)、`GZipMiddleware` 預設排除 `video/*` / JPEG / 206;
  影片串流依賴此行為,升級後請重測 `curl -H "Range: bytes=0-99" /media/{id}` 回 206。
- 影片一律用 `videos.VideoFileResponse`(`chunk_size` 1 MB):FileResponse 每塊一次 `to_thread`,預設 64 KB
  在 20 條全速串流下 CPU 加倍、總覽頁延遲 12 秒(2026-10-16 實測);改 1 MB 後 2 秒。勿改回預設或縮小。

## 測試

- 端到端(不需 ffmpeg / AD):以環境變數指到暫存資料夾與測試埠啟動
  `RSAP_WEB_PORT=5099 RSAP_UPLOAD_FOLDER=<暫存夾> RSAP_UPLOAD_TOKEN=<≥16 字元> python run.py`,
  放幾個假 `.mp4`(任意位元組即可;無 ffmpeg 時不會解碼),以 curl 走:
  `/health` 200 → `/` 303 登入 → admin 初始密碼登入 → 強制改密碼(無 CSRF 403、有 CSRF 303)→
  `/` 卡片數 → `?partial=1&since=<token>` 204 → `/watch/{id}` 200 → `/media/{id}` Range 206 →
  `/download/{id}` attachment → 上傳:無 Token 401、檔名含路徑 400、正常 200、重送 200 exists、
  同名不同內容 409 → 刪除無 CSRF 403、有 CSRF 303 → `/logs` 有 upload / delete 紀錄。
  測試完刪除 `data/rsap.db`、`data/secret.key`、`config.json`(測試產生的初始密碼與金鑰不要留到正式環境)。
- 檔名解析:`recordings.parse_filename()` 對 `PC01_jsmith_20260101093000.mp4`、`PC02_mary.lee_...`、
  `HOST_a_b_c_20260101093000.mp4`(帳號含底線)、`random.mp4`(解析不到 → 空)各驗一次。

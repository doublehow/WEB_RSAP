#!/usr/bin/env bash
# RSAP 預備驗證(在 Ubuntu 專案目錄執行):build → 起容器 → 煙霧測試 → 自動清除。
# 專攔 Win11 UAT 看不到的問題:Nuitka 漏包(動態 import、資料檔)與 Windows→Linux 差異。
# 只驗「活著」(health / 轉向 / 登入頁),不驗業務邏輯。失敗印容器 log 並以非零退出。
#
# 用法:./preprod-verify.sh [port](預設 5099;臨時資料目錄 mktemp,驗完連同容器一併刪除)
set -euo pipefail

export RSAP_PORT="${1:-5099}"
export RSAP_DATA="$(mktemp -d)"
PROJ=rsap-verify
BASE="http://127.0.0.1:${RSAP_PORT}"

cleanup() {
  docker compose -p "$PROJ" down --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$RSAP_DATA"
}
trap cleanup EXIT

fail() {
  echo "FAIL:$1"
  docker compose -p "$PROJ" logs --tail 100 || true
  exit 1
}

echo "== docker compose build =="
docker compose -p "$PROJ" build

echo "== 起容器(port ${RSAP_PORT},資料 ${RSAP_DATA})=="
docker compose -p "$PROJ" up -d

echo "== 等待 /health(最多 60 秒)=="
ok=""
for _ in $(seq 1 30); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "/health 60 秒內未就緒"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")
[ "$code" = "303" ] || fail "/ 未登入應 303 轉向登入,得到 $code"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/login")
[ "$code" = "200" ] || fail "/login 應 200,得到 $code"

[ -f "$RSAP_DATA/initial_admin_password.txt" ] || fail "初始 admin 密碼檔未產生(data volume 寫入異常)"

echo "PASS:/health 200、/ 303、/login 200、初始密碼檔已落地"

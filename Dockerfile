# syntax=docker/dockerfile:1
# RSAP 容器建置:兩階段——builder 以 Nuitka 把 Python 編成原生二進位,
# runtime 只帶編譯產物(無 .py、無直譯器)。必須在 Linux x86_64 上 build(Nuitka 不跨 OS)。
#
#   docker compose build          # 或 ./preprod-verify.sh 一併驗證
#
# 路徑約定:Nuitka standalone 下 app/config.py 的 __file__ 指向 dist 內,
# BASE_DIR = /opt/rsap,故 data/(DB、金鑰、上傳、縮圖)與 config.json 都在 /opt/rsap 下;
# config.json 以符號連結導進 data/,讓單一 volume(/opt/rsap/data)承載全部狀態。

# ---------------------------------------------------------------- builder
# 兩個 stage 的 Debian 版本必須一致(glibc 相容):builder 釘 trixie(Debian 13),
# runtime 用 debian:13-slim。若改版請兩邊一起改,否則 run.bin 會 GLIBC not found。
FROM python:3.14-slim-trixie AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev patchelf ccache \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY requirements.lock.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.lock.txt "nuitka<5"

COPY run.py ./
COPY app ./app

# --include-package=uvicorn:uvicorn 以字串動態載入 loop / protocol 實作,須整包納入
# --include-data-dir:Jinja2 模板是資料檔,Nuitka 不會自動打包
RUN --mount=type=cache,target=/root/.cache/Nuitka \
    --mount=type=cache,target=/root/.cache/ccache \
    python -m nuitka --standalone \
      --include-package=app \
      --include-package=uvicorn \
      --include-data-dir=app/web/templates=app/web/templates \
      --assume-yes-for-downloads \
      run.py

# ---------------------------------------------------------------- runtime
FROM debian:13-slim

# ffmpeg:縮圖(選配但建議);tzdata:zoneinfo 時區資料;curl:HEALTHCHECK 用
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home rsap

ENV TZ=Asia/Taipei

COPY --from=builder --chown=rsap:rsap /src/run.dist /opt/rsap
RUN mkdir -p /opt/rsap/data \
 && chown rsap:rsap /opt/rsap/data \
 && ln -s data/config.json /opt/rsap/config.json

USER rsap
WORKDIR /opt/rsap
# 容器內埠 = 環境變數 RSAP_WEB_PORT(compose 設成與 host 對外埠同號,好記;預設 5001),
# 故不寫死 EXPOSE;HEALTHCHECK 以 shell 形式讀同一變數。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS "http://127.0.0.1:${RSAP_WEB_PORT:-5001}/health"

ENTRYPOINT ["/opt/rsap/run.bin"]

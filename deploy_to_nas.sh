#!/usr/bin/env bash
# 一键通过 SSH 将 EdsonStock 超短游资实盘信号系统部署到 NAS (192.168.5.115) 的 Docker 上
set -e

NAS_USER="${NAS_USER:-admin}"
NAS_HOST="${NAS_HOST:-192.168.5.115}"
NAS_DIR="${NAS_DIR:-/volume1/docker/edsonstock-ultrashort}"
PORT="${PORT:-8899}"

echo "=== [1/3] 正在通过 SSH 在 NAS (${NAS_USER}@${NAS_HOST}) 上创建部署目录: ${NAS_DIR} ==="
ssh "${NAS_USER}@${NAS_HOST}" "mkdir -p ${NAS_DIR}/cache"

echo "=== [2/3] 正在同步项目代码、Dockerfile 与实盘网页到 NAS ==="
scp -r Dockerfile docker-compose.yml SKILL.md scripts references web "${NAS_USER}@${NAS_HOST}:${NAS_DIR}/"

echo "=== [3/3] 正在 NAS 上构建并启动 Docker 容器 (端口: ${PORT}) ==="
ssh "${NAS_USER}@${NAS_HOST}" "cd ${NAS_DIR} && \
  if command -v docker-compose >/dev/null 2>&1; then \
    sudo docker-compose up -d --build; \
  elif docker compose version >/dev/null 2>&1; then \
    sudo docker compose up -d --build; \
  else \
    sudo docker build -t edsonstock-ultrashort:latest . && \
    sudo docker rm -f edsonstock-ultrashort 2>/dev/null || true && \
    sudo docker run -d --name edsonstock-ultrashort --restart unless-stopped -p ${PORT}:8899 -v ${NAS_DIR}/cache:/app/cache edsonstock-ultrashort:latest; \
  fi && \
  sudo docker ps --filter name=edsonstock-ultrashort"

echo ""
echo "✅ 部署完成！"
echo "👉 超短实盘信号工作台地址 : http://${NAS_HOST}:${PORT}/"
echo "👉 腾讯五档与放量实时 API : http://${NAS_HOST}:${PORT}/api/quotes?codes=002771,002579,000823"
echo "👉 东财涨跌停/炸板实时三池: http://${NAS_HOST}:${PORT}/api/zt_pool"

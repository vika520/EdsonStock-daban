#!/usr/bin/env bash
# 通过 SSH 将 EdsonStock 超短游资实盘信号系统部署到 NAS Docker。
# 连接参数从 ops/.env 读取（该文件不会提交），也支持直接传环境变量。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

NAS_HOST="${NAS_HOST:-}"
NAS_USER="${NAS_USER:-}"
NAS_DIR="${NAS_DIR:-/volume1/docker/edsonstock-ultrashort}"
PORT="${PORT:-8899}"

if [ -z "$NAS_HOST" ] || [ -z "$NAS_USER" ]; then
  echo "错误：请先复制 ops/.env.example 为 ops/.env，并填写 NAS_HOST 和 NAS_USER。" >&2
  echo "也可以用环境变量传入：NAS_HOST=... NAS_USER=... bash ops/deploy_to_nas.sh" >&2
  exit 1
fi

SSH_TARGET="${NAS_USER}@${NAS_HOST}"

echo "=== [1/3] 在 ${SSH_TARGET} 上创建部署目录 ${NAS_DIR}/cache ==="
ssh "$SSH_TARGET" "mkdir -p -- '${NAS_DIR}/cache'"

echo "=== [2/3] 同步项目代码与配置到 NAS ==="
scp -r \
  "${REPO_ROOT}/Dockerfile" \
  "${REPO_ROOT}/docker-compose.yml" \
  "${REPO_ROOT}/SKILL.md" \
  "${REPO_ROOT}/scripts" \
  "${REPO_ROOT}/references" \
  "${REPO_ROOT}/web" \
  "${SSH_TARGET}:${NAS_DIR}/"

echo "=== [3/3] 构建并启动 Docker 容器（端口 ${PORT}） ==="
ssh "$SSH_TARGET" "cd -- '${NAS_DIR}' && \
  if command -v docker-compose >/dev/null 2>&1; then \
    sudo docker-compose up -d --build; \
  elif docker compose version >/dev/null 2>&1; then \
    sudo docker compose up -d --build; \
  else \
    sudo docker build -t edsonstock-ultrashort:latest . && \
    { sudo docker rm -f edsonstock-ultrashort 2>/dev/null || true; } && \
    sudo docker run -d --name edsonstock-ultrashort --restart unless-stopped -p '${PORT}:8899' -v '${NAS_DIR}/cache:/app/cache' edsonstock-ultrashort:latest; \
  fi && \
  sudo docker ps --filter name=edsonstock-ultrashort"

echo ""
echo "部署完成：http://${NAS_HOST}:${PORT}/"

FROM python:3.11-slim

LABEL maintainer="vika520 <https://github.com/vika520/edsonstock-data>"
LABEL description="EdsonStock A-Share Ultra-Short Live Signal & Five-Level Order Book Terminal"

# 安装 curl（东财 push2ex 防封必需）与 tzdata（确保中国标准时间 Asia/Shanghai 9:15-15:00 对齐）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/ /app/scripts/
COPY references/ /app/references/
COPY web/ /app/web/
COPY SKILL.md /app/SKILL.md

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV EDSONSTOCK_CACHE=/app/cache
ENV EDSON_WEBHOOK_URL=""

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8899/health || exit 1

CMD ["python3", "scripts/live_signal_server.py", "--host", "0.0.0.0", "--port", "8899"]

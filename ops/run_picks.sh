#!/bin/bash
# EdsonStock-daban NAS 调度脚本：替代 GitHub Actions cron（云端调度会丢/延迟）
# 用法: run_picks.sh <top10|top10_auction|swing>
#
# 本脚本随仓库版本管理（NAS 每次执行 pull 后自动用最新版）。
# 2026-11-27 加固：NAS 到 GitHub 的 TLS 连接经常被中断（GnuTLS -110），首次运行时
# pull/push 各只试一次，结果「调度准时、选股成功、但推送失败，线上仍是昨日数据」。
# 现在 pull / push 都带重试，push 被拒（远端有新提交）时自动 rebase 后再推。
set -u
MODE="${1:-}"
REPO=/vol2/1000/admin_data/workspace/EdsonStock-daban
LOG=/tmp/esd_picks.log
cd "$REPO" || exit 1
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
# NAS 环境有残留代理变量会干扰 GitHub；HTTP/1.1 比 HTTP/2 在这条线路上更稳
G=(git -c http.proxy= -c https.proxy= -c http.version=HTTP/1.1 -c user.name=vika520 -c user.email=vika520@users.noreply.github.com)
ts() { date "+%F %T"; }
log() { echo "[$(ts)] $*" >> "$LOG"; }
log "=== run_picks ${MODE:-?} start ==="

# ---------- 1) 更新代码（带重试；最终失败则用本地代码继续）----------
pulled=0
for i in 1 2 3; do
  if "${G[@]}" pull --ff-only origin main >> "$LOG" 2>&1; then pulled=1; break; fi
  log "git pull 第 $i 次失败，4 秒后重试"; sleep 4
done
[ "$pulled" = 1 ] || log "WARN: git pull 三次均失败，用本地代码继续（本次推送前会再 rebase）"

# 脚本若有更新，用新版本重新执行本模式（避免一直跑旧脚本）
NEWEST=$("${G[@]}" show HEAD:ops/run_picks.sh 2>/dev/null | md5sum | cut -c1-8)
SELF=$(md5sum "$0" | cut -c1-8)
if [ -n "$NEWEST" ] && [ "$NEWEST" != "$SELF" ] && [ "${ESD_REEXEC:-0}" != "1" ]; then
  log "run_picks.sh 有新版本，切换到新版重新执行"
  export ESD_REEXEC=1
  exec bash ops/run_picks.sh "$MODE"
fi

# ---------- 2) 跑选股 ----------
OUT=web/daily_picks.json
case "$MODE" in
  top10)          python3 scripts/auto_pick_top10.py --mode post --out "$OUT" >> "$LOG" 2>&1 ;;
  top10_auction)  python3 scripts/auto_pick_top10.py --mode auction --out "$OUT" >> "$LOG" 2>&1 ;;
  swing)          OUT=web/swing_picks.json; python3 scripts/auto_pick_swing.py --out "$OUT" >> "$LOG" 2>&1 ;;
  *) log "unknown mode '$MODE'"; exit 1 ;;
esac

# ---------- 3) 校验产物 ----------
# 超短：picks 为空说明接口异常，不推送（保留线上旧数据）。
# 波段：防守期 0 推荐是正常状态，只要求 regime.score 是 0~100 的数字。
VALID=$(python3 - "$OUT" "$MODE" <<'PY'
import json, sys
path, mode = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path, encoding="utf-8"))
    if mode == "swing":
        s = (d.get("regime") or {}).get("score")
        ok = isinstance(s, (int, float)) and 0 <= s <= 100
    else:
        ok = (d.get("ok") is not False) and len(d.get("picks") or []) >= 1
    print("1" if ok else "0")
except Exception:
    print("0")
PY
)
if [ "$VALID" != "1" ]; then
  log "WARN: 产物校验未通过（$OUT），不推送，保留线上旧数据"
  "${G[@]}" checkout -- "$OUT" 2>/dev/null
  log "=== run_picks $MODE done（未推送）==="
  exit 0
fi

# ---------- 4) 提交并推送（带重试 + 被拒自动 rebase）----------
if "${G[@]}" diff --quiet -- "$OUT" 2>/dev/null; then
  log "数据无变化，跳过推送"
  log "=== run_picks $MODE done ==="
  exit 0
fi
"${G[@]}" add "$OUT"
"${G[@]}" commit -q -m "chore(data): NAS 调度自动更新 ($MODE / $(date +%Y%m%d))" >> "$LOG" 2>&1

pushed=0
for i in 1 2 3 4 5; do
  out=$("${G[@]}" push origin main 2>&1); rc=$?
  if [ $rc -eq 0 ]; then pushed=1; break; fi
  if echo "$out" | grep -qiE 'non-fast-forward|fetch first|behind'; then
    # 远端有别的提交（多半是本机推的前端改动）：rebase 后重推。
    # 数据文件与前端文件互不相干，冲突极罕见；真冲突则放弃本次，保住线上数据。
    log "push 被拒（远端有新提交），rebase 后重试"
    if "${G[@]}" pull --rebase -q origin main >> "$LOG" 2>&1; then continue; fi
    log "ERROR: rebase 冲突，中止并还原"; "${G[@]}" rebase --abort 2>/dev/null; break
  fi
  log "push 第 $i 次失败（网络），6 秒后重试：$(echo "$out" | grep -iE 'fatal|error' | head -1 | cut -c1-90)"
  sleep 6
done

if [ "$pushed" = 1 ]; then
  log "pushed OK: $("${G[@]}" log --oneline -1)"
else
  # 推送始终失败：提交留在本地，下次运行会连同新结果一起推上去
  log "ERROR: push 五次均失败，提交保留在本地等待下次运行补推"
fi
log "=== run_picks $MODE done ==="

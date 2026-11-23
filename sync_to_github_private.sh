#!/usr/bin/env bash
# 一键将最新代码推送到 GitHub (vika520/edsonstock-data) 并将仓库设为 Private（仅自己可见）
set -e

REPO_OWNER="${REPO_OWNER:-vika520}"
REPO_NAME="${REPO_NAME:-EdsonStock-daban}"
BRANCH="${BRANCH:-main}"

echo "=== [1/3] 正在创建/设置 GitHub 私有仓库 ${REPO_OWNER}/${REPO_NAME}（Private，仅自己可见） ==="
if [ -n "${GITHUB_TOKEN}" ]; then
  HTTP_CODE=$(curl -s -o /tmp/gh_priv_resp.json -w "%{http_code}" -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/user/repos" \
    -d "{\"name\":\"${REPO_NAME}\",\"private\":true,\"description\":\"EdsonStock A股超短游资实时信号与脉冲推送工作台\"}")
  if [ "${HTTP_CODE}" != "201" ]; then
    HTTP_CODE=$(curl -s -o /tmp/gh_priv_resp.json -w "%{http_code}" -X PATCH \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}" \
      -d '{"private":true}')
  fi
  if [ "${HTTP_CODE}" = "200" ]; then
    echo "✅ 仓库 ${REPO_OWNER}/${REPO_NAME} 已成功设置为 Private（私有，仅你本人可见）！"
  else
    echo "⚠️ API 返回状态码 ${HTTP_CODE}，详情："
    cat /tmp/gh_priv_resp.json
  fi
elif command -v gh >/dev/null 2>&1; then
  gh repo create "${REPO_OWNER}/${REPO_NAME}" --private 2>/dev/null || gh repo edit "${REPO_OWNER}/${REPO_NAME}" --visibility private --accept-visibility-change-consequences
  echo "✅ 已通过 GitHub CLI (gh) 将仓库 ${REPO_OWNER}/${REPO_NAME} 设置为 Private！"
else
  echo "⚠️ 未检测到 GITHUB_TOKEN 环境变量或 gh CLI，请先执行 export GITHUB_TOKEN='你的Token' 或 gh auth login。"
  exit 1
fi

echo "=== [2/3] 正在初始化/同步本地 Git 仓库并提交全套升级文件 ==="
if [ ! -d ".git" ]; then
  git init
  git branch -M "${BRANCH}"
fi

if [ -n "${GITHUB_TOKEN}" ]; then
  REMOTE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO_OWNER}/${REPO_NAME}.git"
else
  REMOTE_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${REMOTE_URL}"
else
  git remote add origin "${REMOTE_URL}"
fi

git fetch origin "${BRANCH}" || true
git add -A
git commit -m "feat(ultrashort): 升级四层量化架构、每日Top10自动选股引擎、腾讯L1实盘工作台与NAS Docker部署" || echo "无新增变更需要提交"

echo "=== [3/3] 正在推送最新代码至 GitHub (${REPO_OWNER}/${REPO_NAME} -> ${BRANCH}) ==="
git push -u origin "${BRANCH}" --force-with-lease || git push -u origin "${BRANCH}" -f

# 推送完成后抹除 remote URL 中的明文 Token，防止本地泄露
git remote set-url origin "https://github.com/${REPO_OWNER}/${REPO_NAME}.git"

echo ""
echo "🎉 全部完成！最新代码已同步至 https://github.com/${REPO_OWNER}/${REPO_NAME} 且已锁定为 Private（仅你本人可见）。"

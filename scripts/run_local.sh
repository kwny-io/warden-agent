#!/usr/bin/env bash
# 本地/演练启动：起一个**只监听回环**的控制台服务（后端 + 已构建的前端产物）。
#
# 为什么需要它（而不是让人背一长串 env）：`docs/operations.md` 的日志/可观测一节
# 一直写着 `WARDEN_LOG_FORMAT=text ./scripts/run_local.sh`，但这个脚本**曾经并不存在**
# ——文档把人指到一个不存在的文件，属于"文档比代码先写"的那种债。现在把它补上，
# 并且保持它足够小：**只做本地默认值，不做编排**（编排在 docker-compose / k8s 里）。
#
# 用法：
#   scripts/run_local.sh                     # 127.0.0.1:8000，人眼可读日志，无密钥→用假模型
#   PORT=8123 scripts/run_local.sh           # 换端口（8000 常被别的项目占用）
#   WARDEN_API_KEY=sk-xxx scripts/run_local.sh   # 走真模型（deepseek）
#   scripts/run_local.sh --help              # 透传给服务本身
#
# 几条刻意的默认值：
#   · WARDEN_HOST 默认 127.0.0.1：**回环地址不算对外**，所以没有密钥也能起；
#     设成 0.0.0.0 而没配 WARDEN_API_KEY 时服务会拒绝启动（这是服务自己的判断，这里不绕过）。
#   · WARDEN_LOG_FORMAT=text：本地人眼看就显式选 text（json 是给采集端看的）。
#   · 前端：后端会自动优先返回 web/dist 构建产物；没构建过就回退到内置静态页。
#     要跑 React 控制台请先 `cd web && npm run build`（或 npm run dev 另起 vite）。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo"

# Windows 上用 venv 里的解释器；其它平台优先 venv 里可执行的 python。
py="$repo/.venv/Scripts/python.exe"
[ -x "$py" ] || py="$repo/.venv/bin/python"
[ -x "$py" ] || py="$(command -v python3 || command -v python)"

export WARDEN_HOST="${WARDEN_HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"
export WARDEN_LOG_FORMAT="${WARDEN_LOG_FORMAT:-text}"

echo "[run_local] 启动：$py -m warden_agent.web.run_server  (host=$WARDEN_HOST port=$PORT log=$WARDEN_LOG_FORMAT)"
if [ -z "${WARDEN_API_KEY:-}" ]; then
  echo "[run_local] 未设 WARDEN_API_KEY → 使用内置假模型（离线可跑；要真模型请设 WARDEN_API_KEY）"
fi
if [ -d "$repo/web/dist" ]; then
  echo "[run_local] 使用前端构建产物 web/dist（要更新请 cd web && npm run build）"
else
  echo "[run_local] 未发现 web/dist → 回退到内置静态页（React 控制台需先 build）"
fi

exec "$py" -m warden_agent.web.run_server "$@"

#!/usr/bin/env bash
# npm ci 的加固版：CI 不该被某个第三方镜像的一次 504 判死。
#
# 真实事故（第九十九批，run 39175595595）：`ts/mcp-client/package-lock.json` 里 107 个依赖的
# `resolved` 全部指向第三方镜像 `registry.npmmirror.com`，那个镜像回了
# `npm error 504 Gateway Time-out - GET .../zod-to-json-schema-3.25.2.tgz`
# ⇒ quality job 在"安装 ts 客户端依赖"这步就死了，连 pytest 都没跑起来
# （所以 junit 也没产出，下游"跳过台账"判据拿不到真数据）。
#
# 加固分两层，都不碰 lockfile：
#   1. **重试**（指数退避 + 加大 fetch 超时）：504/502 这类网关抖动重试就能过；
#   2. **换源重试**：重试用尽后，用官方源再跑一次，并加 `--replace-registry-host=always`，
#      让 npm 把 lockfile 里 `resolved` 的主机名整体改写成配置的 registry
#      —— 内容完整性仍由 lockfile 里的 `integrity`（sha512）保证，换源不降低校验强度。
#
# 用法：在任一工作目录 `bash <repo>/scripts/npm_ci.sh`。退出码透传 npm 的。
set -u

retries="${NPM_CI_RETRIES:-3}"
flags=(
  --no-audit --no-fund
  --fetch-retries=5
  --fetch-retry-mintimeout=20000
  --fetch-retry-maxtimeout=120000
  --fetch-timeout=600000
)

if [ ! -f package.json ]; then
  echo "npm_ci.sh：当前目录没有 package.json（cwd=$(pwd)）—— 别在仓根跑它" >&2
  exit 2
fi

attempt=0
while [ "$attempt" -lt "$retries" ]; do
  attempt=$((attempt + 1))
  echo "npm ci（第 $attempt/$retries 次，源=当前配置）"
  if npm ci "${flags[@]}"; then
    exit 0
  fi
  echo "npm ci 第 $attempt 次失败（多半是镜像/网络）—— 退避 $((attempt * 10))s 后重试" >&2
  sleep $((attempt * 10))
done

echo "镜像重试用尽 ⇒ 换官方源重试（--replace-registry-host=always）" >&2
npm ci "${flags[@]}" --registry=https://registry.npmjs.org/ --replace-registry-host=always

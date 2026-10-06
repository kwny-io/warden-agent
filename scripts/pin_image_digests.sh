#!/usr/bin/env bash
# 把镜像引用从可变 tag 钉成不可变 digest（`repo:tag@sha256:<digest>`）。
#
# 为什么这件事值得单独一个脚本（而不是靠人手写死 digest）：
#   1) **digest 手写必错**。Dockerfile 里留 `@sha256:000…0` 这类占位符能过
#      kubeconform / 语法校验，却让每一次 `docker build`、每一次 Pod 调度直接
#      `manifest unknown` —— 风险从「构建期立刻暴露」被推到「上线期才暴露」。
#      所以仓库里**不留占位 digest**，钉 digest 变成发布流水线里的一次显式动作。
#   2) **一次解析、多处回写**。基础镜像（Dockerfile 的 FROM）与应用镜像
#      （deploy/k8s/*.yaml 的 `image:`）必须钉在**同一批** digest 上，
#      否则「签名的是 A、部署的是 B」。手工同步两处必然漂移。
#
# 用法：
#   scripts/pin_image_digests.sh --help
#   scripts/pin_image_digests.sh                                  # 只钉 Dockerfile 的 FROM
#   scripts/pin_image_digests.sh --also postgres:16-alpine        # 顺带钉备份用的基础镜像
#   scripts/pin_image_digests.sh --app-image ghcr.io/acme/warden-agent:0.1.0
#                                                                 # 顺带回写 deploy/k8s/*.yaml
#   scripts/pin_image_digests.sh --dry-run ...                    # 只打印将要做的改动
#
# 设计取舍——**fail-closed**：
#   解析不出 digest（没有 buildx / registry 不可达 / ref 不存在）时**非 0 退出**并给出
#   可执行的下一步，绝不「跳过这一处、继续往下做」。因为「以为钉过了、其实没钉」比
#   「没钉」危险得多：前者会让发布流水线一路绿灯，直到 Pod 起不来。
#
# 幂等：重复运行结果一致（正则会把已有的 `@sha256:<64hex>` 一起吃掉再写回）。
#   ⚠️ 例外：`--app-image` 在「清单里已经没有占位符可回写」时**非 0 退出**（退出码 4），
#   因为它那时的产出是空 diff（见 pin_app_image 的后置条件）——那正是「以为钉过了、
#   其实没钉」的形状，静默成功会让发布绿灯而清单里仍是占位符。
# 原子：先写到同目录临时文件、再 `mv` 覆盖，避免「改到一半断电」留下半个 Dockerfile。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/Dockerfile"
K8S_DIR="${REPO_ROOT}/deploy/k8s"
# 单个 ref 的解析超时（秒）。registry 不可达时不要无限挂着——CI 会等到天荒地老。
INSPECT_TIMEOUT="${INSPECT_TIMEOUT:-60}"

DRY_RUN=0
APP_IMAGE=""
declare -a ALSO_IMAGES=()

usage() {
  cat <<'EOF'
pin_image_digests.sh —— 把镜像引用钉成不可变 digest（repo:tag@sha256:<digest>）

用法:
  scripts/pin_image_digests.sh [选项]

默认行为（无参数）:
  解析 Dockerfile 里每个 `FROM` 的基础镜像 tag，把 digest 写回 Dockerfile。
  例：FROM node:20-slim AS web-build  →  FROM node:20-slim@sha256:<digest> AS web-build

选项:
  --also <ref>        额外钉一个镜像引用（可重复）。用于不在 Dockerfile、但清单里
                      引用到的第三方镜像，例如 --also postgres:16-alpine。
                      会在 Dockerfile 与 deploy/**/*.yaml 里把所有该 ref 出现处钉上
                      digest（幂等）。
  --app-image <ref>   已推送到 registry 的**应用镜像**引用（如
                      ghcr.io/acme/warden-agent:0.1.0）。解析其 digest 后，把
                      deploy/k8s/*.yaml 里所有仍写着 `REPLACE_ME/...` 占位符的
                      `image:` 值整体替换成 `<ref>@<digest>`。
                      （为什么不按字符串替换：占位符仓库名与真实仓库名不同，
                      只有「整体替换占位值」才既明确又不会误伤。）
  --dockerfile <path> Dockerfile 路径（默认仓库根目录的 Dockerfile）。
  --dry-run           只打印将要做的改动，不写文件。
  -h, --help          显示本帮助。

退出码:
  0  全部成功（--dry-run 时表示「解析成功且会这样改」）
  1  用法错误
  2  环境不满足（找不到 docker / buildx 不可用）——按提示先装上
  3  解析 digest 失败（registry 不可达 / ref 不存在）——**不会**留下半成品文件
  4  与 --app-image 同用时：清单里**没有可回写的占位符**，本步什么都没做
     （产出空 diff = 「以为钉过了、其实没钉」，见 pin_app_image() 里的后置条件）
EOF
}

log()  { printf '%s\n' "$*"; }
warn() { printf '警告: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

# ---- 参数解析 ----------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --also)
      [[ $# -ge 2 ]] || die "--also 需要一个镜像引用" 1
      ALSO_IMAGES+=("$2"); shift 2 ;;
    --app-image)
      [[ $# -ge 2 ]] || die "--app-image 需要一个镜像引用" 1
      APP_IMAGE="$2"; shift 2 ;;
    --dockerfile)
      [[ $# -ge 2 ]] || die "--dockerfile 需要一个路径" 1
      DOCKERFILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "未知参数: $1" 1 ;;
  esac
done

[[ -f "$DOCKERFILE" ]] || die "Dockerfile 不存在: $DOCKERFILE" 1
[[ -d "$K8S_DIR" ]]    || die "目录不存在: $K8S_DIR" 1

# ---- 前置检查（fail-closed：宁可现在报错，也不静默少钉一处）---------------------
command -v docker >/dev/null 2>&1 \
  || die "找不到 docker CLI。装好 Docker 后重试；若本机就是没有 Docker，请在发布流水线的构建机上跑本脚本。" 2

if ! docker buildx version >/dev/null 2>&1; then
  die "docker buildx 不可用（docker buildx version 失败）。
      它属于 docker CLI 的 buildx 插件，本脚本靠它解析 registry 上的 manifest digest。
      补救：升级 docker 到含 buildx 的版本，或单独安装 buildx 插件（见 https://github.com/docker/buildx#installing）。" 2
fi

# timeout 在 macOS 上是 gtimeout；两个都没有就直接跑（退化为「可能挂住」）。
# 必须验一下 --version 能不能过：Git Bash / MSYS 里的 `timeout` 会解析到 Windows 自带的
# timeout.exe（那是「等待 N 秒」的另一个程序，参数是 /t，不是 60s），
# 拿它包 docker 会立刻报参数错——本机跑得通、别人机上报错的经典坑。
# （注：本段注释曾被写成换行后无 `#`，于是 bash 把 `timeout.exe**` 当命令执行——
#   在 shell 里写解释「解析陷阱」的注释，本身就是个陷阱。）
TIMEOUT_BIN=""
if timeout --version >/dev/null 2>&1; then TIMEOUT_BIN="timeout";
elif gtimeout --version >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout"; fi

# ---- 解析 digest -------------------------------------------------------------
resolve_digest() {  # $1 = repo:tag（已去掉可能存在的 @sha256:...）
  local ref="$1" out rc=0
  if [[ -n "$TIMEOUT_BIN" ]]; then
    out="$("$TIMEOUT_BIN" "$INSPECT_TIMEOUT" docker buildx imagetools inspect "$ref" \
             --format '{{.Manifest.Digest}}' 2>&1)" || rc=$?
  else
    out="$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}' 2>&1)" || rc=$?
  fi
  if [[ $rc -ne 0 ]]; then
    printf 'ERROR: 无法解析 %s 的 digest（%s 退出码 %s）:\n%s\n' "$ref" \
      "${TIMEOUT_BIN:-docker buildx imagetools inspect}" "$rc" "$out" >&2
    cat >&2 <<EOF
补救方向（按可能性排序）：
  1) registry 不可达 —— 本机到 registry 出网被挡（常见于内网/离线环境）：
       在能出网的构建机上跑本脚本，或为本机配好 HTTP(S)_PROXY / registry mirror。
  2) ref 不存在或拼错 —— 用 \`docker buildx imagetools inspect ${ref}\` 手工确认 tag。
  3) 需要登录私仓 —— 先 \`docker login <registry>\`（CI 里用 secrets 注入）。
本脚本不会跳过这一处：少钉一个 digest 就等于「以为钉过了、其实没钉」。
EOF
    return 3
  fi
  # 只接受 \`sha256:<64 位小写十六进制>\`，避免把警告文本当 digest 写进文件。
  if [[ ! "$out" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    die "解析 ${ref} 得到的不是合法 digest，拒绝写文件: $out" 3
  fi
  printf '%s' "$out"
}

# ---- 原子改写 ----------------------------------------------------------------
# 统一用这个 helper 做「读原文 → sed 改写 → 原子替换」，避免两处各写一遍。
# $1 = 文件；其余 = sed 表达式（-E 扩展正则）
rewrite_file() {
  local file="$1"; shift
  local tmp
  tmp="$(mktemp "${file}.pidXXXXXX")"
  cp -p "$file" "$tmp"          # -p：保留原权限（mktemp 默认 600，别把 Dockerfile 改成 600）
  if sed -E "$@" "$file" > "$tmp"; then
    if cmp -s "$file" "$tmp"; then
      rm -f "$tmp"; log "  未变化: ${file#"$REPO_ROOT"/}"; return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
      rm -f "$tmp"
      log "  [dry-run] 将改写: ${file#"$REPO_ROOT"/}"
      diff -u "$file" <(sed -E "$@" "$file") | sed -n '3,$p' | sed 's/^/    /' || true
    else
      mv "$tmp" "$file"          # 同目录 mv = 原子替换
      log "  已改写: ${file#"$REPO_ROOT"/}"
    fi
  else
    rm -f "$tmp"; die "sed 改写失败: $file" 1
  fi
}

# 把一个具体 tag 出现处钉上 digest（幂等：已存在的 @sha256:<64hex> 会被一起吃掉再写回）
pin_ref_in_file() {  # $1 = 文件；$2 = repo:tag（不含 digest）；$3 = digest
  local file="$1" ref="$2" digest="$3" ref_re
  ref_re="$(printf '%s' "$ref" | sed -E 's/[][\.^$*+?(){}|\/]/\\&/g')"
  rewrite_file "$file" "s|(${ref_re})(@sha256:[0-9a-f]{64})?|\1@${digest}|g"
}

# ---- 默认动作：钉 Dockerfile 里的每个 FROM ------------------------------------
# 为什么从 FROM 自动发现而不是写死镜像名：写死的话，将来有人换基础镜像（或加一个
# 构建阶段）就会漏钉一处，而且不会有任何提示。
pin_dockerfile_froms() {
  local -a refs=()
  local line ref
  while IFS= read -r line; do
    # 形如：FROM node:20-slim AS web-build  /  FROM --platform=linux/amd64 python:3.12-slim
    # 用 awk 而不是 sed 来剥「FROM」：GNU sed 的 `s///I` 大小写不敏感标志在 BSD/macOS sed
    # 上不存在。awk 的 toupper() 在哪都一样，少一个「本机跑得通、同事机上不行」的坑。
    ref="$(printf '%s' "$line" | awk '
      {
        if (toupper($1) != "FROM") next
        for (i = 2; i <= NF; i++) {
          if ($i ~ /^--/) continue                 # 跳过 --platform=... 之类的构建参数
          t = $i
          sub(/@sha256:[0-9a-f]{64}$/, "", t)     # 已钉过就取回裸 ref（幂等）
          print t
          exit
        }
      }')"
    [[ -n "$ref" ]] && refs+=("$ref")
  done < <(grep -Ei '^[[:space:]]*FROM[[:space:]]' "$DOCKERFILE" || true)

  if [[ ${#refs[@]} -eq 0 ]]; then
    warn "在 ${DOCKERFILE#"$REPO_ROOT"/} 里没找到任何 FROM 行——什么都没有钉。"
    return 0
  fi

  log "== 钉 Dockerfile 基础镜像（${DOCKERFILE#"$REPO_ROOT"/}）=="
  local r d
  for r in "${refs[@]}"; do
    log "-> 解析 ${r}"
    d="$(resolve_digest "$r")" || exit 3
    log "   digest ${d}"
    pin_ref_in_file "$DOCKERFILE" "$r" "$d"
  done
}

# ---- --also：钉额外镜像（Dockerfile + deploy 下的清单）--------------------------
pin_also_images() {
  local -a files=()
  local r d f
  # set -u 下取空数组元素会报错，先判长度（bash < 4.4 的行为）
  [[ ${#ALSO_IMAGES[@]} -eq 0 ]] && return 0

  # 不用 mapfile：macOS 自带 bash 3.2 没有它，而这个脚本是要在别人机器上跑的
  files+=("$DOCKERFILE")
  while IFS= read -r f; do files+=("$f"); done \
    < <(find "${REPO_ROOT}/deploy" -type f \( -name '*.yaml' -o -name '*.yml' \) | sort)

  log "== 钉额外镜像（${#ALSO_IMAGES[@]} 个，扫描 ${#files[@]} 个文件）=="
  for r in "${ALSO_IMAGES[@]}"; do
    log "-> 解析 ${r}"
    d="$(resolve_digest "$r")" || exit 3
    log "   digest ${d}"
    for f in "${files[@]}"; do
      # 该文件里根本没有这个 ref 就跳过（否则每个文件都打一行「未变化」的噪音）
      grep -qF "$r" "$f" || continue
      pin_ref_in_file "$f" "$r" "$d"
    done
  done
}

# ---- --app-image：把清单里的占位符整体换成 真实引用@digest ----------------------
pin_app_image() {
  [[ -n "$APP_IMAGE" ]] || return 0
  local d
  log "== 钉应用镜像并回写 deploy/k8s/*.yaml =="
  log "-> 解析 ${APP_IMAGE}"
  d="$(resolve_digest "$APP_IMAGE")" || exit 3
  log "   digest ${d}"

  local full="${APP_IMAGE}@${d}" full_re f
  # 替换的是「image: 后面那一整个占位值」：占位仓库名（REPLACE_ME/...）与真实仓库名不同，
  # 所以只能按「占位符的值」做整体替换，不能按 ref 字符串替换。
  # 替换串里只有 `\` 与 `&` 对 sed 有特殊含义（分隔符选了 |，ref 里不会出现），只转义这两个。
  full_re="$(printf '%s' "$full" | sed -E 's/[\\&]/\\&/g')"
  local -a touched=()
  while IFS= read -r f; do
    grep -qE '^[[:space:]]*image:[[:space:]]+.*REPLACE_ME/' "$f" || continue
    rewrite_file "$f" "s|^([[:space:]]*image:[[:space:]]+)REPLACE_ME/[^[:space:]]+|\1${full_re}|"
    touched+=("$f")
  done < <(find "$K8S_DIR" -maxdepth 1 -type f -name '*.yaml' | sort)

  # ---- 后置条件：本步**必须真的改到了东西**（否则非 0 退出）------------------
  # 为什么这条不是多余的谨慎：没有它的时候，上一步在「清单里已经没有占位符」时会
  # **正常退出 0** —— job 绿灯、summary 里的 diff 是空的、而清单里仍然是占位符，
  # 照着它 apply 会在**上线时**才 manifest unknown。那正是本脚本头注释警告的形状：
  # 「以为钉过了、其实没钉」比「没钉」危险得多，前者让整条流水线一路绿到 Pod 起不来。
  # 另一个防线（scripts/check_pinned_images.py）**故意放行**占位符（它是发布锚点），
  # 所以那两条防线都不看「这次发布到底钉上了没有」——这句话只能在这里说。
  if [[ ${#touched[@]} -eq 0 ]]; then
    # 相对路径**先算好**，不写进下面那段多行提示里：实测定过，Git Bash 下
    # 「多行双引号串里夹 ${v#"$x"/}」会让引号解析提前结束，后面任何 <…> 都会被
    # 当成输入重定向（报 “仓库: No such file or directory”，而且 `bash -n` **看不见**——
    # 语法上它合法）。所以提示里只插值变量字面值，不写 <…> 也不写 ${…#"…"/}。
    local k8s_rel="${K8S_DIR#"$REPO_ROOT"/}"
    die "在 ${k8s_rel}/ 下找不到任何形如 image: REPLACE_ME/… 的占位符，
      本次发布**什么都没钉**（产出是一个空 diff，所以按失败处理，绝不静默成功）。
      三种可能的原因，按可能性排序：
        1) 清单里的应用镜像已经是真实引用（上一版发布留下的）：把本次的 ${full}
           显式更新进清单——本脚本只回写占位符，不会去动别人的真实引用（那会误伤）。
        2) 占位符换了形状（例如写成了列表项 “- image: REPLACE_ME/…”）：改回
           “image: 后面直接跟 REPLACE_ME/仓库:tag” 这一形状，或手工钉。
        3) 清单不在这个目录（路径漂移了）：确认 --app-image 与清单目录的口径是否一致。" 4
  fi
  log "   回写了 ${#touched[@]} 个清单文件"
}

# ---- 主流程 ------------------------------------------------------------------
log "仓库根目录: ${REPO_ROOT}"
[[ "$DRY_RUN" == "1" ]] && log "模式: --dry-run（不改任何文件）"
pin_dockerfile_froms
pin_also_images
pin_app_image
log "完成。"

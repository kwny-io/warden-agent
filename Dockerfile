# Warden Agent Python —— 容器镜像
# 把 Agent HTTP/SSE 服务打包成能被 Docker 部署的镜像（本地/云都可跑）。
#
# 构建 + 运行：
#   docker build -t warden-agent .
#   docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx warden-agent
# 或直接用 docker-compose（连 PostgreSQL 的那套）。
#
# T10 起：多阶段构建。第 1 阶段用 node 构建 React+TS+Tailwind 前端，
# 第 2 阶段把构建产物放进 /app/web/dist，由 FastAPI 单端口托管（/ 是 SPA，API 同源）。
#
# ⚠️ 基础镜像用什么引用形式（tag vs tag@sha256:digest）：
#    理想是 `node:20-slim@sha256:<digest>` —— tag 可变，"同名 tag 被重新推送"会让"回滚"
#    变成"再拉一次、但不知道拉到什么"；digest 不可变，构建/发布可复现。
#    但**本文件故意只写 tag**，因为一条真实的地雷：留 `@sha256:000…0` 这类占位符能过
#    kubeconform / 语法校验，却让每一次 `docker build` 直接 `manifest unknown` ——
#    风险从"构建期立刻暴露"被推到了"上线期才暴露"。默认能构建 >> 形式上有 digest。
#    所以钉 digest 交给发布流水线里的一个显式步骤：
#      scripts/pin_image_digests.sh                    # 只钉本文件的两个基础镜像
#      scripts/pin_image_digests.sh --app-image <ref>  # 顺便把已推送的应用镜像 digest
#                                                      # 回写 deploy/k8s/deployment.yaml
#    该脚本 fail-closed：没有 docker buildx、或 registry 不可达时**非 0 退出**并给出可执行的
#    下一步，绝不静默跳过（"以为钉过了、其实没钉"比不钉更糟）。CI 在构建前调用它即可。

# ---------- 阶段 1：构建 React 前端 ----------
FROM node:20-slim AS web-build
WORKDIR /build
# 先拷锁文件装依赖（利用 Docker 层缓存）
# 锁文件**不是可选的**（不再用 `package-lock.json*` 那个可选通配）：锁文件缺失必须在**构建期**
#   立刻暴露成「文件找不到」，而不是悄悄退化成 `npm install` 重新解析一遍版本。
COPY web/package.json web/package-lock.json ./
# 只用 `npm ci`，**不留** `|| npm install` 回退（第六十七批）：那条回退能把一条**响亮失败**
#   吞成**安静的另一种成功**——`npm ci` 在锁文件与 package.json 不同步时退出码 **1** 并打印
#   「`npm ci` can only install packages when your package.json and package-lock.json or
#   npm-shrinkwrap.json are in sync. Please update your lock file with `npm install`
#   before continuing.」（本机实测，不需网络）；而 `|| npm install` 恰好把这个 1 变成 0
#   ⇒ 镜像里装进**从没被任何人看过**的版本，与 CI 前端门禁（用的是严格 `npm ci`）测过的
#   **不是同一批**——而本文件开头刚写过「构建/发布可复现」。
#   「registry 不通时靠 `npm install` 兜底」是假的：registry 不通时两者都失败；
#   这条回退唯一的真实效果就是掩盖锁文件漂移。
#   盯住这两点的是 `tests/test_dockerfile_install_fail_closed.py`（含负面对照：它必须真能红）。
RUN npm ci
# 再拷源码构建
COPY web/ ./
RUN npm run build

# ---------- 阶段 2：Python 后端 + 托管前端构建产物 ----------
FROM python:3.14-slim

# 构建时可指定 pip 源（默认官方源；国内网络可传 --build-arg PIP_INDEX_URL=<镜像地址>）
ARG PIP_INDEX_URL=https://pypi.org/simple

# 基镜像里的 OS 包也要**显式升**：python:3.12-slim 当前的 debian 13.7 里
#   libpcre2-8-0 = 10.46-1~deb13u2，而安全仓库已有 10.46-1~deb13u3
#   （CVE-2026-103111，pcre2 构造正则导致的越界写，HIGH）。
#
# 这是 CI container job 的 trivy 门禁报出来的（`--ignore-unfixed` 挡不住它：
# 它属于「有修复可用」那一类，所以门禁**应该**红）。修法是升级包，不是放宽门禁。
#
# 为什么只升这一个包、而不是 `apt-get upgrade -y`：保持镜像内容可复现；
# 其余包的安全更新跟着基镜像的更新走（CI 的构建已经带 `--pull`）。
# 下一个 CVE 来了就再补一条、并把版本与理由写在这里——门禁的作用就是逼我们看一眼。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpcre2-8-0 \
    && ver="$(dpkg -s libpcre2-8-0 | awk '/^Version:/ {print $2}')" \
    && echo "[security] libpcre2-8-0 = ${ver}" \
    && dpkg --compare-versions "${ver}" ge "10.46-1~deb13u3" \
    && rm -rf /var/lib/apt/lists/*
# 取版本号故意用 `dpkg -s … | awk` 而不是 `dpkg-query -f='${Version}'`：
#   后者的 `${Version}` 在 Dockerfile 里写法上容易和 Docker 自己的变量替换撞车
#   （有概率撞上空串）；而现在这种写法一个 `${}` 都不给 Docker 留。
# ↑ 这句 `dpkg --compare-versions` 不是装饰，是真的在拦一个失败模式：
#   apt 源里如果**根本没有**安全仓库（或拉不到），`apt-get install` 会“成功地什么也不做”
#   ——CVE 就这样随着镜像出海。加一句话让它当场构建失败。
#   （不依赖它也行：CI 的 trivy 门禁会拦；但那个红是“已经构建完了”，而这个是“根本没让它生成”。）

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WARDEN_HOST=0.0.0.0 \
    WARDEN_DB_PATH=/data/warden.db

WORKDIR /app

# 先只装**运行依赖**（不含本项目自身），这样改业务代码时这一层命中缓存、不用重装依赖。
#
# ⚠️ 必须连 README.md 一起拷：pyproject.toml 里写了 readme = "README.md"，
#    缺它 hatchling 在生成元数据时会直接 `OSError: Readme file does not exist`。
#    （原版 Dockerfile 只拷 pyproject.toml，所以镜像**构建不出来**——
#      容器路径其实从未真正跑通过。）
COPY pyproject.toml README.md ./
# ⚠️ 依赖先落成 requirements 文件再用 `-r` 装，**不能**用未加引号的 $(...) 展开成命令行参数：
#    依赖里可能带环境标记（如 pywin32 的 `; sys_platform == 'win32'`），里面有空格，
#    被 shell 按空格切开后 `==` 会变成一个独立的"依赖名"，pip 直接
#    `Invalid requirement: '=='`。加上 pywin32 之后镜像就再也构建不出来了——
#    这正是容器冒烟脚本测出来的（此前 CI 从不构建镜像，所以一直没暴露）。
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml','rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['postgres']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# 再拷源码并安装本项目自身。--no-deps：依赖上面已装好，不重复装、也不再依赖网络上的依赖解析。
COPY src ./src
RUN pip install --no-cache-dir --no-deps --index-url "${PIP_INDEX_URL}" .

# 拷贝前端构建产物到 /app/web/dist（server.py 用仓库相对路径自动找到并托管）
COPY --from=web-build /build/dist ./web/dist

# 非 root 运行（最小权限）：容器逃逸时能拿到的权限越小越好，成本几乎为零
RUN useradd --create-home --uid 10001 warden \
    && mkdir -p /data \
    && chown -R warden:warden /data
USER warden

EXPOSE 8000

# 存活探针：编排层据此判断"要不要重启"。不查依赖（依赖问题由 /health/ready 表达）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
p='http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health/live'; \
sys.exit(0 if urllib.request.urlopen(p, timeout=2).status == 200 else 1)"

# 默认启动 HTTP/SSE 服务。注意：WARDEN_HOST=0.0.0.0 是对外监听，
# 因此**必须**提供 WARDEN_API_KEY，否则 run_server 会 fail-closed 拒绝启动。
CMD ["python", "-m", "warden_agent.web.run_server"]

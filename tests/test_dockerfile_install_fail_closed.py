"""交付镜像的依赖安装必须 **fail-closed**：不许有把非 0 退出吞成 0 的回退。

现场（第六十七批）：`Dockerfile` 的 web-build 阶段原本是

    COPY web/package.json web/package-lock.json* ./
    RUN npm ci || npm install

两处叠起来，把一条**响亮失败**变成了**安静的另一种成功**：

- `npm ci` 在锁文件与 `package.json` 不同步时退出码 **1**，并打印
  「`npm ci` can only install packages when your package.json and package-lock.json or
  npm-shrinkwrap.json are in sync.」（续文：Please update your lock file with `npm install`
  before continuing.）——2026-10-06 本机实测，`C:/tmp` 沙盒、**不需要网络**；
- `|| npm install` 恰好把这个 1 变成 0 ⇒ 镜像里装进**从没被任何人看过**的版本，
  与 CI 前端门禁（用的是严格 `npm ci`）测过的**不是同一批**；
- 而同一份 `Dockerfile` 开头刚写过「构建/发布可复现」——这条回退正好从里面拆掉一根柱子。

「registry 不通时靠 `npm install` 兜底」这个理由是假的：registry 不通时**两者都失败**。
所以这条回退唯一的真实效果就是掩盖锁文件漂移；`package-lock.json*` 则让锁文件本身变成可选。

判据（静态：不需要 docker、不需要网络、不需要 Node）：
  1) 依赖安装行必须**恰好**是 `npm ci`：不许 `||` / `;` / `&&` 串第二条安装，不许用短名 `npm i`；
  2) 拷依赖清单的那条 `COPY` 必须**点名** `web/package-lock.json`，不许可选通配（`*`）。

负面对照：`test_扫描器能认出坏写法` 拿**合成的坏 Dockerfile** 自检扫描器——保证这个守卫真能红，
而不是一句永真的话（同一条取向见 `scripts/gitleaks_reverse_control.sh` 里那段注释）。

已知限制：
  - 只做静态文本检查，**不真跑** `docker build`（门禁不该依赖 docker 引擎的可用性）；
    真构建由 `ci.yml` 的 container / sbom / publish 三个 job 覆盖。
  - 只盯 **npm**（前端）。Python 侧 `pip install -r /tmp/requirements.txt` 没有回退，
    `pyproject.toml` 是单一事实源，那条轴由容器冒烟与 `tests/test_ci_tool_pinning.py` 覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"

# 依赖安装命令。短名 `npm i` 也认：否则「只写短名」就成了绕过这个守卫的口子。
_INSTALL_RE = re.compile(r"\bnpm\s+(?:ci|install|i)\b")
_WILDCARD = "*"


def _audit(dockerfile_text: str) -> list[str]:
    """返回问题清单（空 = 合规）。注释行不参与判定（否则解释坏写法的注释自己会被判违规）。"""
    problems: list[str] = []
    body = [ln.strip() for ln in dockerfile_text.splitlines() if not ln.lstrip().startswith("#")]

    installs = [ln for ln in body if _INSTALL_RE.search(ln)]
    if not installs:
        problems.append(
            "Dockerfile 里没有任何 npm 安装行（前端不再装依赖？也可能是扫描规则或文件结构变了）"
        )
    for raw in installs:
        command = raw[len("RUN ") :].strip() if raw.startswith("RUN ") else raw
        if command != "npm ci":
            problems.append(f"依赖安装行必须恰好是 `npm ci`，实际是：{raw}")

    manifest_copies = [
        ln for ln in body if ln.startswith("COPY ") and "package.json" in ln
    ]
    if not manifest_copies:
        problems.append("没有拷贝 package.json / package-lock.json 的 `COPY` 行")
    for line in manifest_copies:
        if "package-lock.json" not in line:
            problems.append(f"锁文件必须与 package.json 一起拷贝，缺 package-lock.json：{line}")
        if _WILDCARD in line:
            problems.append(f"清单不许可选通配（`*`）——锁文件缺失必须在构建期就报错：{line}")
    return problems


def test_Dockerfile的依赖安装必须fail_closed() -> None:
    problems = _audit(DOCKERFILE.read_text(encoding="utf-8"))
    assert problems == [], "Dockerfile 的依赖安装不 fail-closed：\n  - " + "\n  - ".join(problems)


_GOOD = """\
FROM node:20-slim AS web-build
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
RUN npm run build
"""

# 注释里**提到**坏写法不算违规：这份真实文件里就写着「不许 `|| npm install`」的理由。
_GOOD_WITH_COMMENT = """\
FROM node:20-slim AS web-build
# 以前这里是 `RUN npm ci || npm install`，它会把锁文件漂移吞掉，所以不留回退。
COPY web/package.json web/package-lock.json ./
RUN npm ci
"""

_LOCK_COPY = "COPY web/package.json web/package-lock.json ./\n"

_BAD = {
    "回退到 npm install": _LOCK_COPY + "RUN npm ci || npm install\n",
    "分号串第二条安装": _LOCK_COPY + "RUN npm ci; npm install\n",
    "与号串第二条安装": _LOCK_COPY + "RUN npm ci && npm install\n",
    "裸 npm install": _LOCK_COPY + "RUN npm install\n",
    "短名 npm i": _LOCK_COPY + "RUN npm i\n",
    "锁文件可选通配": "COPY web/package.json web/package-lock.json* ./\nRUN npm ci\n",
    "清单整体通配": "COPY web/package*.json ./\nRUN npm ci\n",
    "只拷 package.json": "COPY web/package.json ./\nRUN npm ci\n",
    "没有任何安装行": _LOCK_COPY,
}


def test_扫描器自己认得合规写法() -> None:
    assert _audit(_GOOD) == [], _audit(_GOOD)


def test_注释里提到坏写法不算违规() -> None:
    assert _audit(_GOOD_WITH_COMMENT) == [], _audit(_GOOD_WITH_COMMENT)


@pytest.mark.parametrize(("name", "text"), sorted(_BAD.items()))
def test_扫描器能认出坏写法(name: str, text: str) -> None:
    # 负面对照：每条坏写法都必须被抓到。抓不到 = 守卫是句空话，比没有守卫更糟。
    assert _audit(text), f"扫描器漏掉了坏写法：{name}"

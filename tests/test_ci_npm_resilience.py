"""CI 的 npm 安装不许被第三方镜像的一次 504 判死（第九十九批的真事故）。

事故现场（run 39175595595，dev@656029c）：
    npm error code E504
    npm error 504 Gateway Time-out - GET https://registry.npmmirror.com/zod-to-json-schema/-/zod-to-json-schema-3.25.2.tgz
quality job 在「安装 ts 客户端依赖」这步就退出 1，pytest 根本没跑 ⇒ junit 也没产出
⇒ 下游「跳过台账」判据拿不到真数据。代码一行没变，整条交付面却红了。

于是加固成 `scripts/npm_ci.sh`（重试 + 换源重试），这里核三件事：
1. 加固脚本存在，且两件关键手段都在（重试次数、换官方源 + `--replace-registry-host=always`）；
2. workflow 里**不再有裸 `npm ci`**（回到裸的就是把洞挖回去）；
3. 装了依赖的两个工作目录（`web`、`ts/mcp-client`）都得走加固版。

因为换源不降低校验强度：内容完整性仍由 lockfile 里的 `integrity`（sha512）保证。
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SCRIPT = ROOT / "scripts" / "npm_ci.sh"


def _ci_text() -> str:
    return CI.read_text(encoding="utf-8")


def test_加固脚本存在且真的有重试与换源():
    assert SCRIPT.is_file(), "缺 scripts/npm_ci.sh（加固点在它身上）"
    body = SCRIPT.read_text(encoding="utf-8")
    assert "npm ci" in body
    assert re.search(r"while .*retries", body), "没有重试循环 ⇒ 504 抖动一次就死"
    assert "--fetch-retries" in body and "--fetch-timeout" in body, "没加大 fetch 的重试/超时"
    assert "--registry=https://registry.npmjs.org/" in body, "没有换官方源那一层"
    assert "--replace-registry-host=always" in body, (
        "换源时必须带 --replace-registry-host=always：lockfile 里 resolved 写的是镜像主机，"
        "不带它 npm 会继续打镜像"
    )


def test_workflow_里不许再有裸_npm_ci():
    """裸 `npm ci` 出现在 run 里 = 把这次事故的洞挖回去。

    只认「整行就是 npm ci（可带注释）」这种写法；`bash …/npm_ci.sh` 与 `npm ci` 出现在
    注释/说明文字里都不算（历史注释解释了为什么不用裸的，那是有价值的文字）。
    """
    bad = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(_ci_text().splitlines(), 1)
        if re.fullmatch(r"\s*npm ci\s*", line)
    ]
    assert not bad, "workflow 里还有裸 `npm ci`（该换成 scripts/npm_ci.sh）：\n" + "\n".join(bad)


def test_两个装依赖的目录都走加固版():
    text = _ci_text()
    # 把 workflow 切成 step 块，找 working-directory 是这个目录的那些块
    blocks = re.split(r"\n(?=\s*- name: )", text)
    for wd, rel in (("web", "../scripts/npm_ci.sh"), ("ts/mcp-client", "../../scripts/npm_ci.sh")):
        mine = [b for b in blocks if f"working-directory: {wd}" in b]
        assert mine, f"workflow 里找不到 working-directory: {wd} 的步骤"
        joined = "\n".join(mine)
        assert f"bash {rel}" in joined, (
            f"{wd} 的安装步骤没走 {rel}（加固没接上）：\n"
            + "\n".join(line for line in joined.splitlines() if "npm" in line)
        )

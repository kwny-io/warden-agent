"""审计脱敏单元测试：`core/redaction.py`。

设计意图（测试要守住的不变量）：
  - **秘密不进盘**：敏感键下的值、以及文本里按模式认出来的秘密，都换掉；
  - **可比不可还原**：换成的标记带原文指纹，同值同标记、异值异标记；
  - **不做过度脱敏**：普通字段（路径、命令、参数名）与 hash/UUID 这类"看着像随机串"的
    东西必须保持可读——否则账本没法用，运维会想办法绕过它；
  - **不能成为新的攻击面**：超长字符串、深嵌套、海量键、恶意 `__repr__` 都不能让它炸。

变异测试（`[tool.mutmut]` 圈定的三个模块之一）下，本文件有 2 条**杀不掉的等价变异**
（改了行为一样，不是缺口，别去补测）：sha256 的 `utf-8` 换成 `UTF-8`（codec 别名）、
`_sub` 里 `group == 0` 换成 `group == 1`（那条分支恰好也是 0）。证明见 `docs/mutation.md`。
"""

from __future__ import annotations

import hashlib
import re

import pytest

from warden_agent.core.redaction import (
    MAX_ITEMS,
    MAX_STR,
    REDACTED_MARK,
    is_sensitive_key,
    redact_arguments,
    redact_text,
)

SECRET = "sk-live-TOPSECRET123456"


# ---------- 键名判定 ----------


@pytest.mark.parametrize(
    "key",
    [
        "api_key", "apiKey", "API-KEY", "access_key", "secret", "client_secret",
        "token", "auth_token", "refresh_token", "password", "passwd", "pwd",
        "authorization", "credential", "private_key", "session_id", "signing_key",
        "db_password_hash",  # 子串匹配：前缀/后缀带上下文也要命中
    ],
)
def test_敏感键名命中(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize("key", ["path", "command", "city", "url", "content", "sha256", "run_id"])
def test_普通键名不命中(key: str) -> None:
    assert is_sensitive_key(key) is False


# ---------- 值级脱敏 ----------


def test_敏感键下的值换指纹() -> None:
    out = redact_arguments({"api_key": SECRET})
    assert out["api_key"].startswith(REDACTED_MARK)
    assert SECRET not in str(out)


def test_同值同标记_异值异标记() -> None:
    """指纹的价值：审计仍能判断"两次审批用的是不是同一个密钥"。"""
    a = redact_arguments({"token": SECRET})["token"]
    b = redact_arguments({"token": SECRET})["token"]
    c = redact_arguments({"token": "sk-live-OTHER9999999999"})["token"]
    assert a == b != c


def test_非敏感字段保持可读() -> None:
    args = {
        "command": "ls -la /tmp",
        "path": "/var/log/warden.log",
        "timeout": 30,
        "dry_run": True,
        "nothing": None,
    }
    assert redact_arguments(args) == args


def test_嵌套与列表递归脱敏() -> None:
    out = redact_arguments(
        {"outer": [{"password": SECRET}, {"safe": "ok"}], "api_key": SECRET}
    )
    assert out["outer"][0]["password"].startswith(REDACTED_MARK)
    assert out["outer"][1] == {"safe": "ok"}
    assert out["api_key"].startswith(REDACTED_MARK)


# ---------- 文本规则（秘密藏在普通字符串里）----------


@pytest.mark.parametrize(
    "text",
    [
        f"curl -H 'Authorization: Bearer {SECRET}' https://api.example.com",
        f"Bearer {SECRET}",
        f"https://api.example.com?api_key={SECRET}&x=1",
        f"token={SECRET}",
        f"password: {SECRET}",
        f"cli --token={SECRET} --verbose",
        f"cli --api-key {SECRET} --verbose",
        "postgres://warden:s3cr3tp4ssw0rd@db.internal:5432/warden",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ],
)
def test_文本里的秘密被换掉(text: str) -> None:
    out = redact_text(text)
    assert REDACTED_MARK in out
    for fragment in ("TOPSECRET", "s3cr3tp4ssw0rd", "eyJhbGciOiJIUzI1NiJ9"):
        assert fragment not in out


def test_文本规则不误伤普通内容() -> None:
    text = "git apply --check /tmp/p.patch && echo done"
    assert redact_text(text) == text


def test_不像秘密的随机串不脱敏() -> None:
    """不做"高熵就抹掉"的启发式：hash/UUID 抹了账本就不可用了。"""
    text = "sha256=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    out = redact_text(text)
    assert "9f86d081884c7d659a2feaa0c55ad015" in out


def test_超长字符串被截断() -> None:
    out = redact_text("a" * 5000)
    assert "truncated" in out
    assert len(out) < 1000


# ---------- 不能成为新的攻击面 ----------


def test_深度上限() -> None:
    deep: dict[str, object] = {}
    node = deep
    for _ in range(40):
        child: dict[str, object] = {}
        node["child"] = child
        node = child
    node["api_key"] = SECRET
    out = redact_arguments(deep)
    assert "<max-depth>" in str(out)
    assert SECRET not in str(out)


def test_键数量上限() -> None:
    out = redact_arguments({f"k{i}": i for i in range(500)})
    assert "truncated" in str(out)
    assert len(out) <= 71


def test_不可序列化对象转repr且不炸() -> None:
    class Weird:
        def __repr__(self) -> str:  # noqa: D105
            raise RuntimeError("boom")

    out = redact_arguments({"payload": Weird(), "when": object()})
    assert out["payload"] == "<unrepr-able>"
    assert isinstance(out["when"], str)


def test_bytes按文本处理() -> None:
    out = redact_arguments({"note": b"token=abc12345678"})
    assert REDACTED_MARK in out["note"]


# =========================================================================== #
# 边界与细节：这些用例是**变异测试**（scripts/mutation.sh）逼出来的。
# 变异测试把源码里的比较符/常量/分支逐个改掉再跑测试：测试仍绿 = 那段逻辑
# 没有任何测试约束它。下面每一条都写着"改坏了会怎样"，避免下次有人删掉它。
# =========================================================================== #


def test_敏感键下的短口令也整值换指纹() -> None:
    """改坏了会怎样：敏感键判定被短路（`if _sensitive_key else ...` 失效）时，
    短口令会**原样落库**——它不含 `sk-`/`Bearer` 这类模式，值级规则认不出来。
    `hunter2` 这类短口令正是"键名可信、值不可信"的典型场景。
    """
    out = redact_arguments({"password": "hunter2", "token": "x"})
    assert out["password"].startswith(REDACTED_MARK)
    assert "hunter2" not in str(out)
    assert out["token"].startswith(REDACTED_MARK)


def test_指纹固定8位且等于原文sha256前8位() -> None:
    """改坏了会怎样：指纹长度/算法漂移 → **同一密钥在旧账本与新账本里标记不同**，
    "两次审批用的是不是同一个密钥"这个唯一还能回答的问题就答不了了。
    """
    marked = redact_arguments({"token": SECRET})["token"]
    assert re.fullmatch(r"<redacted:[0-9a-f]{8}>", marked), marked
    assert marked == f"<redacted:{hashlib.sha256(SECRET.encode()).hexdigest()[:8]}>"


def test_非法编码的字节与孤立代理项都不炸且仍被脱敏() -> None:
    """改坏了会怎样：`encode/decode(..., "replace")` 的容错参数被去掉或写错名字，
    遇到孤立代理项/非法 UTF-8 时**直接抛异常**（UnicodeEncodeError / LookupError）——
    而账本写入不能因为参数里有一个坏字节就整条失败。
    """
    surrogate = "\ud800"  # 单独一个代理项：strict 编码会抛
    masked = redact_arguments({"token": surrogate})["token"]
    assert masked.startswith(REDACTED_MARK)

    bad_bytes = b"token=abc12345678\xff\xfe"  # 非法 UTF-8：strict 解码会抛
    out = redact_arguments({"note": bad_bytes})
    assert isinstance(out["note"], str) and REDACTED_MARK in out["note"]

    # 敏感键 + bytes：走的是另一条分支（整值换指纹，而不是按模式局部替换）
    sensitive_bytes = redact_arguments({"token": b"raw\xff\xfe-bytes"})
    assert sensitive_bytes["token"].startswith(REDACTED_MARK)


def test_截断边界_恰好上限不截断_超一个就带准确长度() -> None:
    """改坏了会怎样：`<=` 写成 `<` 会白截断一条正好到上限的参数；
    `dropped` 算错（加减号写反、算成 None）会让账本上显示"截掉了 -128 个字符"这种鬼话，
    审计据此判断"原文有多长"就错了。
    """
    exactly = "a" * MAX_STR
    assert redact_text(exactly) == exactly

    over = "b" * (MAX_STR + 1)
    # 头尾各留 320 / 128，中间的 …<truncated N chars>… 里的 N 必须是**真实丢弃长度**：
    # 513 - 320 - 128 = 65（写反加减号就会算成 321 / 705 这种不可能的数）
    assert redact_text(over) == "b" * 320 + "…<truncated 65 chars>…" + "b" * 128


def test_同一段文本里的多个秘密都要换掉() -> None:
    """改坏了会怎样：规则循环里 `continue` 写成 `break`——第一条命中的规则之后
    直接收工，**后面几条规则全部不生效**（漏脱敏 = 明文密钥进账本）。
    """
    text = "leak sk-live-AAAAAAAAAAAAAAAAAAAA and password=p4ssword1234"
    out = redact_text(text)
    assert out.count(REDACTED_MARK) == 2, out
    assert "p4ssword1234" not in out


def test_局部替换保留秘密前后的上下文() -> None:
    """改坏了会怎样：head/tail 拼接写错（`None`、下标用 start+end 而不是 end-start）
    会把秘密周围的文字吃掉或塞进 `None` 字面量——账本变成没法读的乱码。
    """
    out = redact_text("run --token=sk-live-TOPSECRET123456 --verbose")
    assert out.startswith("run --token=")
    assert out.endswith(" --verbose")
    assert "None" not in out


def test_嵌套在敏感键下的整棵子树都脱敏() -> None:
    """这条用例抓的是**一个真漏洞**（不是抛光）：写它的时候发现
    `{"credentials": {"value": "hunter2"}}` 里的 `hunter2` **明文落库**。

    原因：递归时字典分支只按“当前这一级的键名”重算敏感标记，**把父级的判定丢了**
    （列表分支传了、字典分支没传）。于是内层键名（`value`/`inner`/`db`）本身不像秘密时，
    父级已经认出来“这是凭据”，子层却不知道，值级模式也认不出 `hunter2` 这种短口令。

    改法：子级标记 `is_sensitive_key(key) or _sensitive_key`（只能变敏感，不能变不敏感）。
    改坏了会怎样：一旦变回“只按当前键名算”，嵌套凭据又静默明文落库。
    """
    out = redact_arguments({
        "credentials": {"value": "hunter2", "db": {"pwd": "s3cret"}},
        "token": {"inner": "plain-value", "list": ["plain-value"]},
    })
    assert out["credentials"]["value"].startswith(REDACTED_MARK)
    assert out["credentials"]["db"]["pwd"].startswith(REDACTED_MARK)
    assert out["token"]["inner"].startswith(REDACTED_MARK)
    assert out["token"]["list"][0].startswith(REDACTED_MARK)
    assert "hunter2" not in str(out) and "plain-value" not in str(out)


def test_普通分支下的嵌套结构保持不变() -> None:
    """反向守卫：“敏感”只能向外扩散，不能**过度**扩散——
    非敏感键下的兄弟节点、以及非敏感键自己的值，必须原样保留（账本要可读）。
    """
    args = {
        "command": ["ls", "-la"],
        "nested": {"count": 3, "deep": {"ok": True}},
        "path": "/tmp/x",
    }
    assert redact_arguments(args) == args


def test_非字符串键转成字符串而不是None() -> None:
    """改坏了会怎样：`str(k)` 被写死成 `str(None)`——JSON 里的数字键/元组键
    会全部变成 `"None"`，多条不同参数在账本里看起来一模一样（证据失效）。
    """
    out = redact_arguments({1: "x", (2, 3): "y"})
    assert out == {"1": "x", "(2, 3)": "y"}


def test_键数量上限_恰好上限不截断_多一个才截断且数字准确() -> None:
    """改坏了会怎样：`i >= MAX_ITEMS` 写成 `>` 会白丢一个键（刚好 64 个键的参数少一个）；
    `len(value) - MAX_ITEMS` 加减号写反会让"被截掉多少个键"显示成 128 这种不可能的数。
    """
    at_limit = {f"k{i}": i for i in range(MAX_ITEMS)}
    assert "truncated" not in str(redact_arguments(at_limit))

    over = {f"k{i}": i for i in range(MAX_ITEMS + 1)}
    out = redact_arguments(over)
    assert out["…"] == "<truncated 1 keys>", out["…"]


def test_列表项上限_恰好上限不加标记() -> None:
    """改坏了会怎样：`len(items) > MAX_ITEMS` 写成 `>=` 时，
    一个正好 64 项的列表会被追加 `truncated 0 items`——告诉读账本的人"有 0 项被截掉"。
    """
    at_limit = [f"v{i}" for i in range(MAX_ITEMS)]
    out = redact_arguments(at_limit)
    assert len(out) == MAX_ITEMS and "truncated" not in str(out)

    over = redact_arguments([f"v{i}" for i in range(MAX_ITEMS + 1)])
    assert over[-1] == "<truncated 1 items>"


def test_递归深度边界_恰好MAX_DEPTH仍正常脱敏() -> None:
    """改坏了会怎样：深度默认值（0 → 1）、比较符（> → >=）、步长（+1 → +2/-1）
    任何一个写错，都会让**本该脱敏的那一层提前变成 `<max-depth>`**
    （或者反过来：永远碰不到上限，深层秘密明文落库）。
    这里卡的是边界本身：8 层正常脱敏，9 层才标记。
    """
    def nest(levels: int) -> dict[str, object]:
        node: dict[str, object] = {"token": SECRET}
        for _ in range(levels - 1):
            node = {"child": node}
        return node

    ok: object = redact_arguments(nest(8))
    assert "<max-depth>" not in str(ok)
    assert SECRET not in str(ok)

    too_deep: object = redact_arguments(nest(9))
    node2 = too_deep
    while isinstance(node2, dict) and "child" in node2:
        node2 = node2["child"]
    assert isinstance(node2, dict) and node2["token"] == "<max-depth>"


def test_不可序列化对象保留repr内容() -> None:
    """改坏了会怎样：`redact_text(repr(value))` 变成 `redact_text(None)` → 抛异常被兜底，
    账本里只剩 `<unrepr-able>`，**看不到参数到底是什么**（连类型都没有）。
    """
    out = redact_arguments({"when": object()})
    assert "object at 0x" in out["when"], out["when"]


def test_替换时保留键名引号与连接串里的_at() -> None:
    """替换只动秘密本身——`key='值'` 的引号、`user:pass@host` 的 `@` 都要留在原位。

    改坏了会怎样（变异测试抓到的真实情形）：头/尾切片写成 `m.group(1)`、
    或把尾巴偏移写成 `end + m.start()` 时，尾巴算空——口令确实被换掉了，
    但紧跟其后的引号 / `@` 一起消失：账本里变成 `PASSWORD=<redacted:xxx>"`、
    `postgres://user:<redacted:xxx>host/db`。审计的人会以为是别处写错了，
    而不是"这里被脱敏了"。
    """
    quoted = redact_text('psql -c "ALTER USER x PASSWORD=\'hunter2\'"')
    assert "PASSWORD='<redacted:" in quoted, quoted
    assert quoted.count("'") == 2, quoted  # 值两侧的引号都还在

    dsn = redact_text("连 db: postgres://warden:s3cr3tp4ssw0rd@db.internal:5432/warden")
    assert "postgres://warden:<redacted:" in dsn, dsn
    assert dsn.endswith("@db.internal:5432/warden"), dsn  # userinfo 后的 @ 必须留着
    assert "s3cr3tp4ssw0rd" not in dsn


def test_非敏感键下的bytes保留可读文本() -> None:
    """bytes 按文本走值级规则（不是整段打指纹）——否则账本里全是 hash，读不出内容。

    改坏了会怎样（变异测试抓到的真实情形）：bytes 分支被写成"总是 `_masked`"，
    于是 `b"hello world"` 这种**没有秘密的日志片段**也被换成 `<redacted:xxx>`，
    归档日志直接失去排查价值。
    """
    out = redact_arguments({"log": b"hello world, nothing secret"})
    assert out["log"] == "hello world, nothing secret"


def test_列表嵌套的深度上限_恰好MAX_DEPTH仍脱敏() -> None:
    """列表分支同样受递归深度约束（否则 `[[[[…]]]]` 能把栈打穿）。

    改坏了会怎样（变异测试抓到的真实情形）：列表递归写成 `_depth` 不递增 / `+ 2`——
    前者让深度上限**永不生效**（深嵌套参数把栈吃光 → 审批写不进去），
    后者把正常深度的参数误判成超深（值变成 `<max-depth>`，账本丢证据）。
    """

    def nest(levels: int) -> object:
        node: object = {"token": SECRET}
        for _ in range(levels - 1):
            node = [node]
        return node

    ok = redact_arguments(nest(8))
    assert "<max-depth>" not in str(ok), "恰好 MAX_DEPTH 层不该被截断"
    assert SECRET not in str(ok)

    assert "<max-depth>" in str(redact_arguments(nest(9)))


def test_非捕获组规则命中时整段替换与_sub分支等价() -> None:
    """说明性用例：`if group == 0` 与走 `_sub`，在**非捕获组规则**上产出完全一致。

    为什么特意写这条：变异测试会把 `if group == 0:` 改成 `== 1`，看上去"存活"，
    但 `_sub` 的默认参数 `_g` 取的正是同一个 `group`（=0），`span(0)` 算出的头尾都是
    空串，结果与专用分支逐字节相同——这是**等价变异**，不是测试缺口。
    钉住它，免得后人把它当漏洞去追（或者反过来，为了"杀掉"它去改实现）。
    """
    bare_jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = redact_text(f"上游 401，key 是 {bare_jwt}，请核对")
    assert out.startswith("上游 401，key 是 <redacted:"), out
    assert out.endswith(">，请核对"), out
    assert bare_jwt not in out

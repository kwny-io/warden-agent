"""审计/审批落库前的**参数脱敏**。

## 为什么要有这个模块

审批历史与审计账本记的是工具的**原始参数**（`shell.run` 的 command、`mcp.call_tool`
的 headers、`fs.write` 的 content、`credential` 相关调用的 body……）。这些参数里经常
夹着凭据：`Authorization: Bearer <key>`、`--token=<key>`、`api_key=...`、写进命令行的
数据库口令。审批历史是一张给运维/审计回看的表——原样落库就等于把密钥**又复制了一份**，
而且复制到了更容易被导出的地方（备份、只读账号、工单附件）。

## 设计取舍（这些是刻意的，不是偷懒）

- **默认总是脱敏，没有开关可关**。理由：开关的失败方向是"忘了配环境变量 → 密钥落库"，
  这是不可接受的。脱敏后的账本仍然可用（见下条），所以不需要"为了排障先关掉"。
- **脱敏 ≠ 删除，而是替换成指纹**：`<redacted:ab12cd34>`（原文 sha256 前 8 位）。
  审计据此仍能回答"两次审批用的是不是同一个密钥""这条参数和那条是不是同一个"，
  但拿不回原文。纯 `***` 会让这类比对彻底失效。
- **只脱敏"看起来是秘密"的东西**：敏感键名（`password`/`token`/`api_key`/…）下的值一律
  换指纹；文本里按已知模式（Bearer、`key=value`、URL userinfo、JWT、常见前缀）做局部替换。
  不搞"高熵就脱敏"的启发式——那会把 hash、UUID、run_id 一起打掉，账本就没法看了。
- **递归有限深、字符串有限长**：参数是任意 JSON（可嵌套、可超长），脱敏本身不能变成
  DoS 面，也不能让一条审计把账本撑爆。

## 与 `credential.broker.SecretRedactor` 的分工（不要合并）

`SecretRedactor` 是"**已知密钥**名单替换"：它必须先有人把密钥登记进 broker，只适合抹
自己的模型 key 这类"我知道它长什么样"的场合。本模块是"**未知密钥**按模式识别"：
审批参数里夹的是**别人系统**的 token（用户随手贴进 prompt 的、MCP 服务端回显的），
没有名单可依。两者互补：名单类走 broker，模式类走这里。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = [
    "REDACTED_MARK",
    "is_sensitive_key",
    "redact_arguments",
    "redact_text",
]

#: 脱敏标记前缀（便于测试与 grep：`grep '<redacted:' 就能数出被脱敏的条数）
REDACTED_MARK = "<redacted:"

#: 单条字符串参数的最大留存长度。超长内容（例如整篇文件写入）只留头尾，
#: 否则一条审批就能往账本里塞几 MB。
MAX_STR = 512
#: 头尾各保留多少
_HEAD = 320
_TAIL = 128
#: 递归深度与容器规模上限
MAX_DEPTH = 8
MAX_ITEMS = 64

#: 键名命中即脱敏。故意用子串匹配（`db_password_hash`、`X-Api-Key` 都要命中），
#: 但**不**把 `url` 之类泛词写进来——URL 用值级规则只脱 userinfo 与敏感 query。
_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|secret[_-]?key|credential|authorization|auth[_-]?token|"
    r"client[_-]?secret|refresh[_-]?token|session[_-]?id|cookie|signature|salt|"
    r"passwd|pwd|dsn|connection[_-]?string|ssh[_-]?key|signing[_-]?key)",
    re.IGNORECASE,
)

# 值级规则：把"藏在普通字符串里"的秘密替换掉。顺序有意义（先具体后宽泛）。
_TEXT_RULES: tuple[tuple[re.Pattern[str], int], ...] = (
    # Authorization: Bearer <token> / Bearer <token>
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=\-]{8,})"), 2),
    # Authorization: <scheme> <token>（Basic xxx / Token xxx / ApiKey xxx）
    (re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;'\"]{6,})"), 2),
    # key=value / key: value（键名是敏感词）
    (
        re.compile(
            r"(?i)((?:api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|pwd)"
            r"\s*[:=]\s*)(['\"]?)([^\s'\",;]{4,})"
        ),
        3,
    ),
    # CLI 参数：--token=xxx / --api-key xxx / -p xxx（只认敏感参数名）
    (
        re.compile(
            r"(?i)(--?(?:token|password|passwd|api[_-]?key|apikey|secret)[=\s]+)"
            r"(['\"]?)([^\s'\"]{4,})"
        ),
        3,
    ),
    # URL userinfo：scheme://user:password@host → 只脱口令部分
    (re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)"), 2),
    # JWT（三段式，头部固定 base64url 的 eyJ 开头）
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b"),
        0,
    ),
    # 常见厂商密钥的**裸前缀**（没有 `key=` 这种键名可依的情况）。
    # 为什么需要：日志里最常见的是"上游 401，用的 key 是 sk-ant-..."这种句子，
    # 前面没有键名，光靠 key=value 规则漏。
    # 为什么长度门槛拉到 20+ 且要求左边不是标识符字符：避免把 `task-1234567890` 里的
    # `sk-1234567890` 当密钥（真密钥远长于此：OpenAI `sk-` 48、Anthropic `sk-ant-api03-` 90+）。
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:"
            r"sk-[A-Za-z0-9_-]{20,}"          # OpenAI / Anthropic
            r"|sk-ant-[A-Za-z0-9_-]{20,}"
            r"|gh[pousr]_[A-Za-z0-9]{20,}"    # GitHub token
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_-]{20,}"      # GitLab
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
            r"|(?:AKIA|ASIA)[A-Z0-9]{16}"     # AWS access key id
            r"|AIza[A-Za-z0-9_-]{30,}"        # Google
            r"|hf_[A-Za-z0-9]{20,}"           # HuggingFace
            r")"
        ),
        0,
    ),
)


def is_sensitive_key(key: str) -> bool:
    """键名是否表示"值大概率是秘密"。"""
    return bool(_SENSITIVE_KEY_RE.search(key))


def _fingerprint(value: str) -> str:
    """原文指纹：同样内容给同样标记（可比对、不可还原）。"""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def _masked(value: str) -> str:
    return f"{REDACTED_MARK}{_fingerprint(value)}>"


def _truncate(value: str) -> str:
    if len(value) <= MAX_STR:
        return value
    dropped = len(value) - _HEAD - _TAIL
    return f"{value[:_HEAD]}…<truncated {dropped} chars>…{value[-_TAIL:]}"


def redact_text(text: str) -> str:
    """对自由文本做局部脱敏（保留上下文，只换掉秘密本身）。"""
    out = text
    for pattern, group in _TEXT_RULES:
        if group == 0:
            out = pattern.sub(lambda m: _masked(m.group(0)), out)
            continue

        def _sub(m: re.Match[str], _g: int = group) -> str:
            start, end = m.span(_g)
            # 用整段前缀（含 key= / Bearer 前缀）+ 指纹替换，读起来仍是"哪个字段被脱敏了"
            head = m.group(0)[: start - m.start()]
            tail = m.group(0)[end - m.start():]
            return f"{head}{_masked(m.group(_g))}{tail}"

        out = pattern.sub(_sub, out)
    return _truncate(out)


def redact_arguments(value: Any, *, _depth: int = 0, _sensitive_key: bool = False) -> Any:
    """递归脱敏一份工具参数（保持 JSON 形状，便于账本原样展示）。

    非 JSON 可序列化的对象（自定义类、bytes 等）转成截断后的 `repr`——账本里存的是
    **审计证据**，不该因为参数里塞了个奇怪对象就把写入整条炸掉。
    """
    if _depth > MAX_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
        return _masked(value) if _sensitive_key else redact_text(value)
    if isinstance(value, str):
        return _masked(value) if _sensitive_key else redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out["…"] = f"<truncated {len(value) - MAX_ITEMS} keys>"
                break
            key = k if isinstance(k, str) else str(k)
            # 父级已判定为敏感时，整棵子树继续按敏感处理（只能变敏感，不能变不敏感）：
            # 否则 {"credentials": {"value": "hunter2"}} 这类**嵌套凭据会明文落库**——
            # 内层键名（value/db/inner）本身不像秘密，值级模式也认不出短口令。
            out[key] = redact_arguments(
                v,
                _depth=_depth + 1,
                _sensitive_key=is_sensitive_key(key) or _sensitive_key,
            )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        redacted = [
            redact_arguments(v, _depth=_depth + 1, _sensitive_key=_sensitive_key)
            for v in items[:MAX_ITEMS]
        ]
        if len(items) > MAX_ITEMS:
            redacted.append(f"<truncated {len(items) - MAX_ITEMS} items>")
        return redacted
    try:
        return redact_text(repr(value))
    except Exception:  # noqa: BLE001 - repr 都可能抛（恶意 __repr__），账本不能因此失败
        return "<unrepr-able>"

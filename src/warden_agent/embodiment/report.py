"""事件时间线报告：把一次运行变成**能贴给别人看的自包含 HTML**。

为什么在 Python 里生成而不是画在前端：
  前端图要先起服务、要联网、要截图；而"拒绝过一次危险指令"这件事必须在**评委的笔记本上双击就开**。
  所以这里只产出一个 HTML 文件：无外链、无脚本、无 CDN，断网也能看，能进仓库当证据。

它刻意不做的事：
  - 不做时序图的自动布线（那是 archify 的事），只按 kind 分行、按时间定 x；
  - 不美化数据——被拒的事件就是红的，不会因为"看起来乱"被合并掉；
  - 不写"成功率"这种没有分母的指标。图上出现几个数，就必须能追回到事件流。

读法（报告里也印着）：横轴是设备时钟，不是墙钟；同一拍的多条事件会落在同一列，
这是**因果同时发生**，不是画重了。
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from warden_agent.embodiment.types import Code, Event

# 页面上最多画多少个点：再多就该看表格了（也避免一次长跑生成 10MB HTML）。
MAX_MARKS = 400

# 被拒语义的判定只认裁决码：`kind` 是"谁说的"，`code` 才是"结论"。
_DENIED_CODES = {c.value for c in Code if c is not Code.OK}

_LANE_H = 30
_PAD_L = 152
_PAD_R = 24
_WIDTH = 1040


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """报告抬头。字段都是"可复核的事实"，不放形容词。"""

    title: str
    backend: str
    joint_count: int
    metrics: Mapping[str, str]
    note: str = ""


def _entities(text: str) -> str:
    return html.escape(text, quote=True)


def _denied(event: Event) -> bool:
    return event.code in _DENIED_CODES


def _lanes(events: Sequence[Event]) -> list[str]:
    """按 kind 分行；`command` 永远在最上面（它是"人想干什么"，其余都是对它的反应）。"""
    kinds = {e.kind for e in events}
    rest = sorted(k for k in kinds if k != "command")
    return (["command"] if "command" in kinds else []) + rest


def _timeline_svg(events: Sequence[Event], lanes: Sequence[str]) -> str:
    if not events:
        return ('<p class="empty">这次运行没有事件——**没有事件不等于没事发生**，'
                '先查事件是否被接上。</p>')
    t0 = min(e.t for e in events)
    t1 = max(e.t for e in events)
    span = max(t1 - t0, 1e-9)
    height = _LANE_H * len(lanes) + 34
    inner = _WIDTH - _PAD_L - _PAD_R
    parts: list[str] = [
        f'<svg viewBox="0 0 {_WIDTH} {height}" width="100%" role="img" '
        f'aria-label="事件时间线：{len(events)} 条事件，{len(lanes)} 条泳道">'
    ]
    # 泳道底纹与标签
    for i, kind in enumerate(lanes):
        y = 20 + i * _LANE_H
        alt = " lane-alt" if i % 2 else ""
        parts.append(f'<rect x="{_PAD_L}" y="{y}" width="{inner}" '
                     f'height="{_LANE_H - 6}" class="lane{alt}"/>')
        parts.append(f'<text x="{_PAD_L - 10}" y="{y + 16}" class="lane-label" '
                     f'text-anchor="end">{_entities(kind)}</text>')
    shown = events[:MAX_MARKS]
    for e in shown:
        lane = lanes.index(e.kind)
        x = _PAD_L + (e.t - t0) / span * inner
        cy = 20.0 + lane * _LANE_H + (_LANE_H - 6) / 2
        cls = "denied" if _denied(e) else "ok"
        tip = f"{e.t:.3f}s · {e.kind}/{e.code}" + (f" · {e.detail}" if e.detail else "")
        parts.append(f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="4.6" class="mark {cls}">'
                     f'<title>{_entities(tip)}</title></circle>')
    # 时间轴
    base = 20 + len(lanes) * _LANE_H
    parts.append(f'<line x1="{_PAD_L}" y1="{base}" x2="{_WIDTH - _PAD_R}" y2="{base}" '
                 f'class="axis"/>')
    for frac, label in ((0.0, f"{t0:.2f}s"), (0.5, f"{t0 + span / 2:.2f}s"), (1.0, f"{t1:.2f}s")):
        x = _PAD_L + frac * inner
        parts.append(f'<text x="{x:.1f}" y="{base + 16}" class="tick" '
                     f'text-anchor="middle">{label}</text>')
    parts.append("</svg>")
    if len(events) > MAX_MARKS:
        parts.append(f'<p class="empty">只画了前 {MAX_MARKS} 条（共 {len(events)} 条）；'
                     f'完整清单见下表。</p>')
    return "".join(parts)


def _kpi(meta: ReportMeta) -> str:
    cells = "".join(f'<div class="kpi"><div class="k">{_entities(k)}</div>'
                    f'<div class="v">{_entities(v)}</div></div>' for k, v in meta.metrics.items())
    return f'<div class="kpis">{cells}</div>'


def _table(events: Iterable[Event]) -> str:
    rows = []
    for e in events:
        cls = ' class="row-denied"' if _denied(e) else ""
        mark = "拒绝" if _denied(e) else "放行"
        rows.append(
            f"<tr{cls}><td class=num>{e.t:.3f}</td><td>{_entities(e.kind)}</td>"
            f"<td><code>{_entities(e.code)}</code></td><td>{mark}</td>"
            f"<td>{_entities(e.detail)}</td></tr>")
    if not rows:
        rows.append('<tr><td colspan="5">（无事件）</td></tr>')
    return ('<table><thead><tr><th>t / s</th><th>kind</th><th>code</th><th>结论</th>'
            f'<th>detail</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


_CSS = """
:root{--ink:#101418;--dim:#5b6772;--line:#c9d2da;--bg:#fbfcfd;--deny:#b3261e;--ok:#1f6f43;--aux:#eef2f5}
*{box-sizing:border-box}body{margin:0;padding:34px 30px 48px;background:var(--bg);color:var(--ink);
font:14px/1.58 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
h1{font-size:21px;margin:0 0 4px;letter-spacing:.01em}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:18px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.14em;color:var(--dim);
margin:26px 0 10px;font-weight:600}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 6px}
.kpi{border:1px solid var(--line);background:#fff;border-radius:3px;
padding:8px 14px;min-width:100px}
.kpi .k{font-size:10.5px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase}
.kpi .v{font-size:18px;font-variant-numeric:tabular-nums}
.lane{fill:#fff}.lane-alt{fill:var(--aux)}
.lane-label{font:12px "Consolas",monospace;fill:var(--dim)}
.mark{stroke:#fff;stroke-width:1.2}.mark.ok{fill:var(--ok)}.mark.denied{fill:var(--deny)}
.axis,.tick{stroke:var(--line);fill:var(--dim)}.tick{stroke:none;font:11px "Consolas",monospace}
table{border-collapse:collapse;width:100%;font-size:12.5px;background:#fff;
border:1px solid var(--line)}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid #e6ecf1;vertical-align:top}
th{background:var(--aux);font-weight:600;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
td.num{font:12px "Consolas",monospace;color:var(--dim)}
.row-denied td{background:#fdf1f0}
code{font:12px "Consolas",monospace}
.note{border-left:3px solid var(--line);padding:6px 0 6px 12px;color:var(--dim);font-size:12.5px}
.empty{color:var(--dim);font-size:12.5px}
"""


def build_timeline_html(events: Sequence[Event], meta: ReportMeta) -> str:
    """产出**单个自包含** HTML（无外链、无 <script>、无网络请求）。"""
    lanes = _lanes(events)
    denied = sum(1 for e in events if _denied(e))
    note = meta.note or (
        f"共 {len(events)} 条事件，其中被拒 {denied} 条。横轴是设备时钟（不是墙钟）；"
        "同一列上出现多个点表示同一拍内因果同时发生。"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_entities(meta.title)}</title>
<style>{_CSS}</style></head><body>
<h1>{_entities(meta.title)}</h1>
<div class="sub">后端 {_entities(meta.backend)} · {meta.joint_count} 关节 ·
自包含：无外链、无脚本</div>
{_kpi(meta)}
<h2>事件时间线</h2>
{_timeline_svg(events, lanes)}
<h2>事件清单</h2>
{_table(events)}
<h2>怎么读这张图</h2>
<p class="note">{_entities(note)}</p>
</body></html>
"""


def write_timeline(path: str, events: Sequence[Event], meta: ReportMeta) -> str:
    """落盘并返回路径（同内容同字节：便于在测试里比两次生成是否一致）。"""
    text = build_timeline_html(events, meta)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path

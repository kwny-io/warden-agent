"""把「应用对外形状」导出成快照：路由表 + OpenAPI。

给重构用的**证据工具**：拆包前跑一次、拆包后再跑一次，两份文件必须逐字节相同。
路由/字段级别都一样的重构才是安全的；只靠"测试全绿"不够——
测试可能压根没覆盖某个端点的某个响应字段，而 OpenAPI 快照会把它算进去。

用法：
    python scripts/_snapshot_routes.py <输出目录>

（注意：本脚本只会“生成”，**没有**“与基准目录逐字节对比”的模式——想对比就自己 `diff -r`
两个目录。没给目录 / 给了一个 `-` 开头的参数会直接报错退出 2，不会默默建目录。）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import ScriptedModel, weather_tool  # noqa: E402

from warden_agent.model.model import ChatResponse  # noqa: E402
from warden_agent.policy.policy import PolicyEngine  # noqa: E402
from warden_agent.store.sqlite import SqliteStore  # noqa: E402
from warden_agent.web.server import build_app  # noqa: E402


def main(out_dir: str) -> int:
    # 先把参数当参数审一遗：本脚本曾经把 `--check` **当成目录名**（当时想的是
    # “跟基准比一下”，但根本没有这个模式），于是在仓库根下凭空建了一个名叫
    # `--check/` 的目录、里面两份快照——以后的 `git ls-files --others` 里就一直跟着它。
    # 自己“猜到”一个以 `-` 开头的目录名不是容错，是默默干荒唐事：直接拒绝。
    if not out_dir or out_dir.startswith("-"):
        print(
            f"输出目录不合法: {out_dir!r}\n用法：python scripts/_snapshot_routes.py <输出目录>",
            file=sys.stderr,
        )
        return 2
    store = SqliteStore(Path(tempfile.mkdtemp()) / "snap.db")
    app = build_app(
        model=ScriptedModel([ChatResponse(content="ok", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
    )
    routes = [
        {"path": r.path, "methods": sorted(getattr(r, "methods", None) or []), "name": r.name}
        for r in app.routes
    ]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "routes.json").write_text(
        json.dumps(routes, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    (out / "openapi.json").write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    print(f"routes={len(routes)} openapi_paths={len(app.openapi()['paths'])} -> {out}")
    return 0


if __name__ == "__main__":
    # 不再给默认值：没给目录就默认往仓库根写一个快照目录，是个很差的默认。
    if len(sys.argv) != 2:
        print("用法：python scripts/_snapshot_routes.py <输出目录>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))

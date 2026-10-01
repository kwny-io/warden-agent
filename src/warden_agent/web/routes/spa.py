"""SPA 首页与静态产物路径（从 `web/server.py` 机械拆分而来，函数体逐字照搬）。

唯一的非机械改动：`_repo_dir()` 里向上层数从 3 层改成 4 层——文件的物理位置从
`<repo>/src/warden_agent/web/server.py` 变成 `<repo>/src/warden_agent/web/routes/spa.py`，
多一层 `routes/`，所以多上溯一层才能得到同一个仓库根（否则 `web/dist` 会解析到不存在的路径，
首页会从"React 构建产物"退化成内置静态页，`/assets` 挂载也不会发生——那是可观测的行为变化）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from warden_agent.core.settings import env_opt


def _repo_dir() -> str:
    # spa.py 位于 <repo>/src/warden_agent/web/routes/spa.py，向上 4 层回到仓库根
    import os

    here = os.path.dirname(os.path.abspath(__file__))  # .../warden_agent/web/routes
    return os.path.abspath(os.path.join(here, "..", "..", "..", ".."))  # <repo>


def _web_dist_dir() -> str:
    import os

    dist = env_opt("WARDEN_WEB_DIST")
    if dist:
        return dist
    return os.path.join(_repo_dir(), "web", "dist")


def _find_spa_index() -> str | None:
    # 顺序找构建产物：环境变量指定的路径 > 仓库内 web/dist
    p = os.path.join(_web_dist_dir(), "index.html")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    return None


def register(app: FastAPI) -> None:
    """注册 `GET /`（React 构建产物优先，未构建则回退旧演示控制台）。"""

    # 首页：返回可视化演示控制台（HTML），让服务"看得见"。
    # T10 起优先返回 React 构建产物（web/dist）；若未构建则回退到旧版静态 index.html。
    _dist_html: str | None = None

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        html = _find_spa_index()
        if html is None:  # 未构建 React 前端，回退到旧演示控制台
            try:
                from importlib.resources import files

                html = files("warden_agent.web.static").joinpath("index.html").read_text("utf-8")
            except Exception:  # 找不到模板时给个简单占位，不至于 500
                html = "<h1>Warden Agent</h1><p>未找到演示页面。</p>"
        return HTMLResponse(html)

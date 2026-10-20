"""Music Sync Hub 后端。

启动：
    uv run uvicorn app.main:app --host 0.0.0.0 --port 8787
或：
    uv run python -m app.main --port 8787

================================================================================

## 文件布局（2026-10 拆分后）

  app/main.py           ← 你在这里。只剩：app 创建、中间件、异常处理器、
                          lifespan、6 个既有 router 的挂载、静态资源 + 首页、CLI 入口
  app/deps.py           ← **共享运行时状态 + helper**（db / require_writable /
                          封面缓存 / 目录聚合 / 指纹 / 视图转换）。改文件操作的
                          入口逻辑前先看这里。
  app/routes/system.py  ← 系统 / 数据源 / 扫描 / 日志 / 盘符 / 设置
  app/routes/jobs.py    ← 任务中心
  app/routes/library.py ← 曲库浏览 / 封面 / 试听
  app/routes/compare.py ← 对比 / 方案 / 执行 / 查重
  app/routes/files.py   ← 文件浏览 / 跨库传输
  app/*_api.py          ← search / upload / station / tags / mappings（更早拆出去的）
  app/sources/folders_api.py ← 文件夹级操作

**路由函数的函数体与拆分前完全一致**，只是从 `@app.` 换成了 `@router.`。

## 导航索引（路径 → 去哪个文件找）

不要按行号找东西（行号会漂），用下面的索引 + `grep "def 函数名"` 定位。

  ── 应用与异常 ──────────────────────────────────────────────
  lifespan                  启动：装日志、同步数据源、恢复未完成任务
  _source_not_found 404 │ _readonly 403 │ _source_error 400
  _job_busy 409 │ _unhandled 500（带堆栈写日志）

  ── 索引库与只读门禁 ────────────────────────────────────────
  db()                      ★ 全局唯一 DB 入口，带 MSH_DB 热切换语义
  require_writable()        ★ 端点层只读校验（缺失会让"只读源写操作"回 200）
  _db_size()

  ── 系统与数据源 ────────────────────────────────────────────
  GET  /api/system          system_info（含 test_mode、audio_exts）
  GET  /api/source-types    source_types（从注册表推导，不要在前端写第二份）
  GET  /api/sources         list_sources
  GET  /api/sources/status  sources_status      ★ 必须排在 /{sid} 之前
  POST /api/sources         create_source
  PUT  /api/sources/{sid}   update_source
  DEL  /api/sources/{sid}   delete_source
       _validate_source() │ _source_view()

  ── 跨库文件传输 ────────────────────────────────────────────
  POST /api/files/transfer  files_transfer（拒绝非音频；带 transfer_mode）
  POST /api/files/exists    files_exists（批量查存在，上限 5000）
       _reindex_after_transfer()

  ── 日志 / 盘符 / 设备 ──────────────────────────────────────
  GET  /api/logs │ POST /api/logs/clear
  GET  /api/drives │ GET /api/device/probe
       _device_view() │ _connect_tip()

  ── 测试连接 / 浏览 / 扫描 ──────────────────────────────────
  POST /api/sources/{sid}/test     test_source
  POST /api/sources/{sid}/browse   browse
  POST /api/scan/{sid}             start_scan        ★ 独占

  ── 任务队列 ────────────────────────────────────────────────
  GET  /api/jobs │ GET /api/busy │ GET /api/jobs/{jid}
  POST /api/jobs/{jid}/ack
  POST /api/jobs/{jid}/cancel
  POST /api/jobs/{jid}/pause
  POST /api/jobs/{jid}/resume
  POST /api/jobs/{jid}/dismiss  把已结束任务从记录里删掉（忽略）
  POST /api/jobs/clear-finished 一键清理所有已结束任务
  GET  /api/unfinished │ POST /api/unfinished/recover   （崩溃恢复）

  ── 曲库浏览 ────────────────────────────────────────────────
  GET  /api/library/{sid}    library（分页 + 分组 + 排序）
  GET  /api/folders/{sid}    folders
  GET  /api/track/{tid}      track_detail
  POST /api/track/{tid}/refresh │ refresh_tracks()
       _track_view()

  ── 封面与试听 ──────────────────────────────────────────────
  GET  /api/cover/{tid} │ GET /api/cover-raw
       _serve_cover_bytes()  ★ 缓存上限读 Settings.cover_cache_size
  GET  /api/preview/{tid}    preview（HTTP Range 流式）

  ── 双栏对比 → 方案 → 执行 ──────────────────────────────────
  POST /api/compare          compare        → _compare_core()
  POST /api/plan             make_plan      （build_plan + transfer 改写）
  POST /api/execute          execute_plan   ★ 独占
       _tracks_for() │ _tracks_capped()

  ── 查重 ────────────────────────────────────────────────────
  POST /api/duplicates       duplicates     → _duplicates_core()
  POST /api/duplicates/keep  set_keep
       _stable_hash() │ _content_clusters() │ _hash_groups()

  ── 文件浏览（这套接口在 main.py 里，和 upload/station 分开）──────
  GET  /api/files/folders    files_folders   → _folder_map() / _folder_rows()
  GET  /api/files/list       files_list      → _dir_summary()
  GET  /api/state            state_revision  → _disk_view()
  POST /api/files/under      files_under
  POST /api/files/select     files_select
  GET  /api/files/search     files_search
  GET  /api/files/facets     files_facets
  POST /api/files/move       files_move      ★ 独占

  ── 回收站（app/routes/trash.py）────────────────────────────
  GET  /api/trash            trash_list      按日期列出可还原项（含原位置）
  POST /api/trash/restore    trash_restore   ★ 独占 · 还原回原位置，绝不覆盖
  POST /api/trash/purge      trash_purge     ★ 独占 · 永久删除，需 confirm

  ── 设置与重载 ──────────────────────────────────────────────
  GET  /api/settings │ PUT /api/settings
  POST /api/reload           reload_config
  main()                    CLI 入口

## 模块级状态（仅此几处，改前想清楚）

  DB                 由 db() 管理；不要直接赋值绕过它
  _cover_cache       封面内存缓存 + _cover_lock
  _FILE_BROWSE_CACHE 文件浏览缓存（TTL 20 秒）
  WEB_DIR            app/web
  FUZZY_MIN_SCORE    ——

## 姊妹模块

  search_api.py 11 │ upload_api.py 11 │ sources/folders_api.py 8
  station_api.py 7 │ tags_api.py 5 │ mappings_api.py 3

## 改这个文件之前

  先读项目根的 `AGENTS.md`（硬约束、代码地图、验证方法）。
  动文件操作的逻辑时，真正实现都在 `app/core/ops.py`，这里只是入口。
================================================================================
"""
from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (Body, Depends, FastAPI, Header, HTTPException, Query,
                     Request, Response)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from .core import audio as A
from .core import device as DEV
from .core import matching as M
from .core import paths as P
from .core import plan as PLAN
from .core import scanner as S
from .core.config import (AppConfig, Settings, SourceConfig, SourceNotFound,
                          default_db_path, get_secret, load_config,
                          resolve_source, save_config, save_secret, data_dir,
                          test_mode)
from .core.db import Database
from .core.jobs import JobBusy, get_jobs, init as init_jobs, rebind as rebind_jobs
from .core.guard import exclusive_guard
from .core.ops import Executor
from .sources import available_types, manager
from .sources.base import ReadOnlyError, SourceError
from .sources.folders_api import router as folders_router
from .search_api import router as search_router
from .mappings_api import router as mappings_router
from .station_api import router as station_router
from .tags_api import router as tags_router
from .upload_api import router as upload_router

# 拆出去的路由模块。**必须在文件底部才 import** —— 那时 main 的名字已就绪。

WEB_DIR = Path(__file__).resolve().parent / "web"

log = logging.getLogger("msh")

# 这些从 `deps` 转出，保证 `tags_api` / `station_api` / `upload_api` 里
# `_deps().xxx` 的老写法继续可用（它们延迟 import main 取这几个名字）。
from .deps import (  # noqa: E402
    APP_VERSION,
    DB,
    FUZZY_MIN_SCORE,
    _FILE_BROWSE_CACHE,
    _FILE_BROWSE_LOCK,
    _FILE_BROWSE_TTL,
    _browse_cache_clear,
    _connect_tip,
    _content_clusters,
    _cover_cache,
    _cover_lock,
    _db_size,
    _device_view,
    _dir_summary,
    _disk_view,
    _folder_map,
    _folder_rows,
    _hash_groups,
    _reindex_after_transfer,
    _serve_cover_bytes,
    _source_view,
    _stable_hash,
    _step_fingerprint,
    _steps_fingerprint,
    _track_view,
    _tracks_capped,
    _tracks_for,
    _validate_source,
    _wanted_db_path,
    db,
    refresh_tracks,
    require_writable,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时准备好索引库，并把 config.toml 里的数据源同步进 source 表。"""
    # ★ 先把日志装好，之后再有什么问题才留得下来。
    #   以前只有 print()，启动窗口一关就没了，出问题没法回溯。
    try:
        from .core import applog
        p = applog.setup(data_dir())
        if p:
            logging.getLogger("msh").info("启动 Music Sync Hub，日志写到 %s", p)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[startup] 日志初始化失败（不影响使用）: {exc}")
    db()
    cfg = load_config()
    for s_ in cfg.sources:
        db().upsert_source(s_.id, s_.name, s_.type, s_.root)
    # 崩溃恢复：上次如果被强杀/断电，DB 里会留着 state=running 的任务。
    # 不处理的话，界面上会出现一个永远不动的"运行中"任务，
    # 还会一直占着独占位，让所有操作都提示"已有任务在执行"。
    try:
        n = get_jobs().load_unfinished()
        if n:
            logging.getLogger("msh").warning(
                "发现 %d 个上次没做完的任务，可在「操作记录」页尝试接着做完", n)
            print(f"[startup] 发现 {n} 个上次没做完的任务，"
                  f"可在「操作记录」页尝试接着做完")
    except Exception as exc:
        logging.getLogger("msh").warning("恢复上次任务状态失败: %s", exc)
        print(f"[startup] 恢复上次任务状态失败（不影响使用）: {exc}")
    # ★ 索引自动刷新：目录 mtime + 文件指纹探测，只重扫变化的子目录。
    #   没有它，索引只会在手动点扫描时更新 —— 外部程序改了库之后，
    #   界面会一直显示旧状态（删了还看得见、移走了播放失败）。
    try:
        from .core import indexwatch as _iw
        if _iw.start():
            logging.getLogger("msh").info("索引自动刷新已启动")
    except Exception as exc:                                  # noqa: BLE001
        logging.getLogger("msh").warning("索引自动刷新启动失败: %s", exc)
        print(f"[startup] 索引自动刷新启动失败（不影响使用）: {exc}")
    # ★ 启动后台清理过期的 `.mshpart`（进程被强杀时留下的半截临时文件）。
    #   放后台线程：网络盘上全库遍历可能要几十秒，不能挡住启动。
    def _clean_parts() -> None:
        try:
            from .core import ops as _OPS
            from .sources import manager as _mgr
            cfg2 = load_config()
            for s_ in cfg2.sources:
                try:
                    src = _mgr.get(s_.id)
                    if src is not None:
                        _OPS.cleanup_stale_parts(src, older_than_hours=24)
                except Exception:                      # noqa: BLE001
                    continue
        except Exception:                              # noqa: BLE001
            pass

    try:
        threading.Thread(target=_clean_parts, name="msh-clean-parts", daemon=True).start()
    except Exception:                                  # noqa: BLE001
        pass

    yield
    try:
        from .core import indexwatch as _iw2
        _iw2.stop()
    except Exception:                                         # noqa: BLE001
        pass
    manager.close_all()


app = FastAPI(title="Music Sync Hub", version=APP_VERSION,
              description="跨服务器音乐库管理：双栏对比 / 查重查漏 / 安全同步",
              lifespan=lifespan)


# ★ 前端 18 个脚本合计约 558 KB，裸传在首次加载时很慢。
#   GZip 之后约 95 KB（压缩率 ~83%），首屏明显变快。
#   `minimum_size=1000`：小响应（JSON）不压，避免为几百字节付压缩开销。
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(SourceNotFound)
async def _source_not_found(request, exc: SourceNotFound):
    """数据源不存在 → 404，而不是 500。

    为什么要有这个全局兜底：`resolve_source` 原来抛的是裸 `KeyError`，
    FastAPI 不认识它，于是任何带错 id 的请求都会变成"内部服务器错误"。
    更实际的问题是，很多端点是**先占独占位再解析数据源**，
    异常一冒出来独占位就泄漏了。兜底成 404 之后，
    至少用户看到的是"数据源不存在"这种能理解、能自己纠正的错误。
    """
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ReadOnlyError)
async def _readonly(request, exc: ReadOnlyError):
    """只读数据源上的写操作 → 403（而不是 500）。

    `ReadOnlyError` 是 `SourceError` 的子类，必须在 `SourceError` 之前注册，
    否则会被父类的 400 抢走 —— 只读是"权限不够"，不是"参数不对"，
    分开报用户才知道该去取消只读勾选。
    """
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(SourceError)
async def _source_error(request, exc: SourceError):
    """数据源层面的错误 → 400（而不是 500）。

    这一类都是"你给的路径/参数不对"或"源现在连不上"，
    比如路径里带空字符、目录不可达。以前只有个别端点自己包了 try，
    漏掉的就冒到顶层变成 500，用户看到"服务端出错了"，不知道是自己传错了。
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(JobBusy)
async def _job_busy(request, exc: JobBusy):
    """已有独占任务在跑 → 409，而不是 500。

    为什么要有这个全局兜底：`JobBusy` 以前没有任何处理器，谁没包 try
    谁就冒到顶层变成"内部服务器错误"。实测踩到的是
    `/api/execute` 的**预演**路径 —— 它没有 guard、也没包 try/except，
    于是"另一个页面正在写文件"时点一下「预演这批步骤」，
    用户看到的是 HTTP 500（而不是"请等它结束"）。

    兜底之后，任何漏网的 `JobBusy` 都会变成一条读得懂的 409。
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):
    """兜底：未处理的异常**写进日志**（带堆栈），再回一句人话。

    为什么要这个：
      · 以前未处理异常只在控制台打印一次 —— 启动窗口一关就没了，
        用户报"某个操作失败了"时根本查不到。
      · 现在带完整堆栈落到 `logs/msh.log`，界面上也有 `/api/logs` 可看。
      · 回给用户的消息**不暴露堆栈**（可能有路径等隐私），
        但告诉他去哪看日志、以及这是个 bug。
    """
    import traceback as _tb
    logging.getLogger("msh").error(
        "未处理异常 %s %s → %s: %s\n%s",
        request.method, request.url.path,
        type(exc).__name__, exc, _tb.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务端出错（{type(exc).__name__}）。"
                           f"详情已写进日志，可在「设置 → 后台日志」查看。"})


# ★ CORS 不再全开。
#   原来 `allow_origins=["*"]`，而 `/api/execute` 接受客户端自带的步骤 ——
#   本机浏览器里打开的**任何网页**都能跨域调接口改文件。前端走的是同源相对路径，
#   根本不需要 CORS；确实要从别的页面调，用 MSH_CORS_ORIGINS 显式列出（逗号分隔）。
_cors = [o.strip() for o in os.environ.get("MSH_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors:
    app.add_middleware(
        CORSMiddleware, allow_origins=_cors, allow_methods=["*"], allow_headers=["*"],
    )


@app.middleware("http")
async def _reject_cross_site_writes(request, call_next):
    """挡住跨站的写请求（CSRF）。

    只收紧 CORS 还不够：`text/plain` 的"简单请求"不触发预检，浏览器会照发、
    只是不让页面读响应 —— 写操作已经生效了。所以对**改东西的方法**再核一次
    `Origin`：带了 Origin 且不是本服务自己（也不在白名单里）→ 403。
    不带 Origin 的（curl、脚本、同源的部分请求）放行。
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != "null":
            from urllib.parse import urlsplit
            host = request.headers.get("host", "")
            if urlsplit(origin).netloc != host and origin not in _cors:
                return JSONResponse(status_code=403,
                                    content={"detail": "拒绝跨站请求（Origin 不匹配）"})
        elif origin == "null":
            return JSONResponse(status_code=403,
                                content={"detail": "拒绝跨站请求（Origin: null）"})
    return await call_next(request)
# 文件夹级操作（新建/改名/移动/删除）单独放在 sources/folders_api.py，
# 路由在这里挂载，避免 main.py 继续膨胀。
app.include_router(folders_router)
# 搜索 / 集合文件夹 / 智能整理，见 search_api.py
app.include_router(search_router)
# 文件上传 + 按库内结构自动分配，见 upload_api.py
app.include_router(upload_router)
# 元数据（标签 / 封面）编辑，见 tags_api.py
app.include_router(tags_router)
# 自定义映射规则的查看 / 编辑 / 持久化，见 mappings_api.py
app.include_router(mappings_router)
# 中转站（上传后暂存 / 容量限制 / 归位），见 station_api.py
app.include_router(station_router)


# ==================================================================== 拆分出的路由

from .routes import compare as _r_compare      # noqa: E402
from .routes import files as _r_files          # noqa: E402
from .routes import jobs as _r_jobs            # noqa: E402
from .routes import library as _r_library      # noqa: E402
from .routes import system as _r_system        # noqa: E402
from .routes import trash as _r_trash          # noqa: E402

for _r in (_r_system, _r_jobs, _r_library, _r_compare, _r_files, _r_trash):
    app.include_router(_r.router)

# ==================================================================== 前端

if WEB_DIR.exists():
    _static = WEB_DIR / "static"
    if _static.exists():
        app.mount("/static", StaticFiles(directory=str(_static)), name="static")

    @app.get("/")
    def index():
        """首页。

        ★ 测试模式下**服务端注入水印**（而不是靠前端 JS 画）：
          · 前端 JS 可能因为脚本报错没跑起来，水印就没了；
          · 服务端注入的 DOM 在首屏就存在，**第一眼**就能看到"这是测试环境"。
        这段注入只在 `MSH_TEST_MODE=1` 时发生，正常使用完全不受影响。
        """
        p = WEB_DIR / "index.html"
        if not test_mode():
            return FileResponse(str(p))
        html = p.read_text(encoding="utf-8")
        # 水印样式与 DOM 一起插到 </body> 前，避免影响 head 里的加载顺序。
        #
        # ★ 位置在 top:48px（顶栏下方），不是 top:0 —— 顶栏高度 48px，
        #   放 top:0 会盖住导航标签，整排标签点不动。测试包最忌讳这个。
        #   水印条是*整体* pointer-events:none，绝不拦任何点击。
        # 只注入一个角落角标。顶部那条横幅改由前端画 —— 因为它**必须参与布局**
        # （`.topbar` 在 `#app` 里，任何覆盖在顶部的东西都会挡住导航标签，
        #  实测 8 个标签全被挡住）。服务端注入的东西没法安全地挤动布局，
        #  所以这里退回"只管最角落"，顶部交给前端的 `.test-banner`。
        # 角标本身 pointer-events:none，不拦任何点击。
        stamp = """
<!-- ===== 测试模式角标（由服务端注入，MSH_TEST_MODE=1 才有）===== -->
<div id="msh-test-corner" style="position:fixed;right:10px;bottom:96px;z-index:99999;
     pointer-events:none;background:rgba(184,134,11,.92);color:#fff;
     font:700 11px/1.4 'Segoe UI',system-ui,sans-serif;letter-spacing:.08em;
     padding:3px 9px;border-radius:4px;transform:rotate(-8deg);
     box-shadow:0 2px 8px rgba(0,0,0,.4);opacity:.9">TEST</div>
</body>"""
        html = html.replace("</body>", stamp, 1)
        return HTMLResponse(html)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Music Sync Hub")
    parser.add_argument("--host", default=os.environ.get("MSH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MSH_PORT", "8787")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    print(f"\n  Music Sync Hub →  http://{args.host}:{args.port}\n")
    uvicorn.run("app.main:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

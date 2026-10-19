"""元数据编辑（标签 / 封面）。

跟其它写操作一个路子：**先看再改**，而且比搬文件更谨慎 ——
目录搬错了还能搬回来，标签覆盖了原值就没地方找了。

所以这里做三件事：
  1. 写之前把原始标签备份成 JSON（`app/core/tags.py::_save_backup`）；
  2. 原子写：临时文件 → 回读校验 → 再覆盖原文件；
  3. 批量只改**你勾选的那些字段**，没勾的一律不动
     （想给 20 首歌补个"专辑"，不能顺手把它们的标题抹掉）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from .core import paths as P
from .core import tags as T
from .core.config import load_config
from .core.guard import exclusive_guard, exclusive_slot

from .deps import as_list, as_text

router = APIRouter()


def _deps():
    from . import main
    return main


def _resolve(sid: str, path: str):
    """把库内路径解析成 (source, 绝对内部路径, 本地绝对路径)。

    只支持本地数据源 —— 改标签要就地重写文件，远端源要先把整个文件拉下来
    再传回去，风险和工作量都不合适，这里明确拒绝而不是偷偷做半套。
    """
    from .core.config import SourceNotFound, resolve_source
    try:
        resolve_source(sid)
    except (SourceNotFound, KeyError):
        # `SourceNotFound` 是现在的类型；`KeyError` 留着兼容旧调用点
        raise HTTPException(404, f"数据源不存在: {sid}") from None
    src = _deps().manager.get(sid)
    root = P.norm(src.root)
    if not root:
        raise HTTPException(400, "该数据源没有设置根目录")
    p = str(path or "").strip()
    if not p:
        raise HTTPException(400, "缺少 path")
    # ★ 支持源内相对路径（AGENTS.md §2.2 的约定），并用标准工具做越界判定：
    #   裸 startswith 处理不了 Windows 大小写盘符和 `..`。
    try:
        abs_p = src.abs_path(p)
    except Exception as exc:                                  # noqa: BLE001
        raise HTTPException(400, f"路径无法解析: {exc}") from exc
    if not P.inside_root(src.root, abs_p):
        raise HTTPException(400, "路径超出数据源根目录，已拒绝")
    kind = getattr(src, "type", None) or getattr(src.cfg, "type", "")
    if str(kind) != "local":
        raise HTTPException(400, "目前只支持给本地数据源改标签（远端要整份拉回再传回，风险太大）")
    if not src.can_write:
        raise HTTPException(403, f"数据源「{src.cfg.name}」已设为只读，拒绝写入")
    return src, src.rel_path(abs_p), abs_p


@router.get("/api/tags/read")
def tags_read(sid: str, path: str) -> Dict[str, Any]:
    """读一首歌的标签（含封面缩略图，data URL）。"""
    _src, rel, local = _resolve(sid, path)
    if not os.path.isfile(local):
        raise HTTPException(404, f"文件不存在: {path}")
    if not T.can_edit(local):
        raise HTTPException(400, f"这种格式暂不支持读写标签: {os.path.splitext(local)[1]}")
    data = T.read_tags(local)
    return {
        "source": sid, "path": rel, "local": local,
        "name": os.path.basename(local),
        "size": os.path.getsize(local),
        "fields": {k: data.get(k, "") for k in T.FIELDS},
        "labels": T.FIELD_LABELS,
        "cover": data.get("cover"),
        "cover_count": data.get("cover_count", 0),
        "duration": data.get("duration"),
        "bitrate": data.get("bitrate"),
        "codec": data.get("codec"),
        "error": data.get("error"),
        # 封面缩略图给界面用（原图可能好几 MB，不能直接塞进 JSON）
        "cover_thumb": _thumb(data.get("cover")),
    }


def _thumb(data_url: Optional[str], max_size: int = 220) -> Optional[str]:
    if not data_url or not data_url.startswith("data:"):
        return None
    try:
        import base64
        from .core import audio as A
        raw = base64.b64decode(data_url.split(",", 1)[1])
        out, mime, _w, _h = A.make_thumbnail(raw, max_size)
        return "data:%s;base64,%s" % (mime, base64.b64encode(out).decode("ascii"))
    except Exception:
        return None


@router.post("/api/tags/preview")
def tags_preview(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """改之前先看：哪些文件、哪些字段会变、从什么变成什么。"""
    sid = payload.get("source")
    paths: List[str] = [p for p in as_list(payload, "paths", "曲目路径") if p]
    changes: Dict[str, Any] = payload.get("changes") or {}
    if not sid:
        raise HTTPException(400, "缺少 source")
    if not paths:
        raise HTTPException(400, "没有选中任何文件")
    if not changes:
        raise HTTPException(400, "没有要修改的字段")
    bad = [k for k in changes if k not in T.FIELDS]
    if bad:
        raise HTTPException(400, f"不支持的字段: {', '.join(bad)}")

    items = []
    # ★ 超过 500 首必须**报错**，不能悄悄只做前 500 个。
    #   原来这里是 `paths[:500]`：后序文件被丢掉，接口照样返回成功，
    #   用户以为全改完了 —— "看起来成功、实际少做"最难发现。
    if len(paths) > 500:
        raise HTTPException(
            400, f"单次最多处理 500 首（收到 {len(paths)} 个），请分批提交")
    for p in paths:
        try:
            _src, rel, local = _resolve(sid, p)
        except HTTPException as exc:
            items.append({"path": p, "error": exc.detail})
            continue
        if not os.path.isfile(local):
            items.append({"path": p, "error": "文件不存在"})
            continue
        if not T.can_edit(local):
            items.append({"path": p, "error": "格式不支持改标签"})
            continue
        cur = T.read_tags(local)
        diff = {}
        for k, v in changes.items():
            now = str(cur.get(k) or "")
            want = str(v or "")
            diff[k] = {"from": now, "to": want, "changed": now != want}
        items.append({
            "path": p, "name": os.path.basename(local),
            "codec": cur.get("codec"), "diff": diff,
            "will_change": any(d["changed"] for d in diff.values()),
        })
    return {
        "source": sid, "count": len(items),
        "to_change": len([i for i in items if i.get("will_change")]),
        "cannot": len([i for i in items if i.get("error")]),
        "labels": T.FIELD_LABELS,
        "fields": sorted(changes.keys()),
        "items": items,
    }


@router.post("/api/tags/write")
def tags_write(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("tags"))) -> Dict[str, Any]:
    """写入标签。`fields` 列出**允许修改**的字段名，其余忽略。"""
    sid = payload.get("source")
    paths: List[str] = [p for p in as_list(payload, "paths", "曲目路径") if p]
    changes_all: Dict[str, Any] = payload.get("changes") or {}
    if not sid or not paths:
        raise HTTPException(400, "缺少 source / paths")
    if not payload.get("confirm"):
        raise HTTPException(400, "写入标签需要 confirm=true")

    # 只取调用方明确声明的字段，避免"没打算改的字段被空值覆盖"
    allow = payload.get("fields")
    if allow:
        changes = {k: v for k, v in changes_all.items() if k in allow}
    else:
        changes = dict(changes_all)
    if not changes and not payload.get("remove_cover"):
        raise HTTPException(400, "没有要修改的字段")

    cover_bytes = None
    cover_mime = "image/jpeg"
    if payload.get("cover_base64"):
        import base64
        try:
            raw = payload["cover_base64"]
            if raw.startswith("data:"):
                head, raw = raw.split(",", 1)
                if "png" in head:
                    cover_mime = "image/png"
            cover_bytes = base64.b64decode(raw)
        except Exception:
            raise HTTPException(400, "封面数据无法解码")
        if len(cover_bytes) > 12 * 1024 * 1024:
            raise HTTPException(400, "封面图超过 12 MB，请先压缩")

    # 只有这一个源、而它是只读 → 直接 403。
    # 不这样做的话接口会返回 200 + `ok=0 failed=N`，界面容易当成"成功"。
    _deps().require_writable(sid, what="改标签")

    results = []
    ok = fail = 0
    # ★ 超过 500 首必须**报错**，不能悄悄只做前 500 个。
    #   原来这里是 `paths[:500]`：后序文件被丢掉，接口照样返回成功，
    #   用户以为全改完了 —— "看起来成功、实际少做"最难发现。
    if len(paths) > 500:
        raise HTTPException(
            400, f"单次最多处理 500 首（收到 {len(paths)} 个），请分批提交")
    for p in paths:
        try:
            _src, rel, local = _resolve(sid, p)
            res = T.write_tags(
                local, changes,
                cover_data=cover_bytes, cover_mime=cover_mime,
                remove_cover=bool(payload.get("remove_cover")),
                delete_empty=bool(payload.get("delete_empty", True)),
            )
            results.append({"path": p, "state": "done", "backup": res.get("backup"),
                            "applied": res.get("applied")})
            ok += 1
        except Exception as exc:                     # noqa: BLE001
            results.append({"path": p, "state": "error", "detail": str(exc)})
            fail += 1

    # 标签变了，索引里的 title/artist/album 也得跟着更新，否则搜索还是旧的
    if ok and payload.get("refresh_index", True):
        try:
            _deps().refresh_tracks(sid, [r["path"] for r in results
                                         if r["state"] == "done"])
        except Exception:
            pass
    return {"ok": ok, "failed": fail, "results": results,
            "backup_dir": T._backup_dir()}


@router.get("/api/tags/backups")
def tags_backups(limit: int = 100) -> Dict[str, Any]:
    """标签备份列表（改坏了可以还原）。"""
    return {"backups": T.list_backups(int(limit))}


@router.post("/api/tags/restore")
def tags_restore(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("tags"))) -> Dict[str, Any]:
    name = str(payload.get("name") or "")
    if not name:
        raise HTTPException(400, "缺少备份名")
    if not payload.get("confirm"):
        raise HTTPException(400, "还原需要 confirm=true")
    # ★ 先查出还原目标，再判断它属于哪个源 —— 只读源上不允许写回去。
    #   （`restore_backup` 内部是 make_backup=False 的直接覆写，没有其他闸门）
    try:
        target = T.backup_target(name)
    except Exception as exc:                         # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    if target:
        from .core import paths as _P
        for _s in load_config().sources:
            if _P.inside_root(_s.root, _P.norm(target)):
                _deps().require_writable(_s.id, what="还原标签")
                break
    try:
        res = T.restore_backup(name)
    except Exception as exc:                         # noqa: BLE001
        raise HTTPException(400, str(exc))
    try:
        sid = payload.get("source")
        if sid and res.get("path"):
            _deps().refresh_tracks(sid, [res["path"]])
    except Exception:
        pass
    return {"ok": True, **res}

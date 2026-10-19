"""文件夹级操作：新建 / 改名 / 移动 / 删除（进回收站）。

为什么单独一个模块：文件浏览页要能像资源管理器那样管文件夹，而这些操作
有几个共同的安全要点，集中放一处更好审：

* **只在数据源根目录内**：任何 `..`、绝对越界、或指向根目录本身的路径都拒绝；
* **删除不碰文件**：先把整个目录移进 `<根>/.music-sync-trash/日期/`（可恢复），
  再删掉留下的空壳目录；绝不直接递归删除用户内容；
* **改动索引**：改名/移动后同步更新索引里所有后代曲目的 path/parent/rel_path，
  否则界面会继续显示已经不存在的老路径。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..core import paths as P
from ..core.config import SourceNotFound
from ..core.guard import exclusive_guard, exclusive_slot

# 路由挂到主 app 上（见 main.py）
from ..deps import as_text

router = APIRouter()


def _deps():
    """延迟导入，避免与 main 循环依赖。"""
    from .. import main
    return main



def _guarded(fn, what: str):
    """给文件系统调用套上超时。

    坏扇区 / 网络盘掉线 / 被独占 都会让 syscall **永久挂住**：
    既不返回也不报错，HTTP 请求就停在那儿，界面看着像卡死。
    超时后放弃等待并报错，请求能正常返回，用户至少知道发生了什么。
    """
    from ..core.ops import StepTimeout, call_with_timeout
    try:
        return call_with_timeout(fn, what=what)
    except StepTimeout as exc:
        raise HTTPException(504, str(exc))


def _norm_dir(raw: str) -> str:
    if not raw or not str(raw).strip():
        raise HTTPException(400, "缺少目录路径")
    return P.norm(str(raw))


def _inside(sid: str, path: str):
    """解析成 (source 实例, 绝对内部路径, 根路径)，只校验"在根目录范围内"。

    根目录本身是**允许**的：新建目录的 parent、移动的目标父目录都可能是根。
    "不能操作根目录"这条限制只在真正的被操作对象上生效（见 _resolve）。
    """
    m = _deps()
    try:
        sc = m.resolve_source(sid)
    except (SourceNotFound, KeyError):
        # `SourceNotFound` 是现在的类型；`KeyError` 留着兼容旧调用点
        raise HTTPException(404, f"数据源不存在: {sid}") from None
    src = m.manager.get(sid)
    root = P.norm(src.root)
    if not root:
        raise HTTPException(400, "该数据源没有设置根目录")
    abs_path = _norm_dir(path)
    if abs_path in (".", "./"):
        # 空路径 / "." 表示根目录本身（扫描、列举这类操作会用到）
        abs_path = root
    # 拆成段来判越界：直接比较字符串容易漏掉 `..` 这种绕行写法
    parts = [x for x in abs_path.split("/") if x]
    if ".." in parts:
        raise HTTPException(400, "路径不允许包含 ..")
    # ★ 相对路径按"数据源内的相对路径"解释，而不是直接报 400。
    #
    #   原来 `_inside("s1", "Album")` 会走到下面那句"路径超出数据源根目录"
    #   —— 一条**语义上完全合理的相对路径**被当成了越界。
    #   前端一直传绝对路径所以没炸，但任何人拿 API 直接调都会踩到，
    #   而且报错说的是"超出根目录"，跟真实原因（"我只认绝对路径"）
    #   完全不是一回事，排查起来很费劲。
    #
    #   注意顺序：先剥掉可能存在的**本数据源根前缀**，剩下的才当相对路径。
    #   不能无条件拼接，否则已经是绝对路径的输入会被拼成
    #   `D:/Music/D:/Music/x`。
    if abs_path and abs_path != root:
        if not (abs_path == root or abs_path.startswith(root.rstrip("/") + "/")):
            if P.is_windows_abs(abs_path) or abs_path.startswith("/"):
                # 真的是别的盘 / 别的绝对路径 → 仍然拒绝
                raise HTTPException(400, "路径超出数据源根目录，已拒绝")
            # 相对路径 → 挂到根下面
            abs_path = P.join(root, abs_path)
    if not (abs_path == root or abs_path.startswith(root.rstrip("/") + "/")):
        raise HTTPException(400, "路径超出数据源根目录，已拒绝")
    return src, abs_path, root


def _resolve(sid: str, path: str):
    """被操作的**对象**：额外要求它不是根目录本身。"""
    src, abs_path, root = _inside(sid, path)
    if abs_path == root:
        raise HTTPException(400, "这是数据源的根目录，不能对它执行该操作")
    return src, abs_path, root


def _guard_write(src) -> None:
    if not src.can_write:
        raise HTTPException(403, f"数据源「{src.cfg.name}」已设为只读，拒绝写入操作")


def _rel_of(root: str, abs_path: str) -> str:
    r = P.norm(root).rstrip("/")
    return abs_path[len(r) + 1:] if abs_path.startswith(r + "/") else abs_path


def _rewrite_index(sid: str, old_abs: str, new_abs: str, root: str) -> int:
    """把索引里 old_abs 及其所有后代的路径改写到 new_abs 下。返回受影响条数。"""
    m = _deps()
    db = m.db()
    rows = db.query("SELECT id, path FROM track WHERE source_id=?", (sid,))
    old_pre = old_abs.rstrip("/") + "/"
    changed: List[tuple] = []
    for r in rows:
        p = P.norm(r["path"] or "")
        if p == old_abs:
            newp = new_abs
        elif p.startswith(old_pre):
            newp = new_abs.rstrip("/") + "/" + p[len(old_pre):]
        else:
            continue
        changed.append((
            newp, P.parent(newp), P.basename(newp), _rel_of(root, newp),
            sid, r["path"],
        ))
    if not changed:
        return 0
    # 必须走 db.write() 才会 commit，否则界面（其它连接）仍读到旧路径
    with db.write() as conn:
        conn.executemany(
            "UPDATE track SET path=?, parent=?, name=?, rel_path=?"
            " WHERE source_id=? AND path=?",
            changed,
        )
    return len(changed)


@router.post("/api/folders/create")
def create_folder(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("move"))) -> Dict[str, Any]:
    """在 parent 下新建一个子目录。"""
    sid = payload.get("source")
    name = as_text(payload, "name", "文件夹名").strip()
    if not sid:
        raise HTTPException(400, "缺少 source")
    if not name:
        raise HTTPException(400, "缺少目录名")
    # 原来只挡了 \ / : * ? " < > |，CON / NUL / 尾随点空格全能建出来
    _bad = P.name_problem(name)
    if _bad:
        raise HTTPException(400, _bad)
    parent = _norm_dir(as_text(payload, "parent", "上级目录"))
    src, parent_abs, root = _inside(sid, parent)
    _guard_write(src)

    target = P.join(parent_abs, name)
    if not (target == root or target.startswith(root.rstrip("/") + "/")):
        raise HTTPException(400, "目标路径超出数据源根目录，已拒绝")
    if src.exists(target):
        raise HTTPException(409, f"已存在同名目录或文件: {name}")
    try:
        _guarded(lambda: src.mkdirs(target), '新建目录')
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")
    return {"ok": True, "path": target, "rel": _rel_of(root, target), "name": name}


@router.post("/api/folders/rename")
def rename_folder(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("move"))) -> Dict[str, Any]:
    """目录改名（也用于移动到同源的另一个父目录：同时给 new_parent 即可）。"""
    sid = payload.get("source")
    path = payload.get("path")
    new_name = as_text(payload, "name", "文件夹名").strip()
    if not sid or not path:
        raise HTTPException(400, "缺少 source / path")
    if not new_name:
        raise HTTPException(400, "缺少新目录名")
    # 改名成 nul 会让 shutil.move 逐个文件 WinError 3、搬到一半停住
    _bad = P.name_problem(new_name)
    if _bad:
        raise HTTPException(400, _bad)

    src, cur_abs, root = _resolve(sid, path)
    _guard_write(src)
    if not src.exists(cur_abs):
        raise HTTPException(404, f"目录不存在: {path}")

    parent = payload.get("new_parent")
    if parent:
        _s2, parent_abs, _r2 = _inside(sid, parent)
        # 不允许把目录移动到它自己的子孙里（会形成自引用）
        if parent_abs == cur_abs or parent_abs.startswith(cur_abs.rstrip("/") + "/"):
            raise HTTPException(400, "不能把目录移动到它自己的子目录里")
    else:
        parent_abs = P.parent(cur_abs)
    target = P.join(parent_abs, new_name)
    if target == cur_abs:
        return {"ok": True, "path": target, "rel": _rel_of(root, target),
                "moved": False, "indexed": 0}
    if not (target == root or target.startswith(root.rstrip("/") + "/")):
        raise HTTPException(400, "目标路径超出数据源根目录，已拒绝")
    # 只改大小写（Album → album）时"已存在"的就是它自己，不算冲突
    if src.exists(target) and not src.same_entry(cur_abs, target):
        raise HTTPException(409, f"目标已存在: {new_name}")

    try:
        if hasattr(src, "rename_dir"):
            _guarded(lambda: src.rename_dir(cur_abs, target), '重命名')
        else:
            src.replace(cur_abs, target)
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    n = _rewrite_index(sid, cur_abs, target, root)
    return {"ok": True, "path": target, "rel": _rel_of(root, target),
            "moved": True, "indexed": n}


@router.post("/api/folders/delete")
def delete_folder(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("trash"))) -> Dict[str, Any]:
    """删除目录：默认移入回收站（可恢复）；hard=true 才真删（需二次确认）。"""
    sid = payload.get("source")
    path = payload.get("path")
    if not sid or not path:
        raise HTTPException(400, "缺少 source / path")
    src, abs_path, root = _resolve(sid, path)
    _guard_write(src)
    if not src.exists(abs_path):
        raise HTTPException(404, f"目录不存在: {path}")

    # 先看里面有什么，返回给界面做确认提示
    st = src.dir_stat(abs_path) if hasattr(src, "dir_stat") else {
        "files": 0, "dirs": 0, "bytes": 0, "audio": 0}
    hard = bool(payload.get("hard"))
    if hard and not payload.get("confirm_hard"):
        raise HTTPException(400, "永久删除需要 confirm_hard=true")

    m = _deps()
    try:
        if hard:
            _guarded(lambda: src.remove_dir(abs_path), '删除目录')
            trash = ""
            # ★ `remove_dir` 底层是 `shutil.rmtree(ignore_errors=True)`：
            #   文件被别的程序独占时会**静默跳过**。这时目录还在，却把索引
            #   全删了 —— 磁盘留下幽灵文件，而且再也无法通过索引管理。
            try:
                _still = src.stat(abs_path) is not None
            except Exception:                                 # noqa: BLE001
                _still = False
            if _still:
                raise HTTPException(
                    400, "目录里还有文件没能删除（可能正被其它程序占用）。"
                         "索引已保留 —— 请关闭占用它的程序后重试。")
        else:
            if not hasattr(src, "trash_dir"):
                raise HTTPException(400, "该数据源不支持把目录移入回收站")
            trash = src.trash_dir(abs_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    # 索引里这些曲目已经不在原位置了
    db = m.db()
    with db.write() as conn:
        # ★ 不能用 LIKE：`_` 是单字符通配符，删 `Top_100` 会连
        #   `Top-100/` 的索引一起抹掉。
        _base = abs_path.rstrip("/")
        conn.execute(
            "DELETE FROM track WHERE source_id=?"
            " AND (path=? OR SUBSTR(path, 1, ?) = ?)",
            (sid, _base, len(_base) + 1, _base + "/"),
        )
    return {"ok": True, "hard": hard, "trash": trash or None, "removed": st,
            "rel": _rel_of(root, abs_path)}


@router.get("/api/folders/info")
def folder_info(sid: str, path: str = "") -> Dict[str, Any]:
    """删除/移动前的信息：里面有多少文件、多少子目录、多大。"""
    src, abs_path, root = _resolve(sid, path)
    st = src.dir_stat(abs_path) if hasattr(src, "dir_stat") else {
        "files": 0, "dirs": 0, "bytes": 0, "audio": 0}
    st["path"] = abs_path
    st["rel"] = _rel_of(root, abs_path)
    st["name"] = P.basename(abs_path)
    st["read_only"] = not src.can_write
    return st


@router.get("/api/folders/files")
def folder_files(sid: str, path: str = "") -> Dict[str, Any]:
    """列出目录下**所有层级**的音频文件（含子目录），供"解散"时逐个指定去向。"""
    src, abs_path, root = _resolve(sid, path)
    out: List[Dict[str, Any]] = []
    try:
        for e in src.walk(abs_path):
            if P.is_audio(e.name):
                out.append({
                    "did": f"{sid}::{P.norm(e.path)}",
                    "path": P.norm(e.path),
                    "name": e.name,
                    "rel": _rel_of(abs_path, P.norm(e.path)),
                    "size": e.size or 0,
                    "mtime": e.mtime or 0,
                })
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")
    out.sort(key=lambda x: x["rel"].lower())
    return {"source": sid, "path": abs_path, "rel": _rel_of(root, abs_path),
            "parent": P.parent(abs_path), "root": root, "count": len(out),
            "files": out}


@router.post("/api/folders/dissolve")
def dissolve_folder_removed(job=Depends(exclusive_slot("trash"))) -> Dict[str, Any]:
    """已移除。

    "解散文件夹"要求为**每个文件**单独指定去向，为了清几个空壳目录
    要用户做十几步判断，操作极重。已用
    `POST /api/folders/empty-remove`（一键清空空文件夹）取代。
    保留这个路由是为了让可能存在的旧前端拿到明确提示，而不是 404。
    """
    raise HTTPException(
        410, "「解散文件夹」已移除，请改用「清理空文件夹」"
             "（/api/folders/empty-scan + /api/folders/empty-remove）")

# ============================================================ 一键清空空文件夹
# 取代原来的"解散文件夹"：那个功能要求用户为**每个文件**指定去向，
# 操作极重；而实际需求往往只是"把扫出来的空目录壳清掉"。
# 现在的做法：先扫描列出所有空文件夹，用户勾掉要保留的，再一次性删除。

def _is_empty_dir(src, path: str) -> bool:
    """目录里既没有文件、也没有非空子目录才算空。"""
    try:
        entries = list(src.listdir(path))
    except Exception:
        return False
    for e in entries:
        if not e.is_dir:
            return False
        if not _is_empty_dir(src, P.norm(e.path)):
            return False
    return True


@router.get("/api/folders/empty-scan")
def empty_scan(sid: str, start: str = "") -> Dict[str, Any]:
    """列出指定范围里所有（递归意义上的）空文件夹。

    `start` 留空就从数据源根目录扫。
    """
    # 扫描允许从根目录开始，所以用 `_inside`（它不禁止根）而不是 `_resolve`。
    # 注意 `_inside(sid, "")` 会被判成"缺少路径"，所以先解析一次拿 root，
    # 再按需解析子路径。
    src, _abs0, root = _inside(sid, ".")
    abs_start = _inside(sid, start)[1] if start else root
    found: List[Dict[str, Any]] = []
    base = P.norm(abs_start).rstrip("/")
    stack = [base]
    seen = 0
    while stack:
        cur = stack.pop()
        seen += 1
        if seen > 20000:            # 防御：目录异常多时别把请求拖死
            break
        # 只在指定范围内统计：范围外的目录不该出现在结果里
        if not (cur == base or cur.startswith(base + "/")):
            continue
        try:
            entries = list(src.listdir(cur))
        except Exception:
            continue
        for e in entries:
            if e.is_dir:
                stack.append(P.norm(e.path))
        if cur != base and _is_empty_dir(src, cur):
            found.append({
                "path": P.norm(cur),
                "rel": _rel_of(root, cur),
                "name": P.basename(cur),
                "parent": _rel_of(root, P.parent(cur)),
                "depth": _rel_of(root, cur).count("/") + 1,
            })
    # 浅的排前面，方便用户先看大结构的空壳
    found.sort(key=lambda x: (x["depth"], x["rel"]))
    return {"source": sid, "root": root, "start": _rel_of(root, base),
            "count": len(found), "folders": found}


@router.post("/api/folders/empty-remove")
def empty_remove(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("trash"))) -> Dict[str, Any]:
    """删除选中的空文件夹。

    `paths`  要删除的（相对或绝对）目录列表；**界面传的是"勾掉保留之后剩下的"**
    `confirm` 必须为 true —— 这是不可逆操作
    只删**确认为空**的目录，任何非空目录一律跳过并如实报告，
    避免用户勾错了导致文件被连带删除。
    """
    sid = str(payload.get("source") or "")
    paths = payload.get("paths") or []
    if not sid:
        raise HTTPException(400, "缺少 source")
    if not payload.get("confirm"):
        raise HTTPException(400, "删除空文件夹需要 confirm=true")
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, "没有要删除的目录")
    src, _abs0, root = _inside(sid, ".")
    # ★ 只读源上必须直接 403。原来漏了这一句：底层 ReadOnlyError 被当成
    #   "跳过"收集，接口返回 200 {"ok": true} —— 只读保护的假成功。
    _guard_write(src)
    targets: List[str] = []
    for p in paths:
        try:
            # `_inside` 返回 (src, 绝对路径, root)，这里只要绝对路径
            targets.append(_inside(sid, str(p))[1])
        except HTTPException:
            continue
    if not targets:
        raise HTTPException(400, "没有可用的目录路径（可能都越界了）")
    # 先深后浅，否则删了父目录、子目录的路径就失效了
    targets = sorted(set(targets), key=lambda x: -x.count("/"))

    removed: List[str] = []
    skipped: List[Dict[str, str]] = []
    for d in targets:
        if not _is_empty_dir(src, d):
            skipped.append({"path": _rel_of(root, d), "reason": "目录非空，已跳过"})
            continue
        try:
            ok = _guarded(
                lambda: (src.remove_empty_dir(d)
                         if hasattr(src, "remove_empty_dir") else False),
                '删除空目录')
        except Exception as exc:
            ok = False
            skipped.append({"path": _rel_of(root, d), "reason": str(exc)})
            continue
        if ok:
            removed.append(_rel_of(root, d))
        else:
            skipped.append({"path": _rel_of(root, d), "reason": "删除失败或被占用"})

    return {"ok": True, "removed": removed, "removed_count": len(removed),
            "skipped": skipped, "skipped_count": len(skipped)}

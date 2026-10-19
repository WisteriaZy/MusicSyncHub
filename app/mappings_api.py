"""自定义映射规则的存储与接口。

映射是**长期偏好**，要能查看、编辑、记住 —— 之前只存在于一次会话的内存里，
关掉窗口就没了，用户每次都得重新建，等于没这个功能。

存到配置目录下的 `mappings.json`，按数据源分组：
    {"win-music": {"周杰伦": "华语/周杰伦", "__sink__": "0单曲"}}
选文件而不是塞进 config.toml，是因为它是纯运行时数据、量可能不小，
而且读写频繁（每次整理都会碰），不该让主配置反复重写。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from .core.guard import exclusive_slot

router = APIRouter()

_LOCK = threading.RLock()


def _data_dir() -> str:
    """应用数据目录。

    ★ 必须走 `config.data_dir()`，**不能**自己再写一遍
    `MSH_DATA_DIR or ~/.music-sync-hub`。

    自己写的那份少了 `data_dir()` 里的两层保护，于是同样会踩那个
    "空窗期"坑：`MSH_DATA_DIR` 被 pop 掉之后，映射文件会写到
    **用户真实的数据目录**。config.toml 已经因为这个问题被
    测试覆盖过一次（两个真实数据源全丢），mappings.json 是同一个坑，
    只是还没发作 —— 映射规则丢了用户是看不出来的，只会觉得
    "我明明配过，怎么又要重配"。

    统一走 `data_dir()`，测试进程里的兜底才会生效。
    """
    from .core.config import data_dir
    return str(data_dir())


def _path() -> str:
    return os.path.join(_data_dir(), "mappings.json")


def _load_all() -> Dict[str, Any]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 文件坏了不该让整个功能挂掉，当成空的
        return {}


def _save_all(d: Dict[str, Any]) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def get_mappings(sid: str) -> Dict[str, str]:
    """取某个数据源的映射（供整理逻辑调用）。"""
    if not sid:
        return {}
    with _LOCK:
        d = _load_all().get(sid) or {}
    return {str(k): str(v) for k, v in d.items()
            if str(k).strip() and str(v).strip() and str(k) != "_updated"}


@router.get("/api/mappings")
def list_mappings(sid: str = "") -> Dict[str, Any]:
    """列出映射规则。不带 sid 就返回全部数据源的。"""
    with _LOCK:
        d = _load_all()
    if sid:
        return {"source": sid, "rules": get_mappings(sid),
                "updated": (d.get(sid) or {}).get("_updated")}
    out = {k: {kk: vv for kk, vv in (v or {}).items() if kk != "_updated"}
           for k, v in d.items()}
    return {"sources": out}


@router.put("/api/mappings")
def put_mappings(payload: Dict[str, Any] = Body(...),
                 _job=Depends(exclusive_slot("mappings"))) -> Dict[str, Any]:
    """整体替换某个数据源的映射（界面是"编辑后保存"的模式）。"""
    sid = str(payload.get("source") or "").strip()
    if not sid:
        raise HTTPException(400, "缺少 source")
    rules = payload.get("rules")
    if not isinstance(rules, dict):
        raise HTTPException(400, "rules 必须是对象（键=专辑/作者，值=目录）")
    clean: Dict[str, str] = {}
    for k, v in rules.items():
        k2, v2 = str(k).strip(), str(v).strip().strip("/")
        if k2 and v2:
            clean[k2] = v2
    with _LOCK:
        d = _load_all()
        if clean:
            clean["_updated"] = time.time()
            d[sid] = clean
        else:
            d.pop(sid, None)          # 清空了就把这个源整条删掉
        _save_all(d)
    return {"source": sid, "rules": get_mappings(sid), "count": len(clean) - 1
            if "_updated" in clean else len(clean)}


@router.post("/api/mappings/one")
def put_one(payload: Dict[str, Any] = Body(...),
            _job=Depends(exclusive_slot("mappings"))) -> Dict[str, Any]:
    """加/改一条规则（整理向导里"给这一组建映射"直接用这个）。"""
    sid = str(payload.get("source") or "").strip()
    key = str(payload.get("key") or "").strip()
    dst = str(payload.get("dst") or "").strip().strip("/")
    if not sid or not key:
        raise HTTPException(400, "缺少 source 或 key")
    with _LOCK:
        d = _load_all()
        cur = {k: v for k, v in (d.get(sid) or {}).items() if k != "_updated"}
        if dst:
            cur[key] = dst
        else:
            cur.pop(key, None)        # dst 为空 = 删除这条规则
        if cur:
            cur["_updated"] = time.time()
            d[sid] = cur
        else:
            d.pop(sid, None)
        _save_all(d)
    return {"source": sid, "key": key, "dst": dst,
            "rules": get_mappings(sid)}

"""任何端点都不能因为"代码写错"返回 500。

## 为什么需要这条

拆分 `app/main.py` 时，`set_keep()` 被整段重写，把 `out = []` 弄丢了 ——
`POST /api/duplicates/keep` 必然 NameError 500。

**当时 758 项单测 + 90 个接口冒烟全绿**，因为这个端点一处都没被覆盖。
是 `tools/audit_split.py` 的 symtable 检查抓到的。

这条测试从另一侧兜住同一类问题：**把每个端点都用最小/空参数打一遍**，
只要返回 500 就红。400/404/409/422 都是正常业务拒绝，不算问题。

意图不是"测功能"，而是"测**代码路径能走通**" —— 参数不合法可以报错，
但不能因为引用了不存在的名字、导入了不存在的模块而崩。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent

# 这些端点故意不自动调用：
#   · 会真的动文件（且空参数下行为不明确）
#   · 或者会长时间阻塞
SKIP = {
    "/api/execute",          # 空 steps 会 400，但保险起见不自动跑写路径
    "/api/files/move",
    "/api/files/transfer",
    "/api/upload/commit",
    "/api/upload/cleanup",
    "/api/station/move-in",
    "/api/station/move-out",
    "/api/station/restore",
    "/api/station/remove",
    "/api/folders/delete",
    "/api/folders/empty-remove",
    "/api/folders/dissolve",
    "/api/tags/write",
    "/api/tags/restore",
    "/api/organize/apply",
    "/api/organize/album-apply",
    "/api/collect/apply",
    "/api/preview/{tid}",    # 流式响应，TestClient 下会挂住
    "/api/cover/{tid}",
    "/api/cover-raw",
}


class NoServerErrorTest(unittest.TestCase):
    """/api 下的端点：空参数不许 500。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-500-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from fastapi.routing import APIRoute
        from fastapi.testclient import TestClient
        import app.main as M
        cls.client = TestClient(M.app, raise_server_exceptions=False)

        def walk(rs, seen=None):
            seen = seen or set()
            for r in rs:
                if id(r) in seen:
                    continue
                seen.add(id(r))
                if isinstance(r, APIRoute):
                    yield r
                    continue
                inner = getattr(r, "original_router", None) or getattr(r, "router", None)
                if inner is not None and hasattr(inner, "routes"):
                    yield from walk(inner.routes, seen)

        cls.routes = [r for r in walk(M.app.routes)
                      if r.path.startswith("/api/") and r.path not in SKIP]

    @classmethod
    def tearDownClass(cls):
        try:
            import app.deps as D
            if getattr(D, "DB", None) is not None:
                try:
                    D.DB.close_all()
                except Exception:
                    pass
                D.DB = None
        except Exception:
            pass
        try:
            from app.core.db import Database
            Database.close_all()
        except Exception:
            pass
        try:
            from app.core import jobs as J
            J.reset_for_tests()
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls._tmp.cleanup()

    def test_no_endpoint_returns_500_on_empty_input(self):
        bad = []
        for r in self.routes:
            if not r.methods:
                continue
            for method in sorted(r.methods):
                if method in ("HEAD", "OPTIONS"):
                    continue
                path = r.path
                # 把路径参数填成占位值，让它走到业务逻辑而不是 422
                for seg in [s for s in path.split("/") if s.startswith("{")]:
                    path = path.replace(seg, "1")
                try:
                    resp = self.client.request(
                        method, path, json={} if method in ("POST", "PUT", "PATCH") else None)
                except Exception as exc:            # noqa: BLE001
                    bad.append(f"{method} {r.path} 抛异常 {type(exc).__name__}: {exc}")
                    continue
                if resp.status_code >= 500:
                    bad.append(f"{method} {r.path} → {resp.status_code} "
                               f"{resp.text[:160]}")
        self.assertEqual([], bad,
                         "这些端点在空参数下返回 500（多半是引用了不存在的名字 / 模块）：\n  "
                         + "\n  ".join(bad))

    def test_known_keep_endpoint_works(self):
        """`/api/duplicates/keep` 曾经因为没有 `out = []` 而必然 500。

        它当时没有任何覆盖，所以专门锁一条。
        """
        r = self.client.post("/api/duplicates/keep", json={"groups": []})
        self.assertEqual(200, r.status_code, r.text[:300])
        self.assertIn("selections", r.json())
        # 有内容时也要能算（用 did 作为标识符，和 /api/duplicates 一致）
        r2 = self.client.post("/api/duplicates/keep", json={
            "policy": "oldest",
            "groups": [{"key": "k1", "items": [
                {"did": "s::a.mp3", "path": "/s/a.mp3", "size": 10, "mtime": 2},
                {"did": "s::b.mp3", "path": "/s/b.mp3", "size": 20, "mtime": 1},
            ]}],
        })
        self.assertEqual(200, r2.status_code, r2.text[:300])
        sel = r2.json()["selections"][0]
        self.assertEqual("s::b.mp3", sel["keep"], "oldest 策略应按 mtime 最小挑")
        self.assertEqual(["s::a.mp3"], sel["trash"])


if __name__ == "__main__":
    unittest.main()

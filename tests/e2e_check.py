"""端到端联调测试：直接打真实 HTTP 接口，验证整条链路。

配套夹具（tests/make_sample_libs.py）构造了结果唯一确定的场景：
    win 10 首 / linux 7 首
    对比：matched = 6，only_left = 4，only_right = 1
    左➜右补齐后：两边各 11 首，一一对应

前置（推荐用 scripts/dev_reset.sh 一键完成）：
    1. 生成样例库：  uv run python tests/make_sample_libs.py /tmp/msh-e2e
    2. 启动服务：    MSH_DATA_DIR=/tmp/msh-e2e-data uv run python -m uvicorn app.main:app --port 8791

运行：
    uv run python tests/e2e_check.py --base http://127.0.0.1:8791
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8791"
WIN_ROOT = "/tmp/msh-e2e/win/Music"
LIN_ROOT = "/tmp/msh-e2e/linux/music"
PASS, FAIL = 0, 0


def call(method: str, path: str, body=None, timeout=180):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("content-type", "")
            if "json" in ctype:
                return r.status, (json.loads(raw) if raw else None)
            return r.status, {"bytes": len(raw), "content_type": ctype}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode(errors="replace")[:400]}


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def parent_of(p: str) -> str:
    p = str(p or "").replace("\\", "/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def wait_job(job_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, j = call("GET", f"/api/jobs/{job_id}")
        if j and j.get("state") != "running":
            return j
        time.sleep(0.5)
    return {"state": "timeout"}


def scan(sid, **opts):
    st, r = call("POST", f"/api/scan/{sid}", {"workers": 4, **opts})
    return wait_job(r["job"]["id"])


def audio_count(root: str) -> int:
    n = 0
    for _r, _d, fs in os.walk(root):
        n += sum(1 for f in fs if f.lower().endswith((".wav", ".mp3", ".flac", ".m4a")))
    return n


def has_trash(root: str) -> bool:
    return any(".music-sync-trash" in r for r, _d, _f in os.walk(root))


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    # ------------------------------------------------------------ 0
    print("\n=== 0. 环境自检（防止测试互相污染）===")
    wc, lc = audio_count(WIN_ROOT), audio_count(LIN_ROOT)
    clean = (wc == 10 and lc == 7 and not has_trash(WIN_ROOT) and not has_trash(LIN_ROOT))
    check(f"样例库处于干净的初始状态（win={wc} / linux={lc}）", clean,
          "环境不干净，请先执行 scripts/dev_reset.sh 重新初始化")
    if not clean:
        print("\n  ⚠ 请按顺序重跑：\n"
              "     bash scripts/dev_reset.sh          # 清库 + 重启服务 + 重生成样例\n"
              "     uv run python tests/e2e_check.py\n")
        return 2

    print("\n=== 1. 系统 & 数据源 ===")
    st, sysinfo = call("GET", "/api/system")
    check("GET /api/system", st == 200 and sysinfo.get("app") == "Music Sync Hub", str(st))
    check("元数据引擎可用（mutagen）", bool(sysinfo["capabilities"]["mutagen"]))
    check("图片处理可用（Pillow）", bool(sysinfo["capabilities"]["pillow"]))
    st, srcs = call("GET", "/api/sources")
    check("配置里读到 2 个数据源", st == 200 and len(srcs) == 2, str([s["id"] for s in srcs or []]))

    print("\n=== 2. 连接测试 ===")
    for sid in ("win", "linux"):
        st, r = call("POST", f"/api/sources/{sid}/test")
        check(f"{sid} 连接测试通过", bool(r and r.get("ok")), str(r and r.get("error")))

    print("\n=== 3. 扫描建索引 ===")
    for sid, want in (("win", 10), ("linux", 7)):
        j = scan(sid)
        res = j.get("result") or {}
        check(f"{sid} 扫描完成且发现 {want} 个音频文件",
              j.get("state") == "done" and res.get("seen") == want,
              f"state={j.get('state')} seen={res.get('seen')} err={j.get('error')}")
        print(f"     解析 {res.get('parsed')} · 缓存 {res.get('cached')} · 失败 {res.get('errors')} "
              f"· 封面文件 {res.get('covers')} · 耗时 {res.get('duration')}s")

    print("\n=== 4. 曲库浏览 / 元数据 / 封面 ===")
    st, lib = call("GET", "/api/library/win?limit=100")
    check("曲库列表可用", st == 200 and lib["total"] == 10, str(lib.get("total")))
    wavs = [t for t in lib["tracks"] if t["name"].endswith(".wav")]
    sample = next((t for t in wavs if "Adele" in t["name"]), wavs[0])
    check("解析出时长（WAV 头部）", bool(sample.get("duration")), str(sample.get("duration")))
    check("解析出采样率/声道", bool(sample.get("sample_rate")) and bool(sample.get("channels")))
    check("识别为无损格式", bool(sample.get("is_lossless")))
    check("文件名推断出作者/标题",
          sample.get("artist") == "Adele" and sample.get("title") == "Hello",
          str((sample.get("artist"), sample.get("title"))))
    check("识别到同目录封面（cover.jpg）", any(t.get("has_cover") for t in lib["tracks"]))
    bad = next((t for t in lib["tracks"] if "corrupt" in t["name"]), None)
    check("坏文件不中断扫描且被标记为解析异常",
          bool(bad and bad.get("meta_status") == "error"), str(bad and bad.get("meta_status")))
    st, folders = call("GET", "/api/folders/win")
    check("目录树接口可用", st == 200 and len(folders["folders"]) >= 4, str(len(folders.get("folders", []))))
    st, det = call("GET", f"/api/track/{sample['id']}")
    check("曲目详情可用且带流地址", st == 200 and det.get("stream_url"), str(st))

    print("\n=== 5. 封面 / 预览接口 ===")
    cov = next((t for t in lib["tracks"] if t.get("has_cover")), None)
    st, c = call("GET", f"/api/cover/{cov['id']}?size=96") if cov else (0, {})
    check("封面可读取（返回图片字节）", st == 200 and (c or {}).get("bytes", 0) > 0, str(c))
    st, pv = call("GET", f"/api/preview/{sample['id']}")
    check("试听接口可用（Range 流）", st == 200 and (pv or {}).get("bytes", 0) > 0, str(pv))

    print("\n=== 6. 双栏对比 ===")
    st, cmp = call("POST", "/api/compare", {"left": "win", "right": "linux"})
    s = (cmp or {}).get("stats", {})
    print("     " + json.dumps({k: s.get(k) for k in ("left_total", "right_total", "matched",
                                                      "only_left", "only_right")}, ensure_ascii=False))
    check("匹配到 6 对（5 首同名 + 1 首改名同歌）", s.get("matched") == 6, str(s.get("matched")))
    check("Windows 独有 4 首（多余副本 + 低音质版 + 独有曲 + 坏文件）",
          s.get("only_left") == 4, str(s.get("only_left")))
    check("Linux 独有 1 首", s.get("only_right") == 1, str(s.get("only_right")))
    renamed = next((m for m in cmp["matches"] if "Renamed" in (m["left"].get("name") or "")), None)
    check("文件名不同的同一首歌也能配对",
          bool(renamed and renamed["left"]["name"] != renamed["right"]["name"]),
          json.dumps(renamed and [renamed["left"]["name"], renamed["right"]["name"]], ensure_ascii=False))
    check("配对结果带判定依据与置信度",
          bool(renamed and renamed.get("reasons") and renamed.get("confidence")))
    check("同歌的配对置信度足够高", bool(renamed and renamed["score"] >= 0.85),
          str(renamed and renamed["score"]))
    unpaired = {t["name"] for t in cmp["only_left"]} | {t["name"] for t in cmp["only_right"]}
    check("没有把两首不同的独有曲误配成一对",
          "Queen - Bohemian Rhapsody.wav" in unpaired and "Miles Davis - So What.wav" in unpaired,
          str(sorted(unpaired)))

    print("\n=== 7. 同步计划（左➜右补齐，预演）===")
    st, plan = call("POST", "/api/plan", {"left": "win", "right": "linux",
                                          "direction": "left_to_right", "copy_missing": True})
    copies = [x for x in plan["steps"] if x["op"] == "copy"]
    check("左➜右计划包含 4 个复制步骤", len(copies) == 4, str(len(copies)))
    check("复制目标都是 linux 源", all(x["dst"] == "linux" for x in copies))
    check("保留了原有目录结构（Rock / Dup / Broken）",
          {parent_of(x["dst_path"]) for x in copies} >= {"Rock", "Dup", "Broken"},
          str(sorted({parent_of(x["dst_path"]) for x in copies})))
    check("每个步骤都有风险等级与原因", all(x.get("risk") and x.get("reason") for x in plan["steps"]))
    check("没有高危步骤（纯复制）", plan["summary"]["high_risk"] == 0, str(plan["summary"]))

    st, dry = call("POST", "/api/execute", {"steps": plan["steps"], "execute": False})
    check("预演不写入（dry-run 只回计划）",
          st == 200 and dry.get("dry_run") is True and dry["summary"].get("planned") == 4,
          json.dumps(dry.get("summary"), ensure_ascii=False))
    check("预演确实没有产生文件", audio_count(LIN_ROOT) == 7, str(audio_count(LIN_ROOT)))

    print("\n=== 8. 真正执行补齐（写路径 + 校验）===")
    st, ex = call("POST", "/api/execute", {"steps": plan["steps"], "execute": True,
                                           "confirm": True, "policy": "skip", "verify": True})
    summ = ex.get("summary") or {}
    check("4 个步骤全部安全处理（成功或按策略跳过），零失败",
          summ.get("done", 0) + summ.get("skipped", 0) == 4
          and summ.get("error", 0) == 0 and summ.get("blocked", 0) == 0,
          json.dumps(summ, ensure_ascii=False))
    check("已存在的同名文件按 skip 策略跳过（不覆盖、不重复写）",
          summ.get("skipped") == 1, json.dumps(summ, ensure_ascii=False))
    check("每个复制结果都做了完整性校验",
          all(r.get("verified") for r in ex["results"] if r.get("op") == "copy" and r["state"] == "done"),
          json.dumps(ex["results"][:2], ensure_ascii=False))
    steps_by_id = {x["id"]: x for x in plan["steps"]}
    ok_all = True
    for r in ex["results"]:
        step = steps_by_id.get(r["step"])
        if not step:
            ok_all = False
            continue
        src_abs = os.path.join(WIN_ROOT, step["src_path"].split("Music/", 1)[-1])
        dst_abs = os.path.join(LIN_ROOT, step["dst_path"])
        import filecmp
        if not (os.path.exists(src_abs) and os.path.exists(dst_abs)
                and filecmp.cmp(src_abs, dst_abs, shallow=False)):
            ok_all = False
            print(f"        不一致: {step['dst_path']}")
    check("落盘文件与源文件字节完全一致", ok_all)
    check("没有残留 .mshpart 临时文件",
          not any("mshpart" in f for _r, _d, fs in os.walk(LIN_ROOT) for f in fs))

    print("\n=== 9. 重新扫描并核对补齐结果 ===")
    for sid in ("win", "linux"):
        j = scan(sid)
        check(f"{sid} 增量扫描完成", j.get("state") == "done", str(j.get("error")))
    st, cmp2 = call("POST", "/api/compare", {"left": "win", "right": "linux"})
    s2 = cmp2["stats"]
    print("     " + json.dumps({k: s2.get(k) for k in ("left_total", "right_total", "matched",
                                                      "only_left", "only_right")}, ensure_ascii=False))
    check("Linux 侧曲目数增加（7 → 10）", s2.get("right_total") == 10, str(s2.get("right_total")))
    check("两边总数一致（各 10 首）",
          s2.get("left_total") == s2.get("right_total") == 10,
          f"{s2.get('left_total')}/{s2.get('right_total')}")
    check("Linux 侧只剩 1 份孤立的重复副本（内容与 Windows 那份相同）",
          s2.get("only_right") == 1, str(s2.get("only_right")))
    check("其余 9 首一一对应", s2.get("matched") == 9, str(s2.get("matched")))
    left_names = {t["name"] for t in cmp2["only_left"]}
    check("剩下的 Windows 独有只剩'本来就多出来的副本'类文件",
          left_names <= {"corrupt.mp3", "Daft Punk - Get Lucky (低音质).wav",
                         "03 - Daft Punk - Get Lucky.wav"},
          str(sorted(left_names)))

    print("\n=== 10. 查重 ===")
    st, dup = call("POST", "/api/duplicates", {"sources": ["win"], "within": "same",
                                               "hash_mode": "quick", "threshold": 0.9})
    gs = dup.get("groups") or []
    check("查重接口可用", st == 200 and "groups" in dup, str(st))
    print(f"     发现 {dup['stats']['groups']} 组 · 可释放 {dup['stats']['wasted_bytes']} 字节 "
          f"· 可安全释放 {dup['stats'].get('safe_wasted_bytes', 0)} 字节")
    if gs:
        g = gs[0]
        check("检出 Get Lucky 重复组", "Get Lucky" in (g.get("title") or ""),
              json.dumps([x.get("title") for x in gs], ensure_ascii=False))
        clusters = g.get("clusters") or []
        multi = [c for c in clusters if len(c) >= 2]
        check("字节完全相同的两份被归入同一个内容子簇", bool(multi),
              json.dumps([len(c) for c in clusters]))
        if multi:
            check("同子簇内哈希一致",
                  len({(i.get("hash_quick") or i.get("hash_full")) for i in multi[0]}) == 1,
                  json.dumps([i.get("hash_quick") for i in multi[0]]))
        if len(clusters) > 1:
            check("低音质版被单独成簇（不会被误判为可自动删除）", True)
            check("相似但不同音质的组明确标记为不可自动判定", g.get("exact") is False,
                  str(g.get("exact")))
        check("给出了建议保留项", bool(g.get("suggested_keep")))
        best = max(g["items"], key=lambda i: i.get("quality") or 0)["path"]
        check("建议保留的是音质最好的那份", g["suggested_keep"] == best, str(g["suggested_keep"]))
    st, adv = call("POST", "/api/duplicates/keep", {"groups": gs, "policy": "largest"})
    check("保留策略接口可用", st == 200 and len(adv.get("selections") or []) >= 1)

    if gs:
        identical = set(gs[0].get("identical_paths") or [])
        if len(identical) >= 2:
            keep_path = gs[0]["suggested_keep"]
            if keep_path not in identical:
                keep_path = sorted(identical)[0]
            victims = sorted(identical - {keep_path})
            styles = [{"id": f"cleanup{i}", "op": "trash", "src": "", "src_path": p,
                       "dst": "", "dst_path": p, "size": 0, "risk": "high",
                       "kind": "duplicate", "reason": "e2e 清理同一内容子簇内的多余副本"}
                      for i, p in enumerate(victims)]
            # 补上 source_id
            for st_item in styles:
                match = next((t for t in gs[0]["items"] if t["path"] == st_item["src_path"]), None)
                if match:
                    st_item["src"] = st_item["dst"] = match["source_id"]
                    st_item["size"] = match["size"] or 0
            st, dstep = call("POST", "/api/execute", {
                "steps": styles, "execute": True, "confirm": True, "policy": "skip"})
            check("只清理内容完全一致的副本（不同音质版本不动）",
                  st == 200 and dstep["summary"].get("done") == len(victims),
                  json.dumps(dstep.get("summary"), ensure_ascii=False))
            check("返回了回收站路径（可恢复）",
                  all((r or {}).get("trash") for r in dstep["results"]),
                  str(dstep["results"][:1]))
            import os as _os
            check("建议保留的那一份没有被删除", _os.path.exists(keep_path), keep_path)

    print("\n=== 10b. 清理后应达到完全镜像 ===")
    for sid in ("win", "linux"):
        j = scan(sid)
        check(f"{sid} 清理后重扫完成", j.get("state") == "done", str(j.get("error")))
    st, cmp3 = call("POST", "/api/compare", {"left": "win", "right": "linux"})
    s3 = cmp3["stats"]
    print("     " + json.dumps({k: s3.get(k) for k in ("left_total", "right_total", "matched",
                                                      "only_left", "only_right")}, ensure_ascii=False))
    check("清理后 Windows 侧没有孤儿文件",
          s3.get("only_left") == 0, str(s3.get("only_left")))
    check("Linux 侧只剩第 8 步人为复制的副本（正是需要用户决策的典型场景）",
          s3.get("only_right") == 1, str(s3.get("only_right")))
    check("其余文件全部一一对应",
          s3.get("matched") == s3.get("left_total"), json.dumps(s3, ensure_ascii=False))

    print("\n=== 11. 只读保护 ===")
    st, r = call("POST", "/api/sources", {"id": "rotest", "name": "只读源", "type": "local",
                                          "root": LIN_ROOT, "read_only": True})
    check("创建只读数据源", st == 200, str(r)[:200])
    st, ro = call("POST", "/api/execute", {
        "steps": [{"id": "x1", "op": "copy", "src": "win",
                   "src_path": os.path.join(WIN_ROOT, "Pop/01 - 周杰伦 - 晴天.wav"),
                   "dst": "rotest", "dst_path": "x.wav"}],
        "execute": True, "confirm": True})
    check("只读源拒绝任何写入", ro["results"][0]["state"] == "blocked",
          json.dumps(ro["results"][0], ensure_ascii=False))
    call("DELETE", "/api/sources/rotest?purge=true")

    print("\n=== 12. 文件系统落库校验 ===")
    check("补齐时自动创建了缺失目录（Rock / Dup / Broken）",
          all(os.path.isdir(os.path.join(LIN_ROOT, d)) for d in ("Rock", "Dup", "Broken")))
    check("Windows 独有曲已落到 Linux 侧",
          os.path.exists(os.path.join(LIN_ROOT, "Rock", "Queen - Bohemian Rhapsody.wav")))
    check("坏文件也被如实搬运（不因解析失败而漏传）",
          os.path.exists(os.path.join(LIN_ROOT, "Broken", "corrupt.mp3")))
    trash_root = os.path.join(WIN_ROOT, ".music-sync-trash")
    check("回收站目录已建立", os.path.isdir(trash_root), trash_root)
    if os.path.isdir(trash_root):
        found = [os.path.join(r, f) for r, _d, fs in os.walk(trash_root) for f in fs]
        check("被清理的副本确实躺在回收站里", len(found) >= 1, str(found))
    check("没有残留 .mshpart 半截文件",
          not any("mshpart" in f for _r, _d, fs in os.walk(WIN_ROOT) for f in fs))

    print(f"\n{'=' * 46}\n结果：{PASS} 通过 / {FAIL} 失败\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

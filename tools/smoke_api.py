"""后端接口全面冒烟：逐个打一遍，标注期望状态码。

只做**只读**或幂等的调用；写操作一律故意带错参数，确认校验生效而不真改数据。
"""
import json
import urllib.error
import urllib.request

B = 'http://127.0.0.1:8787'
SID = 'win-music'


def call(u, method='GET', body=None, full=False):
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(
        B + u, method=method, data=data,
        headers={'Content-Type': 'application/json'} if data else {})
    try:
        with urllib.request.urlopen(rq, timeout=300) as x:
            # 默认截断到 200 字节只是为了报错时好看；要解析响应体时必须 full=True
            raw = x.read()
            return x.status, (raw if full else raw[:200])
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (raw if full else raw[:160])
    except Exception as e:
        return -1, str(e).encode()[:160]


RO = 'GET'
PO = 'POST'
# (路径, 方法, 体, 期望状态码集合, 说明)
CASES = [
    ('/api/system', RO, None, {200}, '系统信息'),
    ('/api/sources', RO, None, {200}, '数据源列表'),
    (f'/api/library/{SID}?limit=2', RO, None, {200}, '曲库分页'),
    (f'/api/library/{SID}?limit=2&sort=size&order=desc', RO, None, {200}, '曲库排序'),
    (f'/api/library/{SID}?q=%E5%A4%9C&limit=2', RO, None, {200}, '曲库搜索'),
    (f'/api/library/{SID}?artist=x&album=y&limit=2', RO, None, {200}, '曲库按作者专辑'),
    (f'/api/files/list?sid={SID}&path=', RO, None, {200}, '文件列表(根)'),
    (f'/api/files/list?sid={SID}&path=0%E5%8D%95%E6%9B%B2', RO, None, {200}, '文件列表(子目录)'),
    (f'/api/files/list?sid={SID}&path=../../etc', RO, None, {200, 400}, '文件列表(越界)'),
    ('/api/files/under', PO, {'source': SID, 'paths': ['0单曲']}, {200}, '按目录取文件'),
    ('/api/files/under', PO, {}, {400, 422}, '按目录取文件(缺参)'),
    (f'/api/folders/{SID}', RO, None, {200}, '目录树'),
    (f'/api/folders/empty-scan?sid={SID}', RO, None, {200}, '空目录扫描'),
    (f'/api/folders/empty-remove', PO, {'paths': []}, {400, 422}, '空目录删除(空参)'),
    ('/api/folders/dissolve', PO, {'source': SID, 'path': 'x'}, {400, 410}, '解散文件夹(应已移除)'),
    ('/api/station', RO, None, {200}, '中转站概况'),
    ('/api/station/check?add_mb=1', RO, None, {200}, '容量预检'),
    ('/api/station/suggest', PO, {}, {400, 422}, '智能建议(空参)'),
    ('/api/station/move-in', PO, {}, {400, 422}, '移入中转站(空参)'),
    ('/api/station/move-out', PO, {}, {400, 422}, '归入目录(空参)'),
    ('/api/station/remove', PO, {'files': ['x']}, {400, 422}, '删除(未确认)'),
    ('/api/station/restore', PO, {}, {400, 422}, '还原(空参)'),
    (f'/api/mappings?sid={SID}', RO, None, {200}, '映射规则读'),
    ('/api/mappings', RO, None, {200}, '映射规则(全)'),
    ('/api/mappings', 'PUT', {'source': SID, 'rules': {}}, {200, 400, 422}, '映射规则写(空)'),
    (f'/api/search?sid={SID}&q=minecraft', RO, None, {200}, '模糊搜索'),
    (f'/api/search?sid={SID}&q=', RO, None, {200}, '空搜索'),
    # 查重现在是**后台任务**（大库上几十秒），wait=true 走同步分支拿结果。
    # 参数名是 sources（列表），不是 source —— 原来这里写错了，一直没生效。
    ('/api/duplicates', PO, {'sources': [SID], 'within': 'same',
                             'hash_mode': 'none', 'wait': True},
     {200}, '查重(同步)'),
    ('/api/duplicates', PO, {'sources': [SID], 'hash_mode': 'none'},
     {200}, '查重(后台任务)'),
    ('/api/compare', PO, {}, {400, 422}, '对比(缺两侧)'),
    ('/api/compare', PO, {'left': SID, 'right': SID}, {400}, '对比(同一源→拒)'),
    (f'/api/jobs', RO, None, {200}, '任务列表'),
    (f'/api/jobs/nonexistent', RO, None, {200, 404}, '任务详情(不存在)'),
    ('/api/execute', PO, {'steps': []}, {400, 422}, '执行(空步骤)'),
    ('/api/execute', PO, {'steps': [{'id': 'x', 'op': 'copy'}], 'execute': True}, {400, 422}, '执行(未确认)'),
    ('/api/plan', PO, {}, {400, 422}, '计划(空)'),
    (f'/api/library/{SID}/facets', RO, None, {200, 404}, '曲库分面'),
    (f'/api/upload/batches', RO, None, {200}, '上传批次(暂存区)'),
    ('/api/upload/batch-to-station', PO, {'batches': []}, {400, 422}, '批次转中转站(空参)'),
    ('/api/upload/cleanup', PO, {'mode': 'all'}, {400}, '清理暂存区(未确认→400)'),
    ('/api/upload/cleanup', PO, {'mode': 'empty'}, {200}, '清理暂存区(空壳)'),
    (f'/api/upload/dups?batch=x', RO, None, {200, 400, 422}, '上传查重(批次参数)'),
    (f'/api/organize/analyze?sid={SID}', RO, None, {400}, '整理分析(缺目标→400)'),
    (f'/api/organize/preview?sid={SID}&limit=5', RO, None, {200}, '整理预览'),
    # 外置存储探测：列表不做测速（否则打开对话框要等），
    # probe 要测速并给出针对性建议
    ('/api/drives', RO, None, {200}, '盘列表'),
    # 对比计划：transfer=true 时 copy 步骤应被换成 transfer（安全搬运）。
    # 只生成计划、不执行，所以不会动文件。
    ('/api/plan', PO, {'left': SID, 'right': SID, 'transfer': True},
     {400}, '对比计划(同源→400)'),
    ('/api/plan', PO, {'left': SID, 'right': '__nope__', 'transfer': True},
     {404}, '对比计划(坏源→404)'),
    # 候选区过期项检查：只做 stat，不读内容
    ('/api/files/exists', PO, {}, {400, 422}, '文件存在性(缺参)'),
    ('/api/files/exists', PO, {'source': SID, 'paths': []}, {400},
     '文件存在性(空列表→400)'),
    ('/api/files/exists', PO, {'source': '__nope__', 'paths': ['x']},
     {400, 404}, '文件存在性(坏源)'),
    # 跨库传输：只测参数校验与守卫（不动真实文件）
    ('/api/files/transfer', PO, {}, {400, 422}, '跨库传输(空参)'),
    ('/api/files/transfer', PO, {'from': SID, 'to': SID, 'items': ['x']},
     {400}, '跨库传输(同源→400)'),
    ('/api/files/transfer', PO, {'from': SID, 'to': 'nope',
                                 'items': ['x']}, {404}, '跨库传输(源不存在→404)'),
    ('/api/files/transfer', PO, {'from': SID, 'to': SID, 'items': []},
     {400}, '跨库传输(无文件→400)'),
    (f'/api/files/transfer', PO, {'from': SID, 'to': SID, 'items': ['x'],
                                  'policy': 'bogus'}, {400},
     '跨库传输(非法策略→400)'),
    ('/api/device/probe', RO, None, {400, 422}, '盘探测(缺 path)'),
    ('/api/device/probe?path=Z%3A%5C__nope__', RO, None, {404},
     '盘探测(路径不存在→404)'),
    # 播放器的数据源：试听/进度全靠它。以前冒烟完全没覆盖 ——
    # 而"播放器中看不中用"正是用户反馈过的问题，后端接口本身必须先是好的。
    ('/api/preview/999999999', RO, None, {400, 404}, '试听(不存在的曲目)'),
    ('/api/cover/999999999', RO, None, {400, 404}, '封面(不存在的曲目)'),
    ('/api/cover-raw?path=', RO, None, {400, 404, 422}, '封面(缺参)'),
    ('/api/organize/plan', PO, {}, {400, 422}, '整理计划(空)'),
    ('/api/organize/album-apply', PO, {}, {400, 422}, '应用专辑计划(空)'),
    ('/api/organize/apply', PO, {}, {400, 422}, '应用计划(空)'),
    ('/api/organize/plan-paths', PO, {}, {400, 422}, '计划-指定路径(空)'),
    # album-plan 现在**默认后台**（实测真实库按专辑分组要 6 秒，
    # 同步返回时界面只有个转圈、没进度也取消不了）。
    # 所以这里两种模式都要覆盖：
    #   wait=true  → 同步拿方案（脚本/测试用）
    #   默认       → 立刻返回 {job, async:true} 句柄
    ('/api/organize/album-plan', PO, {'source': SID, 'group_by': 'album',
                                      'min_group': 5, 'wait': True},
     {200}, '专辑计划(同步)'),
    ('/api/organize/album-plan', PO, {'source': SID, 'group_by': 'artist',
                                      'min_group': 5, 'wait': True},
     {200}, '作者计划(同步)'),
    ('/api/organize/album-plan', PO, {'source': SID, 'group_by': 'artist_album',
                                      'min_group': 5, 'wait': True},
     {200}, '作者+专辑计划(同步)'),
    ('/api/organize/album-plan', PO, {'source': SID, 'group_by': 'album',
                                      'min_group': 5},
     {200}, '专辑计划(后台→返回 job)'),
    ('/api/organize/album-plan', PO, {'source': SID, 'group_by': 'bogus',
                                      'wait': True},
     {400}, '专辑计划(非法分组→400)'),
    (f'/api/station', RO, None, {200}, '中转站(复查)'),

    # ---- 本轮新增的平台机制 ----
    (f'/api/state?sid={SID}', RO, None, {200}, '数据变化信号'),
    ('/api/state', RO, None, {200}, '数据变化信号(全库)'),
    ('/api/busy', RO, None, {200}, '独占占用查询'),
    ('/api/unfinished', RO, None, {200}, '未完成任务'),
    ('/api/unfinished/recover', PO, {}, {400, 404, 422}, '恢复(缺 job)'),
    ('/api/unfinished/recover', PO, {'job': 'nope'}, {404}, '恢复(不存在)'),
    ('/api/jobs/nonexistent/pause', PO, {}, {200, 404}, '暂停(不存在)'),
    ('/api/jobs/nonexistent/resume', PO, {}, {200, 404}, '继续(不存在)'),
    ('/api/jobs/nonexistent/cancel', PO, {}, {200, 404}, '取消(不存在)'),
    ('/api/jobs/nonexistent/ack', PO, {}, {200, 404}, '确认(不存在)'),

    # ---- 设置校验（防特殊符号 / 天文数字）----
    ('/api/settings', 'PUT', {'scan_workers': 99999999}, {400}, '设置(越界→拒)'),
    ('/api/settings', 'PUT', {'name_similarity': 'NaN'}, {400}, '设置(NaN→拒)'),
    ('/api/settings', 'PUT', {'staging_dir': 'bad<>name'}, {400}, '设置(非法字符→拒)'),
    ('/api/settings', 'PUT', {'station_max_gb': 0}, {200}, '设置(0=不限额)'),

    # ---- 标签读写（只做只读 / 无副作用的调用）----
    ('/api/tags/read?sid=__nope__&path=x', RO, None, {400, 404}, '读标签(坏源)'),
    ('/api/tags/backups?limit=3', RO, None, {200}, '标签备份列表'),
    ('/api/tags/preview', PO, {}, {400, 422}, '标签预演(缺参)'),
    ('/api/tags/write', PO, {}, {400, 422}, '写标签(缺参)'),
    ('/api/tags/restore', PO, {}, {400, 422}, '还原标签(缺参)'),

    # ---- 映射单条 ----
    ('/api/mappings/one', PO, {}, {400, 422}, '映射单条(缺参)'),
]

bad = []
print(f"{'状态':<6} {'说明':<26} 路径")
print('-' * 96)

# ★ 跑之前先给设置拍个快照，跑完恢复。
#
# 踩过：这里有一条 `PUT /api/settings {'station_max_gb': 0}` 用来验证
# "0 = 不限额"是合法的 —— 它确实返回 200，于是**真的把用户 config.toml 里的
# 中转站容量上限改成了 0（不限额）**。用户的 20 GB 保护就这么被一个
# "只读冒烟测试"悄悄关掉了，而且没有任何提示。
# 冒烟测试跑的是**真实服务 + 真实配置**，所以任何成功的写调用都必须还原。
_snap = None
try:
    _c, _raw = call('/api/settings', full=True)
    if _c == 200:
        _body = json.loads(_raw.decode('utf-8', 'replace'))
        # 响应是 {config_path, settings:{...}}，PUT 只认 settings 那一层
        _snap = _body.get('settings') if isinstance(_body, dict) else None
except Exception as _e:
    _snap = None
if _snap:
    print(f"（已记录设置快照，共 {len(_snap)} 项，跑完会恢复）")
else:
    print("（★ 拿不到设置快照 —— 本次若有用例改设置将无法恢复）")

for path, method, body, want, label in CASES:
    code, raw = call(path, method, body)
    ok = code in want
    if not ok:
        bad.append((label, path, code, want, raw))
    mark = 'OK ' if ok else 'BAD'
    print(f"{mark:<4} {code:<4} {label:<26} {path[:52]}")

# ---- 恢复设置快照（见前面的说明：成功的写调用会改到用户真实配置）----
if _snap:
    _c2, _raw2 = call('/api/settings', 'PUT', _snap, full=True)
    _after_c, _after_raw = call('/api/settings', full=True)
    _after = {}
    try:
        _ab = json.loads(_after_raw.decode('utf-8', 'replace'))
        _after = (_ab.get('settings') if isinstance(_ab, dict) else {}) or {}
    except Exception:
        pass
    _diff = {k: (v, _after.get(k)) for k, v in _snap.items()
             if k in _after and _after.get(k) != v}
    if _c2 == 200 and not _diff:
        print(f"\n（设置已恢复原样，{len(_snap)} 项全部一致）")
    else:
        bad.append(('设置未能恢复', '/api/settings', _c2, {200}, b''))
        print(f"\n!!! 设置没有完全恢复，差异：{_diff}")

print()
if bad:
    print(f"!!! {len(bad)} 个不符合预期：")
    for label, path, code, want, raw in bad:
        print(f"\n  {label}  {path}")
        print(f"    期望 {sorted(want)}，实际 {code}")
        print(f"    响应: {raw.decode('utf-8', 'replace')[:220]}")
else:
    print(f"全部 {len(CASES)} 个接口符合预期")

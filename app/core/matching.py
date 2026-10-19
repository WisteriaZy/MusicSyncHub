"""跨库匹配算法。

问题：两台服务器上的音乐"同一首歌、文件名一样或相似，但标签元数据不同"。
因此匹配不能依赖标签，而要按下面的证据链打分：

1. **归一化文件名**（去掉 `01 -`、`(320k)`、`[FLAC]`、`_HQ` 等噪声后比较）
2. **时长**（同一录音时长几乎一致，容差默认 2 秒；试听/剪辑版会被判为不同）
3. **文件大小 / 码率**（同曲不同音质时大小差异大，但不应否决匹配，只影响"哪份更好"）
4. **标签**（有则加分：标题、艺术家、专辑）
5. **内容哈希**（抽样哈希相同 → 100% 同一文件）

输出带置信度的配对结果，低置信度的让用户人工确认，绝不自动删改。
"""
from __future__ import annotations

import difflib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import paths as P

# 权重
W_NAME = 0.38
W_DURATION = 0.34
W_META = 0.16
W_SIZE = 0.12

# 跨名称配对的门槛。真正挡住误配的不是分数，而是下面这条**艺术家否决**规则：
# 「Windows Only - Exclusive Track」和「Linux Only - Exclusive Track」
# 文件名归一化后相似度 0.85、标题还都叫 "Exclusive Track"，分数能到 0.95，
# 但艺术家一个是 "Windows Only" 一个是 "Linux Only"，必须否决。
FUZZY_NAME_MIN = 0.90      # 跨名称配对要求文件名高度相似才放行
# ★ 0.55 会让"作者相似度 0.55~0.72"这段成为盲区：既不否决、也够不上
#   "确认同一人"。实测 `Windows Only` vs `Linux Only` = 0.636 就被放行，
#   两首不同作者的同名歌被并成一组。阈值对齐到确认线 0.72：
#   低于 0.72 一律按"可能不是同一人"走字段比对，不会静默放行。
ARTIST_VETO_MIN = 0.72     # 艺术家相似度低于此值：一票否决

THUMB_KEYS = [
    "id", "source_id", "path", "rel_path", "parent", "name", "ext", "size", "mtime",
    "title", "artist", "album", "album_artist", "track_no", "duration", "bitrate",
    "sample_rate", "channels", "bits_per_sample", "codec", "is_lossless",
    "has_cover", "quality", "name_key", "dup_key", "hash_quick", "hash_full", "hash_state",
]


def rec(row: Any) -> Dict[str, Any]:
    """sqlite3.Row / dict -> 普通 dict，只保留需要的字段。"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return {k: row.get(k) for k in THUMB_KEYS if k in row}
    try:
        keys = row.keys()
    except AttributeError:
        return {}
    return {k: row[k] for k in THUMB_KEYS if k in keys}


# ------------------------------------------------------------------ 键

def dup_key(r: Dict[str, Any], bucket_step: float = 2.0) -> str:
    """同一首歌的精确分组键：标题 + 主艺术家 + 时长桶 + **版本标记**。

    时长按 `bucket_step`（默认 2 秒）分桶，避免不同编码的毫秒误差把同一首歌拆开。

    ★ `bucket_step` 是给**调用方**用的旋钮，不是随便加的。

    这个键是**精确字符串匹配**：桶号不一样就永远配不上。
    默认 2 秒对同库查重没问题（那边有并查集兜底），但跨库配对的
    第 2 阶段完全靠它 —— 于是出现这种不一致：

        文件名相同（走第 1 阶段，那里有 `tolerance` 判断）→ 时长差 3 秒**能配**
        文件名不同（走第 2 阶段，只有精确键）      → 时长差 3 秒**配不上**
                                                   而且**调大容差也没用**

    实测 `Get Lucky (Live Version).flac` ↔ `daft punk - get lucky.flac`：
    两边 `dup_key` 分别是 `get lucky|daft punk|200|live` 和
    `get lucky|daft punk|204|`，桶号 200 vs 204 —— 永远不会进同一个候选池。

    所以调用方可以传一个跟 `tolerance` 走的更粗的桶（比如 `2×tolerance`）：
    这**只是扩大候选池**，真正能不能配仍然由 `score_pair` 的时长门禁
    （`duration_similarity` 里的 2×tolerance）和 `fuzzy_admissible` 把关 ——
    桶粗一点只会多比几对，不会误配。

    默认值保持 2.0，所以索引里存的键、以及其它调用点的行为都不变。

    版本标记（伴奏 / 重制版 / 现场版 / 翻唱 / 国语版 …）必须进键：
    归一化会故意把这些词当噪声删掉（跨库同名匹配需要），如果不在这里补回来，
    `晴天.wav`、`晴天 (伴奏).wav`、`晴天 (Remastered).wav` 会拿到同一个键，
    被当成"同一条录音的多份副本"并成一组——这正是"伴奏/重制版被混在一起"的原因。
    """
    title = P.normalize_text(r.get("title") or "")
    if not title:
        title = P.name_key(r.get("name") or "")
    artist = P.normalize_text(P.primary_artist(r.get("artist") or ""))
    if not artist:
        nk = P.name_key(r.get("name") or "")
        if " " in nk:
            artist = nk.split(" ")[0]
    dur = r.get("duration")
    bucket = ""
    try:
        step = float(bucket_step)
    except (TypeError, ValueError):
        step = 2.0
    if step <= 0:
        step = 2.0
    if dur:
        bucket = str(int(round(float(dur) / step)) * int(step))
    if not title:
        return ""
    # 版本标记来自原始文件名（标题标签里通常不含"伴奏"这类词，文件名才有）
    ver = P.version_tags(r.get("name") or "", r.get("title") or "", r.get("rel_path") or "")
    return f"{title}|{artist}|{bucket}|{ver}"


def song_key(r: Dict[str, Any]) -> str:
    """用于跨库配对的"歌"键，不含时长（时长单独打分）。"""
    title = P.normalize_text(r.get("title") or "") or P.name_key(r.get("name") or "")
    artist = P.normalize_text(P.primary_artist(r.get("artist") or ""))
    if not artist:
        nk = P.name_key(r.get("name") or "")
        if " " in nk:
            artist = nk.split(" ")[0]
    return f"{title}|{artist}"


# ------------------------------------------------------------------ 打分

def name_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """文件名为主、标签为辅的名称相似度。"""
    ka = P.name_key(a.get("name") or "")
    kb = P.name_key(b.get("name") or "")
    base = P.similarity(ka, kb)
    na = P.normalize_text(a.get("title") or "")
    nb = P.normalize_text(b.get("title") or "")
    if na or nb:
        base = max(base, P.similarity(na, nb))
    # ★ "艺术家+标题"这一路**必须在标题存在时才算**。
    #
    #   踩过：原来是 `if sa.strip() or sb.strip()` 就取相似度 ——
    #   而 `sa` 是 `艺术家 + " " + 标题`。当**标题为空**时，
    #   `sa` 就退化成"只有艺术家"。两个艺术家相同（比如都是 Queen）
    #   的文件，这里就得到 1.0，然后 `max()` 把文件名算出来的分数盖掉。
    #
    #   后果是**文件名完全不同也会被当成"名字完全一致"**：
    #       实测 `Bohemian Rhapsody.flac` ↔ `Smells Like Teen Spirit.flac`
    #       （同艺术家、无标题）→ name_sim = 1.0000
    #   于是后续所有"名字像不像"的门禁全部失效，两首不相干的歌被配成一对。
    #
    #   只在标题真的存在时才比较，就不会出现"比了个空"的情况。
    ta = P.normalize_text(a.get("title") or "")
    tb = P.normalize_text(b.get("title") or "")
    if ta and tb:
        sa = P.name_key((a.get("artist") or "") + " " + ta)
        sb = P.name_key((b.get("artist") or "") + " " + tb)
        if sa.strip() and sb.strip():
            base = max(base, P.similarity(sa, sb))
    return round(base, 4)


def duration_similarity(a: Dict[str, Any], b: Dict[str, Any],
                        tolerance: float = 2.0) -> Tuple[float, Optional[float]]:
    """时长相似度。

    ★ 衰减尾巴（28 秒）**不能写死**，否则它会反过来卡住门禁。

    踩过：`group_duplicates` 的合并门禁是 3×tolerance，
    但这里写死 28 秒的线性衰减 —— 于是 tolerance=10 时门禁允许 30 秒，
    而 28.5 秒的一对时长分只有 0.34，总分过不了阈值，**照样合不了**。
    表现就是"把容差调大也没用"，而且边界会莫名其妙地卡在 28 秒附近。

    现在尾巴 = max(28, 3×tolerance)：默认 2.0 时仍是 28（老行为不变），
    容差调大时尾巴跟着放到刚好覆盖门禁。
    """
    da, db = a.get("duration"), b.get("duration")
    if not da or not db:
        return 0.55, None  # 未知：给中间分，不奖不罚
    diff = abs(float(da) - float(db))
    try:
        tol = float(tolerance)
    except (TypeError, ValueError):
        tol = 2.0
    if tol <= 0:
        tol = 2.0
    # 门禁 = 2×tolerance（`group_duplicates` 里的时长门禁与这里**必须一致**）。
    # 落在门禁内给满分 —— 这样"用户把容差调大"才真的能把那些
    # 标签时长有出入的同一首歌收进来，而不是被分数悄悄扣掉。
    gate = 2.0 * tol
    if diff <= gate:
        return 1.0, diff
    # 门禁之外只做"严重不符"的区分，尾部给足余量，避免它反过来卡住门禁
    tail = max(28.0, 4.0 * tol)
    score = max(0.0, 1.0 - (diff - gate) / tail)
    return round(score, 4), diff


def meta_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """标签相似度。两侧都缺失时返回中性值 0.5，不奖不罚。"""
    total_w = 0.0
    acc = 0.0
    for key, weight in (("album", 0.5), ("artist", 0.3), ("title", 0.2)):
        va = P.normalize_text(a.get(key) or "")
        vb = P.normalize_text(b.get(key) or "")
        if not va or not vb:
            continue
        acc += P.similarity(va, vb) * weight
        total_w += weight
    if total_w == 0:
        return 0.5
    return round(acc / total_w, 4)


def size_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    sa, sb = a.get("size") or 0, b.get("size") or 0
    if not sa or not sb:
        return 0.5
    ratio = min(sa, sb) / float(max(sa, sb))
    return round(0.35 + 0.65 * ratio, 4)  # 即使体积差很多，也可能只是不同音质


def _token_sim(na: str, nb: str) -> float:
    """词级相似度：既能容忍拼写/顺序差异，也能对"关键差异词"敏感。"""
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _distinguishing_tokens(na: str, nb: str, min_len: int = 3) -> List[str]:
    """找出"只出现在一侧、而且在另一侧找不到近似对应"的词。

    这类词是文件名里真正区分两首歌的部分。
    例："windows only exclusive track" vs "linux only exclusive track"
        → ["windows", "linux"]（windows/linux 互不相似，是真差异）
        "pink floyd time" vs "pink floyd time remaster"
        → ["remaster"]（只是版本后缀）
    """
    ta = [t for t in na.split() if len(t) >= min_len]
    tb = [t for t in nb.split() if len(t) >= min_len]
    sb = set(tb)
    sa = set(ta)
    out: List[str] = []
    for t in ta:
        if t in sb:
            continue
        if any(P.similarity(t, u) >= 0.6 for u in tb if u not in sa):
            continue
        out.append(t)
    for t in tb:
        if t in sa:
            continue
        if any(P.similarity(t, u) >= 0.6 for u in ta if u not in sb):
            continue
        out.append(t)
    return out


def artist_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """主艺术家相似度，取"序列相似度"与"词级相似度"里更乐观的一个。

    为什么要取更乐观的：`Pink Floyd` vs `Pink Floyd (Remaster)` 这种是同一人，
    字符序列会被多出来的词拖低分数；而 `Windows Only` vs `Linux Only`
    的 Jaccard 只有 0.33，词级证据能把它识别出来。
    """
    ar_a = P.normalize_text(P.primary_artist(a.get("artist") or ""))
    ar_b = P.normalize_text(P.primary_artist(b.get("artist") or ""))
    if not ar_a or not ar_b:
        return None
    return max(P.similarity(ar_a, ar_b), _token_sim(ar_a, ar_b))


def artist_conflict(m: "Match") -> bool:
    """两侧艺术家都明确存在、但明显不是同一个 → 判为不同的歌。

    这是"最后一票否决"，专门拦这种陷阱：
      文件名高度相似（甚至规范化后一模一样）、标题相同、时长也接近，
      但一个是 Windows 独占曲、一个是 Linux 独占曲，只有艺术家能揭穿。

    ★ 这里的守卫条件踩过一个坑：原来要求"文件名里存在互相不相似的差异词"，
    否则直接 `return False`。而**两个文件名一模一样**时，差异词必然是空集
    —— 于是唯一能揭穿它们的信息被这条守卫挡掉了，两首不同的歌照样被并成一组。
    实测：`Song.mp3`（作者 A）与 `Song.mp3`（作者 B）分数 0.949、判定为重复。

    所以差异词为空时要分两种情况：
      · 名字**几乎一样**（≥0.95）→ 正是本条要管的场景，**继续走否决**
      · 名字压根不像 → 那不是"同名"，不归本条管，放行（交给别的判据）
    """
    ar = m.artist_sim
    if ar is None or ar >= ARTIST_VETO_MIN:
        return False
    # 文件名里存在"互相不相似的差异词"，说明这俩本来就该是两首歌
    na, nb = P.normalize_text(m.left.get("name") or ""), P.normalize_text(m.right.get("name") or "")
    if not _distinguishing_tokens(na, nb) and P.similarity(na, nb) < 0.95:
        return False
    # 差异词之外，艺术家有没有一点办法对上？（拼写差异、缩写、带后缀）
    ra = P.normalize_text(P.primary_artist(m.left.get("artist") or ""))
    rb = P.normalize_text(P.primary_artist(m.right.get("artist") or ""))
    if P.similarity(ra, rb) >= ARTIST_VETO_MIN:
        return False
    return True


def version_mismatch(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """两侧版本标记不同 → 不是同一条录音。

    注意是"标记必须一致"，而不是"都不为空才比"：`晴天.flac`（无标记）与
    `晴天（伴奏）.flac`（instrumental）也要判为不一致——归一化会把"伴奏"删掉，
    于是两者文件名相似度 1.0、时长完全一致、分数直接顶满 1.0，
    不在这里拦住就会被自动配成一对 / 并成一组。

    ★ 传给 `version_tags` 的字段必须**和 `dup_key` 完全一致**
    （name + title + rel_path）。

    踩过：这里原来只传了 `name` 和 `title`，而 `dup_key`（第 88 行）还传了
    `rel_path`。于是"版本标记只在目录名里"的歌会出现自相矛盾的结果：
    `dup_key` 认为两者不同版本（分到两组），而配对/合并路径的
    `version_mismatch` 看不到目录名、以为版本一致（又把它们并回来）。
    典型场景：`专辑 (Remix)/Song.mp3` 对比 `专辑/Song.mp3`。
    """
    va = P.version_tags(a.get("name") or "", a.get("title") or "",
                        a.get("rel_path") or "")
    vb = P.version_tags(b.get("name") or "", b.get("title") or "",
                        b.get("rel_path") or "")
    return va != vb


def fuzzy_admissible(m: "Match") -> bool:
    """配对是否允许（精确键与模糊猜测共用）。

    * 版本不同（伴奏 / 重制 / 现场 …）→ 直接否
    * 艺术家冲突 → 直接否
    * 文件名高度相似 → 放行
    * 否则要求"艺术家一致 + 时长几乎相同"才放行
    """
    if version_mismatch(m.left, m.right):
        return False
    if artist_conflict(m):
        return False
    if m.name_sim >= FUZZY_NAME_MIN:
        return True
    artist_ok = m.artist_sim is not None and m.artist_sim >= 0.9
    near_same_dur = m.duration_diff is not None and m.duration_diff <= 0.7
    return bool(artist_ok and near_same_dur)


@dataclass
class Match:
    left: Dict[str, Any]
    right: Dict[str, Any]
    score: float
    name_sim: float
    duration_diff: Optional[float]
    artist_sim: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    identical: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "score": round(self.score, 4),
            "confidence": confidence_label(self.score),
            "name_sim": self.name_sim,
            "artist_sim": self.artist_sim,
            "duration_diff": None if self.duration_diff is None else round(self.duration_diff, 2),
            "reasons": self.reasons,
            "identical": self.identical,
            "same_size": bool(self.left.get("size")) and self.left.get("size") == self.right.get("size"),
            "size_delta": (self.left.get("size") or 0) - (self.right.get("size") or 0),
            "quality_delta": round((self.left.get("quality") or 0) - (self.right.get("quality") or 0), 3),
        }


def confidence_label(score: float) -> str:
    if score >= 0.94:
        return "identical"
    if score >= 0.86:
        return "high"
    if score >= 0.72:
        return "medium"
    return "low"


def score_pair(a: Dict[str, Any], b: Dict[str, Any], tolerance: float = 2.0) -> Match:
    ns = name_similarity(a, b)
    ds, diff = duration_similarity(a, b, tolerance)
    ms = meta_similarity(a, b)
    ss = size_similarity(a, b)
    ar = artist_similarity(a, b)
    score = W_NAME * ns + W_DURATION * ds + W_META * ms + W_SIZE * ss

    reasons: List[str] = []
    if ar is not None and ar >= 0.9 and ns >= 0.8:
        score = min(1.0, score * 1.05)
        reasons.append("艺术家一致")
    if ns >= 0.985:
        reasons.append("文件名归一化后完全一致")
    elif ns >= 0.8:
        reasons.append(f"文件名高度相似 ({ns:.0%})")
    elif ns >= 0.6:
        reasons.append(f"文件名部分相似 ({ns:.0%})")
    if diff is not None:
        if diff <= tolerance:
            reasons.append(f"时长一致（差 {diff:.1f}s）")
        elif diff < 8:
            reasons.append(f"时长接近（差 {diff:.1f}s，可能是不同版本）")
        else:
            reasons.append(f"时长差异较大（差 {diff:.1f}s，需人工确认）")
    else:
        reasons.append("有一侧缺少时长信息")
    if a.get("size") and a.get("size") == b.get("size"):
        reasons.append("文件大小完全相同")
    if (a.get("hash_quick") and a.get("hash_quick") == b.get("hash_quick")
            and a.get("hash_state") not in (None, "none")):
        score = max(score, 0.995)
        reasons.append("抽样内容哈希一致")

    identical = bool(
        a.get("hash_full") and a.get("hash_full") == b.get("hash_full")
    ) or bool(a.get("size") and a.get("size") == b.get("size") and ns >= 0.985 and (diff is not None and diff <= 0.5))
    return Match(left=a, right=b, score=min(1.0, score), name_sim=ns,
                 duration_diff=diff, reasons=reasons, identical=identical,
                 artist_sim=ar)


# ------------------------------------------------------------------ 配对

def _pair_by_key(left, right, used_r, key_fn, tolerance, threshold,
                 require_duration_close=False, filter_left=None,
                 apply_fuzzy_gates=False, duration_gate=None):
    """按指定键分桶做一对一配对。返回新增的 Match 列表，并更新 used_r。

    `duration_gate`：可选的**显式时长门禁**（秒）。键是精确字符串匹配时，
    调用方往往要把桶放宽来容纳标签误差 —— 桶一宽，键本身就失去了
    "时长筛选"的作用。这时必须用这个显式门禁补回来，否则会放进
    时长差很大的候选（实测过：同标题不同艺术家、时长差 20 秒也能配上）。
    """
    l_by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    r_by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for x in (filter_left if filter_left is not None else left):
        k = key_fn(x)
        if k:
            l_by[k].append(x)
    for x in right:
        k = key_fn(x)
        if k:
            r_by[k].append(x)

    out: List[Match] = []
    for k, lrows in l_by.items():
        rrows = [x for x in r_by.get(k, []) if id(x) not in used_r]
        if not rrows:
            continue
        cands: List[Tuple[float, Dict[str, Any], Dict[str, Any]]] = []
        for a in lrows:
            for b in rrows:
                m = score_pair(a, b, tolerance)
                if m.score < threshold:
                    continue
                # 时长差太大 = 同一首歌的不同版本（现场/剪辑/伴奏），
                # 不能自动当成同一份，交给人来判断。
                if require_duration_close and m.duration_diff is not None \
                        and m.duration_diff > tolerance * 2:
                    continue
                # 显式时长门禁（见 docstring）：键放宽之后必须靠它兜住
                if duration_gate is not None and m.duration_diff is not None \
                        and m.duration_diff > duration_gate:
                    continue
                if apply_fuzzy_gates and not fuzzy_admissible(m):
                    continue
                cands.append((m.score, a, b))
        cands.sort(key=lambda t: -t[0])
        taken: set = set()
        for _sc, a, b in cands:
            if id(a) in taken or id(b) in used_r:
                continue
            taken.add(id(a))
            used_r.add(id(b))
            out.append(score_pair(a, b, tolerance))
    return out


def pair_sources(left: Sequence[Dict[str, Any]], right: Sequence[Dict[str, Any]],
                 tolerance: float = 2.0, threshold: float = 0.6,
                 block_by_duration: bool = True) -> Dict[str, Any]:
    """在两个库之间做一对一配对，三阶段递进：

    1. **文件名键精确配对**：归一化文件名一致 —— 最强证据，优先级最高
    2. **歌键精确配对**：标题 + 主艺术家 + 时长桶，补文件名被改写过的场景
    3. **模糊兜底**：仅用时长邻近的候选，且名称不够像时必须有旁证

    每一阶段只处理上一阶段没配上的文件，因此"精确证据"永远优先于"猜测"。
    """
    used_r: set = set()
    # ★ A1：`block_by_duration=False` = 允许跨时长桶做模糊配对。
    #   实现方式是把容差放大到无穷（分桶宽度 = 2×容差 → 只有一个桶、
    #   `score_pair` 的时长惩罚也归零），不需要改各阶段的候选池逻辑。
    if not block_by_duration:
        tolerance = max(float(tolerance or 0.0), 1e6)

    # 准备：词序不同但词元完全一致的同一首歌。
    # `Daft Punk - Get Lucky` 与 `Get Lucky - Daft Punk` 的归一化字符串并不相同
    # （相似度只有 0.76），文件名键阶段配不上、模糊兜底阶段又够不到门槛，
    # 于是同一首歌被当成两首。这里做一次一对一精确配对，只认"词元集合完全一致
    # 且 ≥2 个词"，所以不会把 `Get Lucky` 和 `Get Lucky (Remix)` 配到一起
    # （后者词元多一个，且版本标记不同）。
    def _tokens(x: Dict[str, Any]) -> str:
        nk = x.get("name_key") or P.name_key(x.get("name") or "")
        ts = {t for t in nk.split() if t}
        return " ".join(sorted(ts)) if len(ts) >= 2 else ""

    def _same_tag(x: Dict[str, Any]) -> str:
        # ★ 必须和 `dup_key` 传同样的字段（name + title + rel_path）。
        #   原来这里漏了 `rel_path`，于是"版本标记只在目录名里"的歌
        #   （例：`专辑 (Remix)/Song.mp3`）会被当成与原版同版本参与配对，
        #   而 `dup_key` 又认为它们不同版本 —— 同一个文件两套结论。
        #   约定与本函数原本的注释都写在 `version_mismatch` 的 docstring 里。
        return P.version_tags(x.get("name") or "", x.get("title") or "",
                              x.get("rel_path") or "")

    r_by_tokens: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for x in right:
        key = _tokens(x)
        if key:
            r_by_tokens[(key, _same_tag(x))].append(x)
    m0: List[Match] = []
    # ★ 遍历顺序必须与输入顺序无关。
    #   原来直接 `for a in left`：把同一份数据的两侧顺序反过来，
    #   配对结果会变（实测同一对从 0.57 变成 1.00）——"谁先谁先得"。
    #   同一份库换个行序就该给出同样的结果，所以排序后再走。
    _gate0 = 2.0 * float(tolerance or 2.0)
    for a in sorted(left, key=lambda x: (str(x.get("name_key") or ""),
                                         str(x.get("path") or ""))):
        key = _tokens(a)
        if not key:
            continue
        pool = [b for b in r_by_tokens.get((key, _same_tag(a)), []) if id(b) not in used_r]
        if not pool:
            continue
        # ★ 阶段 0 原来**一道门禁都没有**：不判时长、不判艺术家、不判分数。
        #   实测 `Adele - Hello.flac` ↔ `Hello - Adele.flac` 时长差 255 秒
        #   也照样配上（score 0.558），两侧的 only_left / only_right 因此都是空的
        #   ——"其实缺一份"这个事实被静默吞掉，永远不会有补齐步骤。
        #   漏配能看见（显示成"独有"），瞎配看不见，所以宁可漏配。
        def _score_of(x):
            return score_pair(a, x, tolerance)
        b = max(pool, key=lambda x: (_score_of(x).score,
                                     str(x.get("path") or "")))
        if version_mismatch(a, b):
            continue
        m = _score_of(b)
        if m.duration_diff is not None and m.duration_diff > _gate0:
            continue
        # 这里**不加** `fuzzy_admissible`（艺术家否决）。
        # 阶段 0 专门处理"词序颠倒"（`Daft Punk - Get Lucky` ↔
        # `Get Lucky - Daft Punk`），而 `artist` 缺失时回退用"名称首词"当艺术家
        # —— 词序一颠倒，首词就变成了对方的名字，艺术家相似度必然很低，
        # 一加否决就会把这一类本来就该配上的场景全挡掉
        # （`test_word_order_difference_pairs` 守着这条）。
        # 时长门禁足够挡掉"40 秒片段被当成 5 分钟原曲"那种误配。
        used_r.add(id(b))
        m0.append(m)

    # 阶段 1：归一化文件名精确相同。
    # 这是最强的证据（不受两侧标签写法差异影响），因此优先级最高：
    # 例：Windows 有 Pop/ 与 Backup/ 两份同名 Get Lucky，
    #     若先跑"标题+艺术家"阶段，Linux 那一份会被 Pop 版抢走、Backup 版无人可配。
    # ★ 必须把阶段 0 已经配上的左侧排除掉。
    #   原来这里传的是**整个 `left`**，于是同一个左文件能被配两次：
    #   实测 left 只有 1 条、right 2 条 → 产出 2 个配对，`matched=2 > left_total=1`；
    #   跨库查重时这两条配对会被 union 成"一组 3 条"，其中两条来自同一个库
    #   且是两个**不同的文件**，界面还提示"保留一份、清理其它"。
    #   这也直接违反了本函数 docstring 的承诺（"每阶段只处理上一阶段没配上的"）。
    rest_l0 = [x for x in left if id(x) not in {id(m.left) for m in m0}]
    m1 = _pair_by_key(rest_l0, right, used_r,
                      lambda x: x.get("name_key") or P.name_key(x.get("name") or ""),
                      tolerance, threshold, require_duration_close=True, apply_fuzzy_gates=True)
    # 阶段 2：歌键（标题 + 主艺术家 + 时长桶），补文件名被改写过的场景
    #
    # ★ 这里的时长桶要跟 `tolerance` 走，不能用默认的 2 秒。
    #   阶段 2 是**精确字符串键**匹配，桶号不同就永远进不了同一个候选池 ——
    #   而阶段 1（文件名键）是有 `tolerance` 判断的，于是出现这种不一致：
    #
    #     文件名相同 → 时长差 3 秒能配（阶段 1 的 tolerance 生效）
    #     文件名不同 → 时长差 3 秒配不上，**而且调大容差也没用**
    #
    #   把桶放宽到 2×tolerance 只是**扩大候选池**；能不能配仍由
    #   `score_pair` 的时长门禁和 `fuzzy_admissible` 把关，不会造成误配。
    paired_l = {id(m.left) for m in (m0 + m1)}
    rest_l = [x for x in left if id(x) not in paired_l]
    _tol2 = float(tolerance or 2.0)
    if _tol2 <= 0:
        _tol2 = 2.0
    _gate2 = 2.0 * _tol2                       # 与 `duration_similarity` 同一个数
    _step2 = max(2.0, _gate2)                  # 桶距 = 门禁窗口 → 覆盖完整
    m2 = _pair_by_key(left, right, used_r,
                      lambda x: dup_key(x, bucket_step=_step2),
                      tolerance, threshold, filter_left=rest_l,
                      apply_fuzzy_gates=True, duration_gate=_gate2)

    matches: List[Match] = list(m0) + list(m1) + list(m2)
    matched_l = {id(m.left) for m in matches}
    unmatched_l = [x for x in left if id(x) not in matched_l]
    remaining_r = [x for x in right if id(x) not in used_r]

    # 阶段 3：模糊兜底
    if remaining_r and unmatched_l:
        # ★ 分桶必须由 `tolerance` 推导，别写死。
        #   这里原来是 `int(d // 3)` + 邻居 ±2 —— 等效 ±9 秒的**硬上限**，
        #   和 `group_duplicates` 当初那个 bug 是同一类：用户调"时长容差"
        #   对这一步毫无影响。既然这一步本来就是"猜"（要过
        #   `fuzzy_admissible` 门禁），窗口更应该跟着用户设的容差走。
        #   用多偏移分桶保证覆盖（同 `group_duplicates` 的做法）。
        try:
            _tol3 = float(tolerance)
        except (TypeError, ValueError):
            _tol3 = 2.0
        if _tol3 <= 0:
            _tol3 = 2.0
        _win3 = 2.0 * _tol3
        _K3 = 3
        r_by_dur: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for x in remaining_r:
            d = x.get("duration")
            if not d:
                continue
            b0 = int(float(d) / _win3)
            for off in range(_K3):
                r_by_dur[b0 - off].append(x)
        cands: List[Tuple[float, Dict[str, Any], Dict[str, Any]]] = []
        still_l: List[Dict[str, Any]] = []
        for a in unmatched_l:
            best: Optional[Tuple[float, Dict[str, Any]]] = None
            if not a.get("duration"):
                # 没有时长：只允许"文件名几乎一样 + 体积相同"的窄路
                for b in remaining_r:
                    if id(b) in used_r or b.get("duration"):
                        continue
                    if version_mismatch(a, b):
                        continue
                    m = score_pair(a, b, tolerance)
                    if m.name_sim >= 0.95 and m.left.get("size") == m.right.get("size") \
                            and fuzzy_admissible(m):
                        if best is None or m.score > best[0]:
                            best = (m.score, b)
            else:
                bucket = int(float(a["duration"]) / _win3)
                pool: List[Dict[str, Any]] = []
                for off in (-1, 0, 1):
                    pool.extend(r_by_dur.get(bucket + off, []))
                for b in pool:
                    if id(b) in used_r:
                        continue
                    # 兜底阶段是"猜"，版本标记不一致就直接不猜
                    if version_mismatch(a, b):
                        continue
                    m = score_pair(a, b, tolerance)
                    if m.score < max(threshold + 0.06, 0.7):
                        continue
                    # 注意：这里**故意不加**时长门禁。
                    #
                    #   试过加 `duration_diff > 2×tolerance 就拒`，被
                    #   `test_same_name_large_duration_gap_still_pairs` 挡回来了：
                    #   项目的决定是"**同名是铁证**" —— 标签器估算时长有误差，
                    #   同名文件差 8 秒仍然要配成同一首。
                    #
                    #   代价是清楚的：同名但时长差很多（比如一首是试听片段）
                    #   也会被当成"两边都有"，于是不会生成补齐步骤。这个取舍
                    #   记在 `docs/KNOWN_ISSUES.md` 里，不要当成 bug 再来"修"。
                    # 跨名称猜测必须过门禁
                    if not fuzzy_admissible(m):
                        continue
                    if best is None or m.score > best[0]:
                        best = (m.score, b)
            if best:
                cands.append((best[0], a, best[1]))
            else:
                still_l.append(a)
        cands.sort(key=lambda t: -t[0])
        taken: set = set()
        for _sc, a, b in cands:
            if id(a) in taken or id(b) in used_r:
                continue
            # ★ 兜底配对也要有**分数下限**。
            #
            #   原来这里不看分数：只要进了 `cands` 就配。
            #   而 `cands` 的门槛是 `max(threshold+0.06, 0.7)` = 0.76（默认），
            #   于是实测出这种配对：
            #
            #     "Bohemian Rhapsody.flac"  ↔ "Smells Like Teen Spirit.flac"
            #     同一艺术家、时长相同、体积相同 → score 0.7336，**配上了**
            #
            #   而且更糟的是这两个文件本来**各自都不在对方库里**，
            #   被强行配成一对之后，对比页会把它们都从"独有"里拿掉 ——
            #   用户就看不到"这两首歌各缺一份"，反而以为两边都有。
            #
            #   宁可不配（两边都算"独有"），也不要瞎配：
            #   漏配的表现是"显示成独有"，用户一眼能看出；
            #   瞎配的表现是"悄悄没了"，用户根本发现不了。
            #
            #   下限取 0.86 —— `confidence_label` 里"中等"那一档的起点，
            #   也是 `stats.suspect` 的分界：低于它的配对会被标成
            #   "仅相似"，本来就不该自动当成同一首歌。
            if _sc < 0.86:
                continue
            # ★ 兜底阶段的**名称相似度**也要有下限。
            #
            #   只看总分不够：`size_similarity` 同体积给 1.0、
            #   `duration_similarity` 时长短差给 1.0，两项加起来占 0.46 权重
            #   —— 于是"同艺术家 + 时长几乎相同 + 体积相同"就能把总分顶到
            #   0.86，哪怕名字根本不相关。实测：
            #
            #     "Alpha Song.flac" ↔ "Beta Song.flac"  → 0.8608 配上了
            #     （名字相似度只有 70%）
            #
            #   前两个阶段用的是**精确键**（文件名键 / 歌键），
            #   本来就该严格；这一阶段是"猜"，不该比它们更宽松。
            #   名字像到 85% 以上才允许进入这一层，
            #   既挡住上面那种"共享通用后缀"的误配，
            #   又不影响词序颠倒（`Get Lucky - Daft Punk` 那种，
            #   在 m0 阶段就配掉了，根本走不到这里）。
            _m_best = score_pair(a, b, tolerance)
            if _m_best.name_sim < 0.85:
                continue
            taken.add(id(a))
            used_r.add(id(b))
            matches.append(_m_best)
        matched_l = {id(m.left) for m in matches}
        unmatched_l = [x for x in left if id(x) not in matched_l]

    only_right = [x for x in right if id(x) not in used_r]
    only_left = unmatched_l

    matches.sort(key=lambda m: (-m.score, (m.left.get("path") or "")))
    return {
        "matches": matches,
        "only_left": only_left,
        "only_right": only_right,
        "stats": {
            "left_total": len(left),
            "right_total": len(right),
            "matched": len(matches),
            "only_left": len(only_left),
            "only_right": len(only_right),
            "identical": sum(1 for m in matches if m.identical),
            "suspect": sum(1 for m in matches if m.score < 0.86),
        },
    }


# ------------------------------------------------------------------ 同库查重

def group_duplicates(rows: Sequence[Dict[str, Any]], tolerance: float = 2.0,
                     threshold: float = 0.9) -> List[List[Dict[str, Any]]]:
    """同一个库内部找出疑似重复（同一首歌的多个副本）。

    先按 dup_key 精确分组，再对"键不同但名称/时长很像"的组做合并（并查集）。
    """
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        # 一律就地重算，不信任索引里缓存的那一列：dup_key 的算法会演进
        # （例如新增"版本标记"），而增量扫描会复用旧行的缓存值，
        # 若直接读库里的旧键，新规则要等到整库重扫才生效。
        k = dup_key(r)
        if not k or k.startswith("|"):
            _nk = P.name_key(r.get("name") or "")
            # ★ 归一化后为空（`伴奏.flac` / `01.mp3` / `纯音乐.mp3` 这类）
            #   绝不能全落进 `"name:"` 常量桶 —— 那会把全库所有无标签文件
            #   判成同一首歌的重复版本，界面还建议"只留一份"。
            #   退回原始文件名（去扩展名）分桶。
            if _nk:
                k = "name:" + _nk
            else:
                # ★ 光用 stem 分桶不够：全库的 `01.flac` / `伴奏.mp3` 会全落进
                #   同一个 "raw:01"，被当成同一首歌的多个副本建议删除。
                #   加上"时长 5 秒桶 + 艺术家键"把它们隔开。
                _sec = int(float(r.get("duration") or 0) // 5)
                _ar = P.name_key(str(r.get("artist") or "")) or "-"
                k = f"raw:{P.stem(r.get('name') or '')}|{_sec}|{_ar}"
        # 回写：group_to_dict() 用 row["dup_key"] 作为组键，
        # 不回写的话组键会取到库里的旧值/空值，不同组可能撞成同一个 key。
        r["dup_key"] = k
        groups[k].append(r)

    keys = list(groups)
    parent = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 用代表项近似比较。
    #
    # 性能要点：score_pair() 里有 difflib 序列比对，是这层最贵的一步。
    # 2000 首的库里唯一键接近曲目数，全部两两打分要几十秒到几分钟（实测 179s）。
    # 但"能不能合并"其实由几个**字符串相等**条件决定（见下面的合并门禁），
    # 所以先把这些廉价判断做完，只有真正通过门禁的候选才值得调用 score_pair。
    reps = {k: max(v, key=lambda x: len(str(x.get("name") or ""))) for k, v in groups.items()}

    def _version_of(k: str, r: Dict[str, Any]) -> str:
        """这个组的版本标记。

        ★ 必须与 `dup_key` **同源**，不能再自己算一遍。

        踩过：这里原来写的是
            P.version_tags(r["name"], r["title"])
        而 `dup_key` 用的是
            P.version_tags(r["name"], r["title"], r["rel_path"])
        于是当版本标记只出现在**目录名**里时，两边结论相反：
        `dup_key` 把它们分到两组（版本段不同），而合并门禁看到
        两个 groups 的"版本"都是空串、判定为同版本，又把它们并回来。

        线上实测的受害者：
            `野火 Wildfire (伴奏) - HOYO-MiX.flac`      （目录名带"伴奏"）
            `野火 Wildfire - HOYO-MiX, ...flac`          （原曲）
        被并成同一组 —— 界面提示"清理其中一份"，而那是两个**不同的录音**。

        直接从已经算好的 `dup_key` 尾段取版本，两处就永远不会再打架。
        """
        dk = str(r.get("dup_key") or k or "")
        # `dup_key` 的形状是 `title|artist|bucket|ver`，前三段各自不含 `|`，
        # 但 **ver 自己可能含 `|`**（多个标记，如 `instrumental|remix`）。
        # 所以必须用 split(maxsplit=3) 从**左边**切三段，剩下全是版本 ——
        # 用 rsplit 会把 `instrumental|remix` 切成 `remix`，于是
        # "原曲 vs 伴奏"又被判成同版本（实测就是这么漏掉一组的）。
        parts = dk.split("|", 3)
        return parts[3] if len(parts) == 4 else ""

    sigs = {}
    for k, r in reps.items():
        ka = r.get("name_key") or P.name_key(r.get("name") or "")
        sigs[k] = {
            "name_key": ka,
            "tokens": frozenset(t for t in ka.split() if t),
            "version": _version_of(k, r),
        }
    # 时长分桶：只和**邻近桶**比，避免全库两两打分。
    #
    # ★ 必须**保证覆盖**下面那道时长门禁，否则门禁会被分桶边界悄悄绕过。
    #
    #   踩过两次，值得写清楚：
    #   1. 原来是 `int(d // 3)` + 邻居 ±1 —— 等效 ±6 秒硬上限，
    #      而且**完全不理会 tolerance**：容量从 2 调到 10 毫无变化。
    #   2. 改成"桶宽 = tolerance、邻居 ±1"之后仍会漏：整数除法 + 落在
    #      桶边界上，`int(a/w)` 和 `int(b/w)` 的差可能比预期大一，
    #      于是 3.6 秒的一对在容差 2.0 下时收时不收（真实库里没并、
    #      合成数据里并了 —— 同样是"边界靠运气"）。
    #
    #   现在用标准的**多偏移分桶**：每行按 K 个等距偏移各进一个桶，
    #   查询时取主桶的邻居。只要 `K × bucket_w ≥ 门禁窗口`，
    #   任意落在门禁内的一对**必然**共享至少一个桶 —— 覆盖是构造出来的，
    #   不靠运气。
    try:
        _tol = float(tolerance)
    except (TypeError, ValueError):
        _tol = 2.0
    if _tol <= 0:
        _tol = 2.0
    _window = 2.0 * _tol            # 与 `duration_similarity` 的门禁同一个数
    _K = 3                          # 偏移个数
    bucket_w = _window              # 桶距 = 窗口；K 个偏移 → 总覆盖 K×窗口
    by_dur: Dict[int, List[str]] = defaultdict(list)
    for k, r in reps.items():
        d = r.get("duration")
        if not d:
            continue
        b0 = int(float(d) / bucket_w)
        for off in range(_K):
            by_dur[b0 - off].append(k)
    for k, r in reps.items():
        d = r.get("duration")
        if not d:
            continue
        sk = sigs[k]
        bucket = int(float(d) / bucket_w)
        for off in (-1, 0, 1):
            for k2 in by_dur.get(bucket + off, []):
                if k2 == k or find(k) == find(k2):
                    continue
                s2 = sigs[k2]
                # ---- 先做廉价的合并门禁（字符串比较，零成本）----
                #
                # 没有这道门禁会严重**过度合并**："体积相同 + 时长接近"就能把分数
                # 顶到阈值（size_similarity 同体积给 1.0，标签都缺时 meta_similarity
                # 给中性 0.5），于是整张专辑被并成一个巨型组，看起来就像"查重只有几组"。
                #
                # 也不能用普通相似度阈值：difflib 对 "song 01"/"song 02" 这种
                # 只差一两个字符的不同歌也给 0.857，照样误并。
                # 所以只承认三种"确实是同一首歌的另一份"的证据：
                #   1) 标题键完全相同 —— 同一首歌、时长落在同一个桶
                #   2) 文件名归一化后完全相同 —— 改名的同一份文件
                #   3) 词元集合一致且 ≥2 个词（容纳词序/标点差异）
                name_equal = bool(sk["name_key"]) and sk["name_key"] == s2["name_key"]
                same_key = r.get("dup_key") == reps[k2].get("dup_key")
                tokens_equal = len(sk["tokens"]) >= 2 and sk["tokens"] == s2["tokens"]
                if not (name_equal or same_key or tokens_equal):
                    continue
                # 版本不同（原曲 vs 伴奏 / 重制版 / 现场版 …）绝不合并：
                # 它们是不同的录音，哪怕名字和时长都像。
                if sk["version"] != s2["version"]:
                    continue
                # ★ 时长差要**显式**把关，不能只靠分桶窗口。
                #
                #   分桶只是为了缩小候选范围（性能）。阈值与
                #   `duration_similarity` 的门禁**保持同一个数**
                #   （2×tolerance），两处永远不会打架。
                _da, _db = r.get("duration"), reps[k2].get("duration")
                if _da and _db:
                    if abs(float(_da) - float(_db)) > _window:
                        continue
                # ---- 廉价门禁全过，才付出打分代价 ----
                m = score_pair(r, reps[k2], tolerance)
                if m.score < threshold:
                    continue
                if artist_conflict(m):
                    continue
                if not (name_equal or same_key or tokens_equal):
                    continue
                    continue
                union(k, k2)

    merged: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for k, items in groups.items():
        merged[find(k)].extend(items)
    out = [v for v in merged.values() if len(v) > 1]
    out.sort(key=lambda g: (-len(g), str(g[0].get("name") or "").lower()))
    return out


def merge_matched_pairs(pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
                        ) -> List[List[Dict[str, Any]]]:
    """把"两两配对"的结果合并成**互不重叠**的组。

    ## 为什么必须合并

    多库跨库查重是拿**每一对库**分别比一次的。3 个库里各有一份同一首歌时，
    会产生 3 个配对：

        A+B、A+C、B+C

    如果每对各自成组，**同一份文件会同时属于 2 个组**。前端"每组保留一份"
    的规则立刻就自相矛盾了 —— 实测会生成把同一个文件删两次的计划，
    而且"保留哪一份"可能在组之间打架（在组 1 里选了保留 A，组 2 却把 A 删了）。
    跑起来第二条报 `FileNotFoundError: 系统找不到指定的文件`（幻影错误），
    用户会以为出事了。

    合并之后 3 份变成**一组 3 条成员**，和单库查重的组形状完全一致 ——
    `clusters` / `suggested_keep` / 前端"保留策略"这些现成机制直接复用，
    不需要为多库再造一套。

    ## 做法

    并查集（和 `group_duplicates` 同一个套路）：每条记录按
    `(source_id, path)` 取键，配对就是把两个键 union 起来，
    最后按根聚合。**不用 `dup_key`** —— 那东西不含 source_id，
    两个库里同一首歌的 key 完全相同，会把不同源的文件混成一个键。
    """
    parent: Dict[Tuple[str, str], Tuple[str, str]] = {}

    def k_of(r: Dict[str, Any]) -> Tuple[str, str]:
        return (str(r.get("source_id") or ""), str(r.get("path") or ""))

    def find(x: Tuple[str, str]) -> Tuple[str, str]:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:          # 路径压缩
            parent[x], x = root, parent[x]
        return root

    def union(a: Tuple[str, str], b: Tuple[str, str]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # 字典序小的当根 —— 结果与输入顺序无关，可复现
            if rb < ra:
                ra, rb = rb, ra
            parent[rb] = ra

    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for a, b in pairs:
        ka, kb = k_of(a), k_of(b)
        # 同一份文件自己和自己配（理论上不该有）直接跳过，别造出 1 条成员的组
        if ka == kb:
            continue
        by_key.setdefault(ka, a)
        by_key.setdefault(kb, b)
        union(ka, kb)

    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for k, r in by_key.items():
        buckets.setdefault(find(k), []).append(r)
    # 组的顺序也定住（按根键排序），否则同一份数据两次跑出来的组序不同
    return [buckets[k] for k in sorted(buckets)]


def pick_representative(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """组内"最好的一份"：质量分最高，其次体积、时长完整度。

    ★ key 末尾要带 `path`：前几项全并列时（同一张专辑里两份**完全一样**
    的音轨，质量/体积/时长都相同），原来取的是"参数顺序里的第一个" ——
    而 `group_duplicates` 传进来的顺序会随排序、扫描顺序变化。
    于是同一组在不同时候算出来的"建议保留"可能是**不同的那一个**，
    用户按建议删就会删掉不同的文件。加一个稳定的兜底键就定住了。
    """
    def key(r: Dict[str, Any]):
        return (
            r.get("quality") or 0,
            r.get("is_lossless") and 1 or 0,
            r.get("bitrate") or 0,
            r.get("size") or 0,
            r.get("duration") or 0,
            # 稳定兜底：路径字典序最大者胜（纯为了"每次都一样"）
            str(r.get("path") or ""),
        )

    if not group:
        return {}
    return max(group, key=key)


def group_to_dict(group: Sequence[Dict[str, Any]], keep_path: Optional[str] = None) -> Dict[str, Any]:
    # ★ 空组要能安全返回。
    #   原来直接 `pick_representative(group)` 再 `.get(...)` —— 空列表会
    #   `ValueError`（max of empty sequence）。当前所有调用点都有长度保护，
    #   属于潜伏问题，但接口一旦被别处复用就会炸得莫名其妙。
    if not group:
        return {"key": "", "base_key": "", "title": "", "artist": "",
                "album": "", "duration": None, "count": 0, "items": [],
                "suggested_keep": keep_path or "", "wasted_bytes": 0,
                "total_bytes": 0, "best_quality": 0, "version_tags": ""}
    rep = pick_representative(group)
    ordered = sorted(group, key=lambda r: -(r.get("quality") or 0))
    total = sum((r.get("size") or 0) for r in ordered)
    keep = keep_path or rep.get("path")
    wasted = sum((r.get("size") or 0) for r in ordered if r.get("path") != keep)
    dk = rep.get("dup_key") or P.name_key(rep.get("name") or "")
    # 版本无关的组键（去掉最后的版本段），供界面"不区分版本"视图把
    # 原曲/伴奏/重制版重新并成一个总体视图使用。
    parts = dk.rsplit("|", 1)
    base_key = parts[0] if len(parts) == 2 else dk
    return {
        "key": dk,
        "base_key": base_key,
        "title": rep.get("title") or P.stem(rep.get("name") or ""),
        "artist": rep.get("artist") or "",
        "album": rep.get("album") or "",
        "duration": rep.get("duration"),
        "count": len(ordered),
        "items": ordered,
        "suggested_keep": keep,
        "wasted_bytes": wasted,
        "total_bytes": total,
        "best_quality": rep.get("quality") or 0,
        # 同上：与 `dup_key` 的字段集保持一致，否则界面显示的版本标记
        # 会和分组键里的版本段对不上。
        "version_tags": P.version_tags(rep.get("name") or "", rep.get("title") or "",
                                       rep.get("rel_path") or ""),
    }


def naming_conflicts(left: Sequence[Dict[str, Any]], right: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一首歌两边都有、但文件名不同（"名字打架"清单）。"""
    lmap = {P.name_key(r.get("name") or ""): r for r in left}
    out = []
    for r in right:
        k = P.name_key(r.get("name") or "")
        o = lmap.get(k)
        if o is None:
            continue
        if o.get("name") != r.get("name"):
            out.append({"left": o, "right": r, "score": 1.0})
    return out

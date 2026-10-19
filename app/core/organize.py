# -*- coding: utf-8 -*-
"""智能整理：判断"这首歌是不是放错地方了"，并给出**最小的**纠正动作。

和上一版的根本区别
------------------
上一版是"按模板重排整个库"：算出每首歌在 `作者/专辑` 模板下该去哪，然后把
不一致的全部搬走。对一个真实的、用户自己整理过的库，这等于把人家的工作推倒
重来 —— 实测会给出"移动 2301 首、新建 1495 个目录"，那是破坏不是整理。

这一版反过来：**假设现有结构是对的**，只在能证明某一首歌放错了位置时才动它。

判定"放错了"（misplaced）
-------------------------
一首歌的目录里，没有任何**同作者或同专辑**的邻居 → 它很可能是走错了门。
反过来，只要目录里有同作者或同专辑的歌，就认为它在对的地方：
你可能只按作者分了目录、只按专辑分了目录、或者两者都有，工具都尊重。

收纳区（sink）
--------------
`单曲01`、`英语歌精选集`、`日语歌精选集` 这类"故意把各种歌收在一起"的目录，
天生就是混放的，**不能当成错误**。它们用两种方式识别：
  * 目录名匹配常见写法（单曲/精选/合集/杂项/未分类/singles/misc…）
  * 用户在界面上手动指定（`sinks` 参数）
收纳区里的歌一律不动；反过来，放错位置的歌也优先收进收纳区，而不是新建目录。

只有在真的找不到合适去处、且同组歌攒够了数量（默认 ≥5）时，才会建议新建目录。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import paths as P

# 判定"一个组值得单独建文件夹"的默认曲目数
DEFAULT_MIN_GROUP = 5

TEMPLATE_FIELDS = ("artist", "album", "year", "codec")

# 目录名里出现这些词 → 认为它是"故意混放"的收纳区。
# 匹配规则见 is_sink_name：关键词要出现在**开头或结尾**（允许后面接编号或
# 一两个语气尾巴字），不能用简单子串搜索 —— 否则 `其他人的目录` 会被
# `其他` 误判成收纳区，用户精心分好的目录就被当成垃圾堆了。
SINK_PATTERNS = [
    "单曲", "散曲", "精选", "精选集", "合集", "合辑", "杂项", "杂集", "未分类",
    "其他", "其它", "待整理", "临时", "下载", "新建文件夹",
    "single", "singles", "misc", "mixed", "various", "unsorted",
    "collection", "best", "compilation", "random", "inbox", "other",
]
# 关键词后面允许出现的"尾巴"（编号或语气字）：`单曲01`、`精选集`、`合集` 等
_TAIL_CHARS = "集辑选曲单集"


def _keyword_hits(leaf: str, kw: str) -> bool:
    if not leaf or not kw:
        return False
    if leaf == kw:
        return True
    if leaf.startswith(kw):
        rest = leaf[len(kw):]
        # 后面接编号/分隔符/语气尾巴才算，避免把 `singlelegend` 误伤成收纳区
        return bool(rest) and (not rest[0].isalpha() or rest[0] in _TAIL_CHARS)
    if leaf.endswith(kw):
        # ★ 英文关键词必须要求词边界。
        #   原来这里裸 `endswith`，而 SINK_PATTERNS 里有 `"other"` ——
        #   于是 `Brother` / `Mother` / `Another` / `Each Other` 这些**正常的**
        #   艺术家/专辑目录全被判成"收纳区"，里面的歌永远不会被整理。
        #   中文关键词没有词边界概念，保持原样（`我的单曲` 之类要能命中）。
        if kw.isascii():
            if len(leaf) == len(kw):
                return True
            prev = leaf[-len(kw) - 1]
            return not prev.isalpha()
        return True
    return False


def is_sink_name(rel_or_name: str) -> bool:

    """目录名看起来是不是"故意混放"的收纳区。"""
    if not rel_or_name:
        return False
    leaf = P.norm(rel_or_name).rstrip("/").rsplit("/", 1)[-1].strip()
    if not leaf:
        return False
    low = leaf.lower()
    for kw in SINK_PATTERNS:
        if _keyword_hits(leaf, kw) or _keyword_hits(low, kw.lower()):
            return True
    return False


def _safe_seg(name: str, fallback: str = "") -> str:
    """把标签值变成安全的目录名（去掉路径分隔符与非法字符）。

    ★ 走 `paths.safe_name`（唯一来源）。原来这里只换了非法字符，
      专辑/艺术家标签叫 `Con` / `Nul` / `Aux`（真有这样的乐队）时，
      整理会生成 `Con/` 目录 —— 资源管理器里打不开也删不掉；
      `Nul/` 更糟，往里搬的每首歌都会报"找不到路径"。
    """
    return P.safe_name(name, fallback)


# remix / 翻唱 / 改编的标志。这类文件的 artist 标签往往是**原作者**，
# 而真正的归属是"谁做的这个版本"，所以不能拿 artist 去判断该放哪个目录。
_REMIX_RE = re.compile(
    r"remix|bootleg|flip|edit|mashup|mash\s?up|rework|vip|cover|ver\.|version|"
    r"混音|重制|翻唱|改编|试听|伴奏|纯音乐",
    re.I)


def is_derivative(name: str, title: str = "") -> bool:
    """这首歌是不是"派生作品"（remix / 翻唱 / 改编…）。

    派生作品的标签极易误导：`K-391_Alan Walker_..._Play（白井冰Shiroi-Ice remix）
    - Shiroi-Ice.flac` 的 artist 标签是 `K-391/Alan Walker/...`（原作者），
    但它其实是 Shiroi-Ice 的作品，放在 `Shiroi-ice/` 里是对的。
    """
    return bool(_REMIX_RE.search(f"{name or ''} {title or ''}"))


# 文件名里" - 某某"这种后缀，常常是真正的表演者。
# `Astrogirl(Shiroi-Ice★Bootleg) - Shiroi-Ice.flac` → 末尾是 Shiroi-Ice
def filename_artist(name: str) -> str:
    """从文件名末尾取" - 艺术家"里的艺术家（取不到就返回空串）。"""
    if not name:
        return ""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    # 从右往左找最后一个" - "（曲名里也可能有连字符，取最右的更保险）
    for sep in (" - ", " – ", " — "):
        idx = stem.rfind(sep)
        if idx > 0:
            tail = stem[idx + len(sep):].strip()
            # 去掉后面的括号补充说明
            tail = re.sub(r"[（(\[].*?[)）\]]", " ", tail).strip()
            if 1 < len(tail) <= 60:
                return tail
    return ""


def _rel(root: str, path: str) -> str:
    r = P.norm(root).rstrip("/")
    p = P.norm(path)
    return p[len(r) + 1:] if r and p.startswith(r + "/") else p


def _folder_of(path: str) -> str:
    return P.parent(path)


def build_folders(rows: Sequence[Dict[str, Any]], root: str) -> Dict[str, Dict[str, Any]]:
    """每个目录的画像：装了哪些作者/专辑、有多少首、是不是收纳区。"""
    root_n = P.norm(root)
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        rel = _rel(root_n, r.get("path") or "")
        buckets[_folder_of(rel)].append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for folder, items in buckets.items():
        artists: Dict[str, int] = defaultdict(int)
        albums: Dict[str, int] = defaultdict(int)
        for i in items:
            a = str(i.get("artist") or "").strip()
            al = str(i.get("album") or "").strip()
            if a:
                artists[a] += 1
            if al:
                albums[al] += 1
        out[folder] = {
            "folder": folder,
            "leaf": folder.rsplit("/", 1)[-1] if folder else "",
            "count": len(items),
            "artists": dict(artists),
            "albums": dict(albums),
            "items": items,
            "sink": is_sink_name(folder) or not folder,
        }
    return out


def find_sinks(rows: Sequence[Dict[str, Any]], root: str,
               extra: Optional[Sequence[str]] = None) -> List[str]:
    """找出所有"收纳区"目录（自动识别 + 用户手动指定）。"""
    folders = build_folders(rows, root)
    out = {f for f, d in folders.items() if d["sink"] and f}
    for e in (extra or []):
        # ★ 调用方（前端 / API）传的是**绝对路径**，而 folders 的键是
        #   源内相对路径。不转换就永远不相等 —— 用户手动指定的收纳区
        #   会静默失效（界面显示"已指定"，实际一个都没生效）。
        e = _rel(root, P.norm(str(e)))
        if e in folders:
            out.add(e)
    return sorted(out)


def _is_same_identity(folder_seg: str, want: str) -> bool:
    """某一层目录名和"要匹配的名字"是不是**同一个身份**（对称判断）。

    这是整个匹配体系的核心，专辑、作者、收纳区都用它，避免各处各写一套
    松紧不一的规则（那正是之前"按专辑分却匹配到作者目录"的根源）。

    判定顺序：
      1. 归一化后完全相等 → 是
      2. 两边的"区分词"集合相等 → 是（`Oneshot - Solstice` ≡ `Oneshot_ Solstice`）
      3. 一边的区分词是另一边的**真子集**，且多出来的词都很短（≤3 字符）
         → 是（`洛天依Official` ≡ `洛天依`、`Hope` ≡ `HOPE-T`）
      4. 相似度 ≥0.92，且长度相近、无冲突词 → 是（纯拼写差异）
      5. 否则 → 否

    **对称**很重要：`Minecraft` 是 `Minecraft - Caves & Cliffs (…)` 的子集，
    但多出来的 `caves` / `cliffs` 很长，第 4 条不成立，于是拒绝 ——
    正是我们要的。
    """
    a = str(folder_seg or "").strip()
    b = str(want or "").strip()
    if not a or not b:
        return False
    na, nb = P.normalize_text(a), P.normalize_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    da, db = _distinctive(a), _distinctive(b)
    if da and db:
        if da == db:
            return True
        extra_a, extra_b = da - db, db - da
        # 一边是另一边的真子集：只有当多出来的词都是**尾部后缀**时才认。
        #   `洛天依Official` vs `洛天依`  extra={'official'} 在末尾 → 认
        #   `Hope` vs `HOPE-T`            extra={'t'} 在末尾        → 认
        #   `Owl city` vs `City`          extra={'owl'} 在**开头**  → 拒绝
        #   `Minecraft - Caves & Cliffs…` vs `Minecraft`
        #                                 extra 有 caves/cliffs，且 cliffs 不在末尾 → 拒绝
        # 后缀词本来就该短，所以再加一条长度上限，避免
        # `X Original Game Soundtrack` 这种长尾被当成后缀（那些词在 _NOISE 里，
        # 但万一有新词也能挡住）。
        if bool(extra_a) != bool(extra_b):
            longer_is_b = bool(extra_b)
            longer = b if longer_is_b else a
            extra = extra_b if longer_is_b else extra_a
            toks = _tokens(longer)
            if len(toks) <= len(extra):
                return False                     # 整条名字都是"多出来的"，太牵强
            tail = set(toks[len(toks) - len(extra):])
            if extra <= tail and all(len(t) <= 3 for t in extra):
                return True
            return False
        # 各有对方没有的实质词 → 不是同一个
        if extra_a and extra_b:
            return False
    if len(na) < 4 or len(nb) < 4:
        return False
    short, long_ = sorted((len(na), len(nb)))
    if long_ > short * 2:
        return False
    if _conflicting_identity(a, b):
        return False
    return P.similarity(na, nb) >= 0.92


def folder_matches_name(folder: str, want: str) -> bool:
    """目录（含各层）的名字是不是**就是** `want` 指的那个东西。

    原来这里是 `s == w or w in s or s in w`（子串包含），太松了：
    作者标签 `Minecraft` 会命中目录
    `Minecraft - Caves & Cliffs (Original Game Soundtrack)`
    —— 于是"按作者整理"把 6 首 Minecraft 歌塞进了一张 Minecraft 原声专辑目录。
    真实库里 47 组里就这一组离谱，但性质很严重。

    现在改成和专辑匹配**同一套身份判断**（`_is_same_identity`），
    并且是**对称**的：谁比谁更具体都不行，只有"同一个身份"才算。
    这样：
      · `周杰伦` → `周杰伦`、`周杰伦/范特西`         ✅ 同名层
      · `洛天依Official` → `洛天依`                  ✅ 只多一个后缀词
      · `Shiroi-Ice_Rigel Music` → `Shiroi-ice`      ✅ 同上
      · `Minecraft` → `Minecraft - Caves & Cliffs…`  ❌ 差太多，拒绝
    """
    w = str(want or "").strip()
    if len(P.normalize_text(w)) < 2 or not folder:
        return False
    for seg in P.norm(folder).split("/"):
        if seg and _is_same_identity(seg, w):
            return True
    return False


def has_same_artist_cohort(folder: str, rec: Dict[str, Any],
                           folders: Dict[str, Dict[str, Any]],
                           *, min_count: int = 2, threshold: float = 0.8) -> bool:
    """这个目录里有没有**别的**同作者的歌（容忍作者名拼写差异）。

    这是"它已经在对的地方"最可靠的证据：作者标签通常是对的（错的多是专辑
    标签），而用户会把同一作者的歌聚在一个目录里。

    用模糊比较而不是精确相等，是因为真实库里目录名常跟标签差一两个字母：
    用户的 `Vicetone/` 里躺着 8 首 Vexento 的歌（目录名拼错了），
    精确比较会认为"这个目录里没有同作者的歌"，从而把好歌判成放错位置。
    """
    d = folders.get(folder) or {}
    artists = d.get("artists") or {}
    if not artists:
        return False
    artist = str(rec.get("artist") or "").strip()
    if not artist:
        return False
    owner = rec.get("_name") or rec.get("name") or ""
    for a, n in artists.items():
        if a == artist:
            # 减掉自己
            if n - (1 if P.basename(owner) == P.basename(rec.get("name") or "") else 0) >= 1:
                return True
            if n >= min_count:
                return True
            continue
        if P.similarity(P.normalize_text(a), P.normalize_text(artist)) >= threshold:
            return True
    return False


def folder_fits(folder: str, rec: Dict[str, Any], depth: int = 2) -> bool:
    """目录名能不能对上这首歌的作者或专辑（最多看前 `depth` 层）。

    `Minecraft - Volume Alpha` 里 artist=C418、album=Minecraft - Volume Beta
    的歌：目录名对不上 album，但"看前两层"的规则让 `作者/专辑` 这种结构也能
    被正确识别（第 1 层对作者、前 2 层组合对专辑）。
    """
    if not folder:
        return False
    segs = [s for s in P.norm(folder).split("/") if s]
    keys = [P.normalize_text(str(rec.get(k) or "").strip())
            for k in ("artist", "album")]
    keys = [k for k in keys if len(k) >= 2]
    if not keys or not segs:
        return False
    for n in range(1, min(depth, len(segs)) + 1):
        cands = [P.normalize_text("/".join(segs[:n])), P.normalize_text(segs[n - 1])]
        for c in cands:
            for k in keys:
                # ★ 单字符目录（`A/`、`O/` 这种字母分区）不能走子串匹配：
                #   `c="o"`、歌手 `Coldplay` → "o" in "coldplay" 恒真，
                #   于是歌手名里带 o 的全部被判"已在正确目录"，整理彻底停摆。
                if c == k:
                    return True
                if len(c) >= 3 and len(k) >= 3 and (k in c or c in k):
                    return True
    return False


# ------------------------------------------------------------------ 计划

def plan_relocate(rows: Sequence[Dict[str, Any]], root: str,
                  sinks: Optional[Sequence[str]] = None,
                  scopes: Optional[Sequence[str]] = None,
                  min_group: int = DEFAULT_MIN_GROUP,
                  singles_dir: str = "单曲",
                  include_gather: bool = True,
                  fallback_sink: str = "",
                  orphan_policy: str = "keep",
                  trust_filename: bool = True,
                  gather_min: int = 0) -> Dict[str, Any]:
    """算出"把放错位置的歌挪回去"的最小动作集。

    `sinks`        手动指定的收纳区（会与自动识别的合并）
    `scopes`       只在这些目录里找问题（空 = 整个数据源）
    `min_group`    同组至少几首才值得为它新建目录
    `singles_dir`  兜底目录名；库里若已有同名目录会直接用它
    `fallback_sink` 明确指定"放错位置的歌统一收到哪个目录"（优先于
                   `singles_dir`）；不传就自动挑一个已有的收纳区
    `include_gather` 是否把"同组散在多处"也合并（默认开）
    `orphan_policy` 找不到任何现成去处的歌怎么处理：
                   `keep` 原地不动（默认，最保守 —— 只为"能证明放错了、
                          且有明确该去的地方"的歌动手）
                   `sink` 全部收进兜底收纳区
                   `new`  同组够 `min_group` 首就新建目录，否则收进收纳区
    `trust_filename` 搬动前要求"文件名支持目标目录"（默认开）。
                   关掉后标签说了算，标签有错时就会搬错地方。
    `gather_min`   合并"同一组散在多处"所需的最少曲目数（0 = 用 min_group）。
    """
    root_n = P.norm(root)
    folders = build_folders(rows, root_n)
    sink_set = set(find_sinks(rows, root_n, sinks))

    def _as_rel(p: str) -> str:
        """把用户给的目录统一成"相对数据源根"的形式。

        内部一律用相对路径（`整理区/别人的目录`），但用户/接口可能传绝对路径
        （`D:/M/整理区`）或带尾斜杠，这里都归一化掉，免得范围过滤静默失效。
        """
        s = P.norm(str(p)).rstrip("/")
        if not s:
            return ""
        if s == root_n:
            return ""
        if s.startswith(root_n + "/"):
            return s[len(root_n) + 1:]
        return s

    # 限定范围
    scope_set = {_as_rel(s) for s in (scopes or [])}
    scope_set.discard("")
    if scopes:
        def in_scope(f: str) -> bool:
            f = P.norm(f).rstrip("/")
            for s in scope_set:
                if f == s or f.startswith(s + "/"):
                    return True
            return False
    else:
        def in_scope(f: str) -> bool:
            return True

    # 手动指定的收纳区即使不在限定范围里也要纳入，否则会被当成普通目录处理
    extra_sinks = {_as_rel(s) for s in (sinks or [])}
    extra_sinks.discard("")
    sink_set |= extra_sinks

    # 兜底收纳区：放错位置又找不到合适去处的歌收进这里。
    # **优先复用用户已有的收纳区**（你的库里是 `0单曲`），而不是新建一个
    # 名字很像的新目录 —— 那只会让库里多出第二个"单曲"，更难找。
    # 优先级：显式指定 > singles_dir 点名且库里已存在 > 自动识别的收纳区
    #        > singles_dir 给的名字 > "单曲"
    sink_dest = _as_rel(fallback_sink) if fallback_sink else ""
    named = _as_rel(singles_dir) if singles_dir else ""
    if not sink_dest and named:
        for f in sorted(list(folders) + sorted(sink_set)):
            if f and (f == named or f.rsplit("/", 1)[-1] == named):
                sink_dest = f
                break
    if not sink_dest and sink_set:
        def sink_rank(f: str) -> Tuple[int, int]:
            """层级浅的优先，其次曲目多的（那通常就是主力收纳区）。"""
            return (len(f.split("/")), -int(folders.get(f, {}).get("count", 0)))
        sink_dest = sorted(sink_set, key=sink_rank)[0]
    if not sink_dest:
        sink_dest = named or "单曲"

    if orphan_policy not in ("keep", "sink", "new"):
        raise ValueError(f"不支持的 orphan_policy: {orphan_policy}")

    # 每个目标目录的存量：想挪进去的歌数，用来判断"值不值得新建"
    want: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    moves: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    unplaceable: List[Dict[str, Any]] = []

    # ---- 1) 找出"放错位置"的歌 ----
    # 判定依据是**这个目录里的主流跟这首歌的标签对不对得上**。
    #
    # 三条判据，按顺序：
    #   a) 收纳区（`单曲01`、`英语歌精选集`…）里什么都有，一律不动；
    #   b) 目录名对得上（`周杰伦/范特西` 里的歌）→ 位置正确；
    #   c) 目录里**大多数歌都属于同一个作者/专辑**（`Shiroi-ice/` 的 21 首
    #      同作者），而这首不是 → 它走错门了。
    # 第 (c) 条专门用来区分"收藏夹"和"某人的目录"：
    #   · `Shiroi-ice/` 有 21 首 Shiroi-Ice 的歌，混进去一首黄诗扶 → 明显放错
    #   · `洛天依/` 只有 1 首且不是洛天依的 → 说不定是用户自己按主题建的
    #     收藏夹，没有证据说人家放错了，不动它。
    # 也刻意不看"邻居数量"来判断：`周杰伦/范特西` 里只有 1 首也应该安全。
    def norm_key(s: Any) -> str:
        return P.normalize_text(str(s or "").strip())

    fits_keys = folder_fits          # 复用模块级实现（接口层也要用）

    def folder_majority(folder: str) -> Tuple[str, str, int]:
        """目录里占多数的 (作者, 专辑, 该组曲目数)。"""
        d = folders[folder]
        best_a = max(d["artists"].items(), key=lambda x: x[1]) if d["artists"] else ("", 0)
        best_al = max(d["albums"].items(), key=lambda x: x[1]) if d["albums"] else ("", 0)
        if best_a[1] >= best_al[1]:
            return best_a[0], "", best_a[1]
        return "", best_al[0], best_al[1]

    def belongs_to_majority(rec: Dict[str, Any], maj_val: str) -> bool:
        if not maj_val:
            return False
        mv = norm_key(maj_val)
        if len(mv) < 2:
            return False
        for val in (rec.get("artist"), rec.get("album")):
            v = norm_key(val)
            if v and (v == mv or mv in v or v in mv):
                return True
        return False

    # 复用模块级实现（接口层也要用同一套判定）
    folder_matches_name = globals()["folder_matches_name"]

    def has_home_folder(rec: Dict[str, Any]) -> bool:
        """库里有没有"名字对得上这首歌"的目录（作者目录或专辑目录）。"""
        for folder in folders:
            if folder == rec["_folder"] or folder in sink_set:
                continue
            for val in (rec.get("artist"), rec.get("album")):
                if val and folder_matches_name(folder, val):
                    return True
        return False

    def filename_supports(name: str, folder: str) -> bool:
        """文件名本身是否支持"这首歌属于 `folder`"。

        这是最可靠的一条证据：文件名是用户自己起的，标签却常常是抓取错的。
        用途是**否决一次搬迁** —— 打算把歌搬到某个目录前，先看那个目录的
        主流标签值在不在文件名里；不在就说明搬过去有问题。

        例：`Minecraft - Volume Alpha/Beginning - C418.flac` 的 album 标签写成了
        Volume Beta，于是算法想把歌搬到 `Minecraft - Volume Beta/`。但文件名是
        `Beginning`（Volume Alpha 的第一首），而 Volume Beta 目录里的曲目并没有
        它 —— 这条证据把这次搬迁否掉，歌留在原地。
        """
        n = norm_key(name)
        if not n:
            return False
        d = folders.get(folder)
        if not d:
            return False
        for val in (d.get("artists"), d.get("albums")):
            if not val:
                continue
            top = max(val.items(), key=lambda x: x[1])
            if top[1] < 2:
                continue
            k = norm_key(top[0])
            if k and k in n:
                return True
        return False

    def is_credit_list(artist: str) -> bool:
        """artist 字段是不是"一串合作者"（`A/B/C` 或 `A, B, C`）。

        一串合作者里的任何一个都不足以决定该放哪个目录 —— 尤其对 remix。
        """
        a = str(artist or "")
        return ("/" in a) or (a.count(",") >= 1 and len(a) > 24)

    # 记录每首歌的判定依据，界面里可以直接解释"为什么这么判"
    reasons: Dict[str, str] = {}

    misplaced: List[Dict[str, Any]] = []
    no_home: List[Dict[str, Any]] = []
    for folder, d in folders.items():
        maj_val, _maj_album, maj_n = ("", "", 0)
        if folder not in sink_set:
            a, al, n = folder_majority(folder)
            maj_val, maj_n = (a or al), n
        for r in d["items"]:
            path = r.get("path")
            if folder in sink_set or not in_scope(folder):
                keep.append(r)
                continue
            if not str(r.get("artist") or "").strip() and not str(r.get("album") or "").strip():
                keep.append(r)          # 没标签，无从判断对错
                continue
            if folder_fits(folder, r):
                keep.append(r)          # 目录名对得上 → 位置正确
                continue

            name = r.get("name") or ""
            title = r.get("title") or ""
            folder_leaf = folder.rsplit("/", 1)[-1] if folder else ""

            # ① 派生作品（remix / 翻唱 / 改编）不能拿 artist 标签判断：
            #    这些歌的标签写的是原作者，真正归属看文件名。
            if is_derivative(name, title):
                fa = filename_artist(name)
                if fa and folder_matches_name(folder, fa):
                    keep.append(r)
                    reasons[path] = f"派生作品，文件名署名「{fa}」与所在目录一致"
                    continue
                if is_credit_list(r.get("artist")):
                    keep.append(r)
                    reasons[path] = "派生作品（remix/翻唱），artist 是原作者名单，不宜据此搬动"
                    continue

            rec = dict(r)
            rec["_folder"] = folder
            rec["_name"] = name
            # ② 目录有明确的"主流"而这首不属于它 → 有证据说它走错门了
            if maj_n >= 2 and not belongs_to_majority(r, maj_val):
                # artist 是串合作者时，不足以判定它错
                if is_credit_list(r.get("artist")):
                    keep.append(r)
                    reasons[path] = "artist 是多个合作者，无法据此判断归属"
                    continue
                misplaced.append(rec)
                continue
            # ③ 目录里只有一两首、看不出主流，但库里确实有它该去的目录
            if has_home_folder(rec):
                misplaced.append(rec)
                continue
            # 用户明确选了"sink"/"new"策略时，这类"谁都不知道"的歌也交给他处理
            if orphan_policy != "keep":
                misplaced.append(rec)
                continue
            no_home.append(rec)
            keep.append(r)

    # ---- 2) 给每首"放错位置"的歌找去处 ----
    # 候选顺序：**名字跟这首歌对得上**的现成目录 → 收纳区 → 新建。
    # 关键点：不能随便挑一个"里面有同作者的歌"的目录就算数。否则一堆散落在
    # `各种1`、`各种2` 里的同一张专辑，会互相往对方那儿搬（实测出现过
    # "各种0 → 各种1、各种1 → 各种0"这种原地打转的方案）。
    def find_home(rec: Dict[str, Any]) -> Optional[str]:
        artist = str(rec.get("artist") or "").strip()
        album = str(rec.get("album") or "").strip()
        best: Optional[str] = None
        best_score = 0
        for folder, d in folders.items():
            if folder == rec["_folder"] or folder in sink_set:
                continue
            if not folder_fits(folder, rec):
                continue
            score = 0
            if album and d["albums"].get(album):
                score += 2                     # 同名专辑最贴切
            if artist and d["artists"].get(artist):
                score += 1
            if score > best_score:
                best_score, best = score, folder
            elif score == best_score and best and len(folder) < len(best):
                best = folder                  # 同分取更靠上的目录
        return best

    def accept_move(rec: Dict[str, Any], dst: str, why: str,
                    extra: str = "", album_move: bool = False) -> None:
        """最终裁决。

        只有一种情况需要否决：**按专辑搬**，而文件名看起来属于另一个专辑。
        例：`Minecraft - Volume Alpha/Beginning - C418.flac` 的 album 标签错写成
        Volume Beta，于是想搬到 Volume Beta 目录；但同一张 Alpha 系列里还有
        `Beginning 2`（那才是 Beta 的曲目名），说明这首歌本来就该在 Alpha 里。
        算法没法替用户分辨标签和文件名哪个对，所以这种"两套信息打架"的情况
        一律不动，交给人判断。

        按作者搬**不做这个否决**：中文歌的文件名基本不含作者名，拿文件名去
        要求"必须出现作者名"会把正常搬迁全部拦掉（实测把这批全挡了）。
        目标目录名已经能对上作者，这就是足够的依据。
        """
        if album_move:
            cur = rec.get("_folder") or ""
            # 已经在"同作者聚集"的目录里 → 不再按专辑标签把它挑出来。
            # 例：`Minecraft - Volume Alpha/` 里有 23 首 C418 的歌，其中
            # `Beginning - C418.flac` 的 album 标签错写成 Volume Beta。
            # 它已经跟对的作者在一起了，专辑级重排属于过度干预 —— 用户要的
            # 是"把放错位置的歌放回去"，不是按标签重排专辑。
            if has_artist_cohort(cur, rec):
                keep.append(rec)
                reasons[rec.get("path")] = (
                    f"已在「{cur}」——该目录里就有同作者的歌，位置是对的；只是专辑标签跟目录名不一致")
                return
            if not album_tag_agrees(rec, dst):
                keep.append(rec)
                reasons[rec.get("path")] = (
                    f"{extra or '疑似走错门'}，但文件名与专辑标签打架（标签说「{rec.get('album')}」），先不动")
                return
        # ★ 一旦真的决定搬，就必须从 keep 里移除（同一路径只能有一个状态）。
        _p = rec.get("path")
        if _p:
            keep[:] = [k for k in keep if k.get("path") != _p]
        moves.append(_move(rec, dst, why))

    def has_artist_cohort(folder: str, rec: Dict[str, Any]) -> bool:
        return has_same_artist_cohort(folder, rec, folders)

    def album_tag_agrees(rec: Dict[str, Any], dst: str) -> bool:
        """按专辑搬动时，文件名是否不反对这个专辑。

        目标目录的标签值出现在文件名里 → 完全同意。
        目标目录的名字都没在文件名里出现，但还是想搬 → 只在"歌的专辑标签
        确实等于目标目录名"时放行（标签和目录名自洽）。
        其余情况视为信息打架，不动。
        """
        name = norm_key(rec.get("_name") or rec.get("name") or "")
        dst_leaf = norm_key(dst.rsplit("/", 1)[-1] if dst else "")
        alb = norm_key(rec.get("album") or "")
        if not name or not dst_leaf:
            return False
        if dst_leaf in name:
            return True
        # 标签本身跟目标目录自洽，且文件名不包含**另一个**专辑系列名
        return bool(alb) and alb == dst_leaf

    for rec in misplaced:
        home = find_home(rec)
        if home:
            # 决定"这次搬迁属于什么性质"：目标目录名对上的是**专辑**还是**作者**？
            # 对上专辑 → 属于专辑级重排，要过更严的闸门；
            # 对上作者（作者目录，或者比作者更粗的作者/专辑层级）→ 按作者归位，放行。
            dst_leaf = norm_key(home.rsplit("/", 1)[-1])
            alb = norm_key(rec.get("album") or "")
            album_move = bool(alb) and (
                alb == dst_leaf or alb in dst_leaf or dst_leaf in alb)
            accept_move(rec, home,
                        f"这个目录里没有同作者/同专辑的歌，应归到「{home}」",
                        extra="目录名对不上", album_move=album_move)
        else:
            want[f"__new__{_group_key(rec)}"].append(rec)

    # ---- 3) 兜底：找不到现成去处的怎么办 ----
    # 默认 `orphan_policy="keep"`：**原地不动**。
    # 这是刻意的保守选择：为它新建一个目录属于"制造结构"，用户只想要
    # 放错的歌回到正确位置，而不想工具发明新文件夹。实测中这一条避免了给
    # 43 首"谁都不是"的歌新建目录（比如把 7 首 `节奏医生/X-0` 的歌
    # 单独抽出来建个 `X/`，纯属折腾）。
    # 想全部收拢的话把 orphan_policy 设成 "sink" 或 "new"。
    for key, items in want.items():
        probe = items[0]
        gname = _group_dirname(probe, key.replace("__new__", ""))
        enough = len(items) >= max(2, min_group)
        if orphan_policy == "new" and enough:
            for rec in items:
                moves.append(_move(rec, gname,
                                   f"同组 {len(items)} 首，建议新建「{gname}」"))
        elif orphan_policy == "sink":
            for rec in items:
                accept_move(rec, sink_dest,
                            f"孤立单曲（同组仅 {len(items)} 首），收进「{sink_dest}」",
                            extra="没有明确去处")
                unplaceable.append(rec)
        elif orphan_policy == "new":
            for rec in items:
                accept_move(rec, sink_dest,
                            f"同组仅 {len(items)} 首（少于 {min_group}），收进「{sink_dest}」",
                            extra="同组太小")
                unplaceable.append(rec)
        else:
            keep.extend(items)

    # ---- 4) 归拢：同一组散落在多处 → 合并 ----
    #
    # 这里刻意比"数满 N 首就合"严格得多。朴素的 ≥2 规则在真实库上会乱来：
    #   · 有人只在同一层建了 `Alan Walker/`，另一个人的库里是
    #     `Alan Walker/专辑A/` —— 层级不同但内容没差，硬合会制造出
    #     `Alan Walker/专辑A/专辑B/` 这种嵌套垃圾；
    #   · 某作者有 30 首歌散在 12 个目录里，但那些是不同专辑 —— 那不是
    #     "该按专辑分类"，合并反而会毁掉信息；
    #   · 收纳区（精选集）与正式目录之间不能互相搬。
    #
    # 所以合并必须同时满足：
    #   ① 曲目数 ≥ 门槛（默认取 min_group）；
    #   ② 来源目录 ≥ 2 个，且**目标目录装的那部分不能过半**
    #      —— 否则它其实已经基本就位了，搬剩下的只会造成无谓改动；
    #   ③ 每个要搬的歌，文件名都支持目标目录（走 accept_move 的闸门）；
    #   ④ 目标不是收纳区，来源也不是收纳区（收纳区是有意混放的）。
    gathered = 0
    if include_gather:
        by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        # 只排除**已经要移动**的；被判 keep 的歌仍然可以被归拢段接管 ——
        # "合并到专辑目录"本身就是纠正 keep 的理由（它只是暂时待在那儿）。
        # 守恒由 `accept_move` 保证：真搬走时会把这条从 keep 里剔除，
        # 所以最终 keep + move == total，不会同一首两个状态。
        placed = {m["path"] for m in moves}
        for folder, d in folders.items():
            if folder in sink_set or not in_scope(folder):
                continue
            # 收纳区里的歌也统计进来：它们是"该被合并"的主要来源
            for r in d["items"]:
                if r.get("path") in placed:
                    continue
                key = _group_key(r)
                if key:
                    by_group[key].append((folder, r))
        # ★ 收纳区**不能**当归拢来源：它是有意混放的（精选集、合集）。
        #   原来这里会把收纳区里的歌抽出来搬进正式目录，直接违反
        #   "收纳区里的歌一律不动" 这条规则（文档和界面文案都写着）。

        need_gather = max(2, gather_min or min_group)
        for key, entries in by_group.items():
            if len(entries) < need_gather:
                continue
            fset = {f for f, _r in entries}
            if len(fset) < 2:
                continue                        # 本来就集中在一个目录
            target = _pick_gather_target(entries, folders, sink_set)
            if not target:
                continue
            in_target = sum(1 for f, _r in entries if f == target)
            # ② 目标已经装了大半 → 剩下的零头不值得搬
            if in_target * 2 >= len(entries):
                continue
            for folder, r in entries:
                if folder == target:
                    continue
                rec = dict(r)
                rec["_folder"] = folder
                rec["_name"] = r.get("name") or P.basename(r.get("path") or "")
                accept_move(rec, target,
                            f"同组 {len(entries)} 首散在 {len(fset)} 个目录，合并到「{target}」",
                            extra="同组分散")
                if rec.get("path") not in {k for k in reasons}:
                    gathered += 1

    # ---- 5) 现状摘要 ----
    summary = {
        "total": len(rows),
        "keep": len(keep),
        "move": len(moves),
        "misplaced": len(misplaced),
        "no_home": len(no_home),
        "unplaceable": len(unplaceable),
        "gathered": gathered,
        "folders_now": len([f for f in folders if f]),
        "sinks": sorted(sink_set),
        "singles_target": sink_dest,
        "min_group": int(min_group),
        "scopes": sorted(scope_set),
        "move_bytes": sum(int(m.get("size") or 0) for m in moves),
        "new_dirs": sorted({m["dst_dir"] for m in moves
                            if m["dst_dir"] not in folders}),
    }
    moves.sort(key=lambda m: (m["dst_dir"], m["name"]))
    return {
        "summary": summary,
        "moves": moves,
        "reasons": reasons,
        "keep": [{"path": r.get("path"), "name": r.get("name"),
                  "folder": _folder_of(_rel(root_n, r.get("path") or ""))}
                 for r in keep],
        "folder_stats": sorted(
            ({"folder": f, "leaf": d["leaf"], "count": d["count"],
              "artists": len(d["artists"]), "albums": len(d["albums"]),
              "sink": f in sink_set}
             for f, d in folders.items() if f),
            key=lambda x: (-x["count"], x["folder"])),
    }


# ------------------------------------------------------------------ 辅助

def _group_key(rec: Dict[str, Any]) -> str:
    """分组键：优先 作者+专辑，其次只用作者。"""
    artist = _safe_seg(rec.get("artist"))
    album = _safe_seg(rec.get("album"))
    if artist and album:
        return f"{artist}\x1f{album}"
    if artist:
        return f"{artist}\x1f"
    if album:
        return f"\x1f{album}"
    return ""


def _group_dirname(rec: Dict[str, Any], key: str) -> str:
    artist = _safe_seg(rec.get("artist"), "未知艺术家")
    album = _safe_seg(rec.get("album"))
    if album:
        return f"{artist}/{album}"
    return artist


def _pick_gather_target(entries: List[Tuple[str, Dict[str, Any]]],
                        folders: Dict[str, Dict[str, Any]],
                        sink_set: set) -> Optional[str]:
    """合并时选哪个目录当目标：取"层级最浅、同组歌最多"的那个。"""
    counts: Dict[str, int] = defaultdict(int)
    for folder, _r in entries:
        counts[folder] += 1
    non_sink = [(c, f) for f, c in counts.items() if f not in sink_set]
    pool = non_sink or list(counts.items())
    if not pool:
        return None
    # 同组歌数多的优先；同数取更靠上的目录（层级浅的往往离作者层更近）
    pool.sort(key=lambda x: (-x[0], len(x[1])))
    return pool[0][1]


def _move(rec: Dict[str, Any], dst_dir: str, reason: str) -> Dict[str, Any]:
    name = rec.get("_name") or P.basename(rec.get("path") or "")
    return {
        "path": rec.get("path"),
        "name": name,
        "did": f"{rec.get('source_id')}::{rec.get('path')}",
        "artist": rec.get("artist") or "",
        "album": rec.get("album") or "",
        "title": rec.get("title") or "",
        "size": int(rec.get("size") or 0),
        "from_dir": rec.get("_folder") or "",
        "dst_dir": dst_dir,
        "dst_path": P.join(dst_dir, name) if dst_dir else name,
        "reason": reason,
        "is_new_dir": False,          # 由调用方按 summary.new_dirs 判断
    }


# ------------------------------------------------------------------ 兼容旧接口

def build_plan(rows: Sequence[Dict[str, Any]], root: str, mode: str = "artist_album",
               template: str = "{artist}/{album}", min_group: int = DEFAULT_MIN_GROUP,
               singles_root: bool = True) -> Dict[str, Any]:
    """旧签名（按模板重排）保留给老的调用方/测试，内部转成新逻辑。

    新逻辑默认假设"现有结构是对的"，所以这里只做 relocate：
    把放错位置的歌挪回去，而不是把整个库按模板重排。
    """
    return plan_relocate(rows, root, min_group=min_group,
                         singles_dir="单曲" if singles_root else "",
                         include_gather=True,
                         orphan_policy="sink" if singles_root else "keep")


def analyze(tracks: Sequence[Dict[str, Any]], root: str,
            template: str = "{artist}/{album}", min_group: int = DEFAULT_MIN_GROUP,
            mode: str = "artist_album", single_artist: bool = False) -> Dict[str, Any]:
    plan = build_plan(tracks, root, mode=mode, template=template, min_group=min_group,
                      singles_root=not single_artist)
    plan["loose"] = []
    return plan


# "第几卷 / 上篇下篇"这类标记。名字相似但标记不同的目录**不是**重复目录，
# 而是同一系列的不同卷 —— 合并它们会把两张专辑搅在一起。
_VOLUME_RE = re.compile(
    r"(?:vol\.?|volume|disc|disk|cd|part|pt\.?|chapter|book)\s*([0-9]+|[ivx]+|one|two|three)"
    r"|([0-9]+)\s*(?:st|nd|rd|th)\s*(?:edition|anniv)"
    r"|(上|下|中)\s*篇"
    r"|第\s*([0-9一二三四五六七八九十]+)\s*(?:卷|辑|部|章|篇|碟|盘)",
    re.I)
# 名字末尾孤立的数字/罗马数字也常常是卷号：`星空剧场2`、`A Story 03`
_TRAILING_NUM_RE = re.compile(r"[\s\-_]*([0-9]{1,2}|[ivx]{1,4})\s*$", re.I)


def volume_signature(name: str) -> str:
    """提取"第几卷"的标记，用来区分同系列的不同卷。

    `星空剧场2 Astral Theater Vol.2` → `2`；`上篇 ... Disc1` → `1`；
    `The Wild Update` → ``（没有卷号）。两个目录名字很像但签名不同，
    说明它们是同一系列的不同卷，不该建议合并。
    """
    s = str(name or "")
    sigs: List[str] = []

    def _num(raw: str) -> str:
        """统一成规范形式：去前导零、罗马数字/英文数字/中文数字转阿拉伯数字。

        罗马数字和 `上/中/下` 都归一到阿拉伯数字，这样 `Disc1`、`上篇`、
        `Vol.1` 得到同一个签名，不会因为写法不同被当成不同的卷。
        """
        t = (raw or "").strip().lower()
        words = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
                 "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
                 "一": "1", "二": "2",
                 "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
                 "八": "8", "九": "9", "十": "10",
                 "上": "1", "中": "2", "下": "3"}
        if t in words:
            return words[t]
        if t.isdigit():
            return str(int(t))          # 01 → 1
        return t

    for m in _VOLUME_RE.finditer(s):
        for g in m.groups():
            if g:
                sigs.append(_num(g))
                break
    # `上/中/下 篇` 单独认，归一成 1/2/3（上面的正则会把它当"上"捕获）
    for zh, num in (("上", "1"), ("中", "2"), ("下", "3")):
        if re.search(rf"{zh}\s*篇", s):
            sigs.append(num)
    if not sigs:
        m = _TRAILING_NUM_RE.search(s.strip())
        if m:
            sigs.append(_num(m.group(1)))
    return ",".join(sorted(set(sigs)))


# 这些词在专辑/作者名里是噪声：它们不区分两个目录的身份，只会把相似度撑高。
# `Minecraft - Nether Update` vs `Minecraft - The Wild Update` 相似度 92%，
# 但把通用词去掉后剩下 `minecraft nether` vs `minecraft wild`，一眼就是两回事。
_GENERIC_WORDS = (
    "original game soundtrack", "original soundtrack", "original sound track",
    "official soundtrack", "game soundtrack", "soundtrack", "ost", "album",
    "update", "edition", "deluxe", "remaster", "remastered", "collection",
    "complete", "the", "a", "an", "vol", "volume",
    "原声", "原声带", "专辑", "合集", "精选", "版", "集",
)


def distinctive_name(name: str) -> str:
    """去掉通用词之后的目录名 —— 用来判断两个目录是不是"真的在说同一件事"。"""
    s = P.normalize_text(str(name or ""))
    for w in _GENERIC_WORDS:
        s = re.sub(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _shares_run(a: str, b: str, n: int = 3) -> bool:
    """两个字符串有没有长度 ≥ n 的共同子串。

    用来挡掉"短字符串凑巧相似"的误判：`范特西` 和 `要整理` 一个共同字都没有，
    但 difflib 给的相似度有 0.9（长度相同、字符全不同时它也会给高分）。
    """
    if not a or not b:
        return False
    if len(a) < n or len(b) < n:
        return a == b
    grams = {a[i:i + n] for i in range(len(a) - n + 1)}
    return any(b[i:i + n] in grams for i in range(len(b) - n + 1))


def self_new_reason(group_by: str, canonical: str, n: int, min_group: int) -> str:
    """新建目录时给用户的说明（按分组依据换措辞）。"""
    what = {"artist": "作者目录", "album": "专辑目录",
            "artist_album": "作者/专辑目录"}.get(group_by, "目录")
    return f"该组 {n} 个曲目达到门槛 {min_group}，建议新建{what}「{canonical}」"


def _tokens(s: str) -> List[str]:
    return [t for t in re.split(r'[^0-9a-z\u4e00-\u9fff]+',
                                str(s or '').lower()) if t]


# 这些词在专辑/作者名里是噪声，不能算"区分性词"
_NOISE = {
    'the', 'a', 'an', 'of', 'and', 'or', 'to', 'in', 'on', 'at', 'for',
    'vol', 'volume', 'part', 'pt', 'disc', 'disk', 'cd', 'ep', 'lp',
    'original', 'soundtrack', 'ost', 'edition', 'deluxe', 'remaster',
    'remastered', 'version', 'ver', 'mix', 'remix', 'edit', 'feat',
    'ost', 'complete', 'full', 'album', 'single',
}


def _distinctive(name: str) -> set:
    """名字里"能区分身份"的词。

    用于挡住"名字很像但其实是两回事"的合并 —— 最典型的是
    `Minecraft - Volume Alpha` vs `Minecraft - Volume Beta`：
    整体相似度 0.889 很高，但 `alpha` / `beta` 是实打实的区分词。

    **单字符词一律当噪声丢掉**（`HOPE-T` 里的 `t`、`Vol.1` 里的 `1`）：
    它们是版本/后缀标记，当成"区分词"会把 `Hope` → `HOPE-T` 这种
    正常容错也挡掉。真正有意义的区分词都是 ≥2 字符的。
    """
    return {t for t in _tokens(name) if len(t) > 1 and t not in _NOISE}


def _conflicting_identity(a: str, b: str) -> bool:
    """两个名字是否不可能指同一个东西（**对称判据**）。

    **各有对方没有的区分词** → 不是同一个。
    挡住 `Minecraft - Volume Alpha` vs `Minecraft - Volume Beta`
    （`alpha` / `beta` 各自只在一边）。

    标点与全角半角差异在 `normalize_text` 里已经抹平，不会误伤
    `Oneshot_ Solstice` vs `Oneshot - Solstice` 这种。
    """
    da, db = _distinctive(a), _distinctive(b)
    if not da or not db:
        return False
    return bool(da - db) and bool(db - da)


def _too_specific_for(want: str, candidate: str) -> bool:
    """`candidate` 是否比 `want` 更"具体"，因而不该被 `want` 认领。

    挡住 `City` → `Owl city` 这类：专辑名 `City` 太泛，
    而目录名 `Owl city` 多出一个自己的招牌词 `owl` —— 那多半是"猫头鹰之城"
    乐队的目录，不是一张叫《City》的专辑。

    反过来 `Hope` → `HOPE-T` 是**允许**的：单字符词 `t` 在
    `_distinctive` 里已被当噪声丢掉，两边的区分词集合都是 `{hope}`，
    所以判为可以匹配（属于拼写/后缀差异，合理容错）。

    调用方向很重要：`_too_specific_for(要匹配的名字, 候选目录名)`。
    """
    dw, dc = _distinctive(want), _distinctive(candidate)
    if not dw or not dc:
        return False
    return bool(dc - dw)


def _same_folder_name(folder: str, want: str) -> bool:
    """目录（的**任意一层**）名和专辑/作者名是否指同一个东西。

    直接复用核心判断 `_is_same_identity`（对称、词级），不再各写一套。
    看**每一层**而不是只看叶子，是因为库里常见 `Rhythm Doctor OST/FLAC`
    这种"专辑目录下面再套一个格式子目录"的结构 —— 只看叶子会把 `FLAC`
    拿去和专辑名比，永远匹配不上。
    """
    if not want or not folder:
        return False
    return any(_is_same_identity(seg, want)
               for seg in P.norm(str(folder)).split("/") if seg)


def _merge_same_destination(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把**目标目录相同**的组并成一个。

    `artist_album` 的分组键是"作者+专辑"，但一首曲目的 `artist` 标签经常
    不是唯一作者：`Riya`、`fizzd; Riya`、`celesti whispers` 都在同一张
    Rhythm Doctor OST 里，`Minecraft_ Lena Raine`/`Minecraft_ Aaron Cherof`
    都在同一张 Tricky Trials 里。于是同一张专辑被拆成好几个组。

    这在界面上非常怪：同一个目标目录出现十几行，每行一两首，看着像算法疯了。
    实测 79 个目标目录被多个组指向（最多的一个被 107 个组指向）。

    归并之后：
      · 一个目标目录只有一行，曲目数是各组合计；
      · `already` 也合计（都在同一个目录里，"已就位"本来就是共享的）；
      · 依据保留**第一条**（去重后最多列 3 条），并说明是按几个分组键合并来的。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for a in actions:
        dst = a.get("dst") or ""
        if dst not in groups:
            groups[dst] = []
            order.append(dst)
        groups[dst].append(a)

    merged: List[Dict[str, Any]] = []
    for dst in order:
        g = groups[dst]
        if len(g) == 1:
            merged.append(g[0])
            continue
        first = dict(g[0])
        first["count"] = sum(int(x.get("count") or 0) for x in g)
        first["already"] = sum(int(x.get("already") or 0) for x in g)
        first["total"] = sum(int(x.get("total") or 0) for x in g)
        first["bytes"] = sum(int(x.get("bytes") or 0) for x in g)
        folders: List[str] = []
        for x in g:
            for f in x.get("from_folders") or []:
                if f not in folders:
                    folders.append(f)
        first["from_folders"] = sorted(folders)
        first["items"] = [i for x in g for i in (x.get("items") or [])]
        # 分组键也要留着，便于"改目录映射"时能一次作用到所有相关组
        keys: List[str] = []
        for x in g:
            k = x.get("key")
            if k and k not in keys:
                keys.append(k)
        first["keys"] = keys
        reasons: List[str] = []
        for x in g:
            w = (x.get("why") or "").strip()
            if w and w not in reasons:
                reasons.append(w)
        first["reasons"] = reasons[:3]
        # 专辑/作者取"信息最全"的那条，避免合并后标题变成空
        for field in ("album", "artist"):
            if not first.get(field):
                first[field] = next((x.get(field) for x in g if x.get(field)), "")
        if len(keys) > 1:
            # 合并后的组常常是"同一作者的不同专辑各自零散"，此时单说
            # "专辑：1 首，不再分层"会很怪（合并后明明有 44 首）。
            # 主轴改成"按 N 个分组归并"，原始依据留在 reasons 里。
            first["why"] = (f"按 {len(keys)} 个分组归并到同一目录"
                            + (f"（{reasons[0]}）" if reasons else ""))
            first["merged_from"] = len(keys)
        merged.append(first)
    return merged


def plan_by_album(rows: Sequence[Dict[str, Any]], root: str,
                  scopes: Optional[Sequence[str]] = None,
                  min_group: int = DEFAULT_MIN_GROUP,
                  sinks: Optional[Sequence[str]] = None,
                  custom: Optional[Dict[str, str]] = None,
                  include_artist_fallback: bool = True,
                  small_policy: str = "keep",
                  group_by: str = "artist_album",
                  keep_flat_below: bool = True,
                  single_naming: str = "专辑目录",
                  folder_pool_rows: Optional[Sequence[Dict[str, Any]]] = None,
                  ) -> Dict[str, Any]:
    """按"分组依据"整理库（智能整理的主流程）。

    元数据是唯一依据 —— 这一点必须贯彻到底：
      · `artist`        同一 `artist` 标签归一组，目标是**作者目录**
      · `album`         同一 `album` 标签归一组，目标是**专辑目录**，
                        **绝不**因为作者标签相同就把歌塞进作者目录
      · `artist_album`  同一作者同一专辑归一组，目标是 `作者/专辑`

    目录匹配一律走"严格同名 / 高相似且无冲突区分词"，不做子串包含匹配。

    `folder_pool_rows`：用来建"库内有哪些目录"这份画像的行。
    默认就是 `rows`；当只想处理**一部分文件**（候选区、入库那类场景）时，
    传全量行进来，这样仍能复用库里已有的目录，而不是只能新建。
    """
    return _plan_by_album_impl(
        rows, root, scopes=scopes, min_group=min_group, sinks=sinks,
        custom=custom, include_artist_fallback=include_artist_fallback,
        small_policy=small_policy, group_by=group_by,
        keep_flat_below=keep_flat_below, single_naming=single_naming,
        folder_pool_rows=folder_pool_rows)


def _plan_by_album_impl(
        rows: Sequence[Dict[str, Any]], root: str,
        scopes: Optional[Sequence[str]] = None,
        min_group: int = DEFAULT_MIN_GROUP,
        sinks: Optional[Sequence[str]] = None,
        custom: Optional[Dict[str, str]] = None,
        include_artist_fallback: bool = True,
        small_policy: str = "keep",
        group_by: str = "artist_album",
        keep_flat_below: bool = True,
        single_naming: str = "专辑目录",
        folder_pool_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """实际的整理实现（对外入口是 `plan_by_album`）。

    `group_by` 决定**按什么分组**，也就决定了目标目录的形态：
      `artist_album`  同一作者同一专辑 → `作者/专辑`
      `artist`        同一作者        → `作者`（该作者所有专辑放一起）
      `album`         同一专辑        → `专辑`

    `keep_flat_below`：曲目数未达 `min_group` 的分组，是否保留在现有位置
    （默认是）。关掉就会收进收纳区。

    `single_naming`：某组没有专辑信息时落到的目录名（默认"专辑目录"，
    也可以设成"单曲"之类）。
    """
    root_n = P.norm(root)
    # 目录画像用全量行（如果调用方给了），但**分组仍用 rows** ——
    # 这样"只整理库中的几个目录"（候选区、入库）时，
    # 依然能认出库里已有的目录去复用，而不是只能新建。
    pool_rows = list(folder_pool_rows) if folder_pool_rows else list(rows)
    folders = build_folders(pool_rows, root_n)
    sink_set = set(find_sinks(pool_rows, root_n, sinks))
    custom = {str(k).strip(): P.norm(str(v)).strip("/")
              for k, v in (custom or {}).items() if str(k).strip() and str(v).strip()}

    def _as_rel(p: str) -> str:
        s = P.norm(str(p)).rstrip("/")
        if not s or s == root_n:
            return ""
        if s.startswith(root_n + "/"):
            return s[len(root_n) + 1:]
        return s

    # 1) 范围外**选中的直接忽略**
    ignore_set = {_as_rel(s) for s in (scopes or [])}
    ignore_set.discard("")

    def ignored(folder: str) -> bool:
        f = P.norm(folder).rstrip("/")
        for s in ignore_set:
            if f == s or f.startswith(s + "/"):
                return True
        return False

    # 库里所有"可当目标"的目录（排除收纳区与忽略范围）
    target_pool = [f for f in folders if f and f not in sink_set and not ignored(f)]
    # 候选目录的标签画像，用于相似度匹配
    pool_info = []
    for f in target_pool:
        d = folders[f]
        leaf = f.rsplit("/", 1)[-1]
        top_album = max(d["albums"].items(), key=lambda x: x[1])[0] if d["albums"] else ""
        top_artist = max(d["artists"].items(), key=lambda x: x[1])[0] if d["artists"] else ""
        # 父目录的"主流作者" —— 用来判断这个候选目录是不是挂在**别的**
        # 作者目录下（`陈奕迅/专辑0` 的父级作者就是陈奕迅）。
        # 光看父目录**名字**是不够的：得知道那层装的是谁。
        parent = f.rsplit("/", 1)[0] if "/" in f else ""
        parent_artist = ""
        if parent and parent in folders:
            pa = folders[parent]["artists"]
            parent_artist = max(pa.items(), key=lambda x: x[1])[0] if pa else ""
        pool_info.append({"folder": f, "leaf": leaf, "album": top_album,
                          "artist": top_artist, "count": d["count"],
                          "parent": parent, "parent_artist": parent_artist})

    # 2) 按"分组依据"把曲目分组
    #
    # `group_by` 是用户直接选的，必须真正决定分组方式 —— 之前这个选择根本
    # 没暴露出来，代码里写死"专辑优先、没专辑才回落作者"，用户看到的是
    # "想按作者分却被按专辑拆开"，而界面上没有任何地方能改：
    #   artist_album  同一作者同一专辑  → 目标 `作者/专辑`
    #   artist        同一作者         → 目标 `作者`
    #   album         同一专辑         → 目标 `专辑`
    if group_by not in ("artist_album", "artist", "album"):
        raise ValueError(f"不支持的分组依据: {group_by}")

    groups: Dict[str, List[Dict[str, Any]]] = {}
    # ★ 没有任何元数据的曲目：单独收起来，**不参与智能整理**，交给手工处理。
    #
    #   以前这里是 `if not key: continue` —— 这些曲目被**静默丢弃**：
    #   既不在 `groups` 里（不会被移动），也**没进任何结果列表**
    #   （不在 keep、不在 small、不在 no_home），
    #   于是 `summary.total` 和"保留/移动"对不上，用户看不到它们去哪了，
    #   更不知道该自己动手。
    #
    #   判定用「`artist` 和 `album` **两个标签都为空**」，而不是"分组键为空"：
    #   `title` 是**不可靠**的 —— 扫描器读不到标签时会用文件名兜底填上
    #   （实测 `Track 01.mp3` 的 title 就是 `'Track 01'`）。
    #   拿 title 判断的话，无标签文件会被当成"有元数据"，
    #   然后在下游按那个假歌名建目录、把散装文件全扫进去。
    manual: List[Dict[str, Any]] = []
    for r in rows:
        rec = dict(r)
        rec["_rel"] = _rel(root_n, rec.get("path") or "")
        rec["_folder"] = _folder_of(rec["_rel"])
        rec["_name"] = rec.get("name") or P.basename(rec.get("path") or "")
        rec["_artist"] = _safe_seg(rec.get("artist"))
        rec["_album"] = _safe_seg(rec.get("album"))
        # 两个标签都空 → 没有可信的归属依据，只能人工决定
        if not rec["_artist"] and not rec["_album"]:
            manual.append(rec)
            continue
        if group_by == "artist":
            key = rec["_artist"]
        elif group_by == "album":
            key = rec["_album"] or (rec["_artist"] if include_artist_fallback else "")
        else:
            # 作者/专辑：有作者有专辑才用组合键，否则退化成"有的那个"
            if rec["_artist"] and rec["_album"]:
                key = f"{rec['_artist']}\x1f{rec['_album']}"
            else:
                key = rec["_artist"] or rec["_album"]
        if not key:
            # 理论上到不了这里（上面已按标签判过），留作兜底：
            # 一旦有别的 key 计算方式产出空串，也别静默丢
            manual.append(rec)
            continue
        groups.setdefault(key, []).append(rec)

    actions: List[Dict[str, Any]] = []
    moves: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    no_home: List[Dict[str, Any]] = []
    small: List[Dict[str, Any]] = []      # 同专辑不足门槛、库内也没对应目录的

    # 收纳区兜底：优先用户点名的，其次层级最浅/曲目最多的那个
    sink_dest = ""
    named = _as_rel(custom.get("__sink__", "")) if custom.get("__sink__") else ""
    if named:
        sink_dest = named
    elif sink_set:
        sink_dest = sorted(sink_set, key=lambda f: (len(f.split("/")),
                                                    -folders[f]["count"]))[0]
    if not sink_dest:
        sink_dest = "单曲"

    def similar_folder(key: str) -> Optional[Dict[str, Any]]:
        """库里有没有"名字最像这个专辑"的现成目录。

        四个必要的门槛，每一个都是被真实误报逼出来的：

          · **只比目录名，绝不动 p["album"]**。这是最坑的一条：`p["album"]`
            是"这个目录里最常见的专辑标签"，拿它去比等于**自问自答** ——
            作者目录 `Ahxello` 里装着 `Forever Away` 这首歌，它的
            `top_album` 就是 `Forever Away`，于是相似度 1.000，
            "库内找到最相似的目录 Ahxello"。实测 9 组全栽在这上面
            （`Dreams`→`plum`、`Memory`→`SmK`、`Memory`→`SmK` …）。

          · 名字太短就不比。`范特西` vs `要整理` 的相似度有 0.9，
            `专辑0` vs `各处0` 也有 0.75 —— 短字符串里凑巧重合的比例很高，
            于是会把毫不相干的目录当成"相似目录"，把整组歌吸过去。
            所以要求至少 4 个字符，而且必须有一段真正的共同子串。

          · 相似度要够高（≥0.90），只在明显是拼写差异时才认。

          · **不许有互相冲突的区分词** —— `Minecraft - Volume Alpha` vs
            `Minecraft - Volume Beta` 整体相似度 0.889，光看数字容易并错，
            但 `alpha`/`beta` 是实打实的身份词，一律不认。
        """
        nk = P.normalize_text(key)
        if len(nk) < 4:
            return None
        best, best_sim = None, 0.0
        for p in pool_info:
            segs = [s for s in P.norm(p["folder"]).split("/") if s]
            if not segs:
                continue
            # 最后（最靠下）一层是主名（格式子目录之类的不该主导判断）
            probe = segs[-1]
            np_ = P.normalize_text(probe)
            if len(np_) < 4:
                continue
            # 已经严格同名的走 album_exact，这里只处理"拼写有差异"的
            if np_ == nk:
                continue
            if _conflicting_identity(key, probe):
                continue
            # 专辑名比候选更"泛"时不许认领（`City` 不能并进 `Owl city`）
            if _too_specific_for(key, probe):
                continue
            # 必须有一段实打实的共同片段，而不是短串凑巧相似
            if not _shares_run(nk, np_, 3):
                continue
            sim = P.similarity(nk, np_)
            if sim > best_sim:
                best, best_sim = p, sim
        if best and best_sim >= 0.90:
            return {**best, "sim": best_sim}
        return None

    for key, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        # 范围内的才处理；范围外的原样保留
        in_scope_items = [i for i in items if not ignored(i["_folder"])]
        keep.extend([i for i in items if ignored(i["_folder"])])
        if not in_scope_items:
            continue

        src_folders = {i["_folder"] for i in in_scope_items}
        album = in_scope_items[0]["_album"]
        artist = in_scope_items[0]["_artist"]
        # 组合键形如 `作者\x1f专辑`；单项键则没有分隔符
        if "\x1f" in key:
            _a, _al = key.split("\x1f", 1)
            artist = artist or _a
            album = album or _al
        # probe = "这一组叫什么"，用于相似目录匹配与自定义映射
        probe = album or artist
        # 这一组的"规范目标目录名"由分组依据决定
        if group_by == "artist":
            canonical = artist or album
        elif group_by == "album":
            canonical = album or artist
        else:
            canonical = f"{artist}/{album}" if artist and album else (artist or album)
        enough = len(in_scope_items) >= max(2, min_group)
        # 按作者分组时不需要再分专辑层：作者目录本身就是目标
        want_sub = (group_by == "artist_album" and bool(album) and enough)

        # ① 自定义映射优先级最高
        dest = custom.get(probe) or (custom.get(artist) if artist else "") or ""
        kind, why = "custom", f"按自定义映射：{probe} → {dest}" if dest else ""

        # ② 库里有同名专辑目录 → 直接进它
        #    关键：目录**名字**得跟专辑有关才算"专辑目录"。只看里面装了同专辑的
        #    歌判断是不够的 —— 一个 2 首歌的临时目录，它的"主流专辑"就是那 2 首，
        #    于是会被当成专辑目录、反过来把别处的歌吸进去（实测把 6 首
        #    《よるつむぎ》判成目标 `X`）。
        def name_relates(folder: str, want: str) -> bool:
            if not want:
                return False
            nw = P.normalize_text(want)
            if len(nw) < 2:
                return False
            parts = [P.normalize_text(s) for s in P.norm(folder).split("/") if s]
            for seg in parts:
                if not seg:
                    continue
                if seg == nw or nw in seg or seg in nw:
                    return True
                if P.similarity(seg, nw) >= 0.88:
                    return True
            return False

        # ② 同名专辑目录
        #    **仅在"这一组以专辑为身份"时才走**（仅专辑 / 作者+专辑）。
        #
        #    `group_by` 的语义必须在这里也生效 —— 见下面的 `_album_dir_ok`。
        #    踩过：`_same_folder_name` 是**逐层**比较（那是为了
        #    `Rhythm Doctor OST/FLAC` 这种"专辑目录下再分格式"设计的），
        #    于是在"仅专辑"整理时，`周杰伦/专辑0` 也满足"某层名等于专辑名"，
        #    被 `next(...)` 挑中，全组并进 `周杰伦/专辑0/`。
        #    这既违反了文件里的原则（"绝不因为作者标签相同就把歌塞进作者目录"），
        #    又让 ③a 的同方案目标撞车合流（15 个文件静默变 5 个）。
        #
        #    所以整段是**仅在 `dest` 还空着时**才跑 —— 上面 ①自定义映射
        #    已经定了去向的话，这里必须让路（否则自定义映射被抢走）。
        if not dest and album and group_by in ("artist_album", "album"):
            # 本组涉及的所有作者 —— 判断某个候选目录是不是"别的作者的目录"
            _group_artists = {str(it.get("artist") or "") for it in items
                              if str(it.get("artist") or "")}

            def _parent_looks_like_artist(c: Dict[str, Any]) -> bool:
                """候选目录的父级看着像不像一个作者目录。

                像的话就**不能**选它：把专辑塞进 `某作者/专辑` 等于把
                "按专辑整理"偷偷变成"按作者整理"。

                ★ 判据有两条，缺一不可：
                  1. 父目录的"主流作者"（`pool_info[].artist`，就是该目录里
                     出现最多的作者标签）本身是这张专辑的演唱者之一 ——
                     说明它确实是一个作者目录（不是随手起的同级子目录）；
                  2. 那个作者**不是本组自己的作者** —— 否则那是"回自己家"，
                     是正常的（例如专辑 `MIMI/よるつむぎ` 归到 `MIMI` 下）。

                踩过（我第一版就写错了）：只判了第 2 条 ——
                `OG_folder_matches(父目录名, 本组作者)`。对
                `陈奕迅/专辑0` 配 Aimer 的歌来说，父目录名 `陈奕迅`
                跟 `Aimer` 当然不匹配，于是判成"父级不是作者目录"，
                Aimer 的歌照样被塞进陈奕迅的目录。
                "不是**我的**作者"不等于"不是作者"。
                """
                parts = str(c.get("folder") or "").split("/")
                if len(parts) < 2:
                    return False
                parent = parts[-2]
                parent_artist = str(c.get("parent_artist") or "").strip()
                if not parent_artist:
                    # 父目录本身只是中间层、没有曲目，folders 里查不到它的画像。
                    # 这时退回用父目录名判断：它要是跟本组某个作者同名，那它就是
                    # 那位作者的目录，同样不能把歌塞进去。
                    # （真实库里 `某人的全部作品/专辑0` 就是这么组织的。）
                    for mine in _group_artists:
                        if mine and OG_folder_matches(parent, mine):
                            return True
                    return False
                if not OG_folder_matches(parent, parent_artist):
                    return False
                for mine in _group_artists:
                    if mine and OG_folder_matches(parent_artist, mine):
                        return True
                return False

            def _parent_is_group_artist(c: Dict[str, Any]) -> bool:
                """候选目录的父级是不是**本组自己**的作者目录。

                仅专辑分组时，只有"父级就是本组作者"的同名专辑目录才算"回自己家"
                （例如专辑 `MIMI/よるつむぎ` 归到 `MIMI/` 下）。父级是别人的作者
                目录时，宁可新建一个自己的 `作者/专辑`，也不能把歌塞进别人的目录。
                """
                parts = str(c.get("folder") or "").split("/")
                if len(parts) < 2:
                    return True
                parent = parts[-2]
                parent_artist = str(c.get("parent_artist") or "").strip() or parent
                for mine in _group_artists:
                    if mine and OG_folder_matches(parent_artist, mine):
                        return True
                return not _parent_looks_like_artist(c)

            def _album_dir_ok(cand: Dict[str, Any]) -> bool:
                """候选目录能不能当这张专辑的目标（按 group_by 区分严格度）。

                ★ 第一道永远是"这个目录里**装的主要就是这张专辑**"
                （`p["album"]` 是该目录出现最多的专辑标签）。
                少了这道判断，任何**名字**恰好等于专辑名的目录都会被选中 ——
                包括用户用自定义映射指定的目标目录、以及别的作者的
                `某作者/专辑0`，于是自定义映射被无声地抢走（踩过）。
                """
                if P.normalize_text(cand.get("album") or "") != P.normalize_text(album):
                    return False
                if group_by == "album":
                    leaf = str(cand.get("leaf") or "")
                    # 仅按专辑：只看**叶子层**就是专辑名的目录，
                    # 也就是 `专辑0/` 或 `专辑0/FLAC/` 这种"专辑目录的子目录"。
                    if P.normalize_text(leaf) != P.normalize_text(album):
                        return False
                    if _conflicting_identity(leaf, album):
                        return False
                    return not _parent_looks_like_artist(cand)
                # 作者/专辑：允许 `某作者/专辑`，但仍要挡住明显冲突的区分词
                return _same_folder_name(cand["folder"], album)

            cands = [p for p in pool_info if _album_dir_ok(p)]
            if group_by == "album" and len(cands) > 1:
                # 同名专辑出现在多个不同父目录下：**不能挑最浅的**（那取决于
                # 字典排序，用户完全无法预期）。优先挑曲目最多的
                # —— 那最可能是这张专辑真正的家。
                cands = sorted(cands, key=lambda c: -int(c.get("count") or 0))
            if not cands:
                # 兜底步：用专辑名做弱匹配（相似度），同样尊重 group_by
                cands = [p for p in pool_info
                         if _same_folder_name(p["folder"], album)
                         and not _conflicting_identity(p["leaf"], album)]
                if group_by == "album":
                    cands = [p for p in cands
                             if not _parent_looks_like_artist(p)
                             and _parent_is_group_artist(p)]
            if cands:
                exact = cands[0]
                dest, kind = exact["folder"], "album_exact"
                why = f"库内已有同名专辑目录「{exact['leaf']}」"
        # ③ 同名作者目录
        #    在"仅专辑"分组时**一律不用作者目录当目标** —— 那么做等于把
        #    "按专辑整理"偷偷变成"按作者整理"。实测就是这样把
        #    `Alone` 送进 `Marshmello`、`BedsideStories` 送进 `Shiroi-ice`
        #    （名字相似度低到 0.09），用户看到的方案当然是混乱的。
        if not dest and group_by != "album" and artist:
            hit = next((p for p in pool_info if OG_folder_matches(p["leaf"], artist)), None)
            if hit:
                if want_sub:
                    dest = f"{hit['folder']}/{album}"
                    kind = "artist_album"
                    why = (f"库内已有作者目录「{hit['leaf']}」，"
                           f"专辑《{album}》有 {len(in_scope_items)} 首，在其下建子目录")
                else:
                    dest, kind = hit["folder"], "artist_exact"
                    why = (f"库内已有作者目录「{hit['leaf']}」"
                           + (f"（专辑仅 {len(in_scope_items)} 首，不再分层）"
                              if group_by == "artist_album" and album else ""))
        # ③2 仅专辑模式：没有现成专辑目录时，优先归到**逐层名字等于这张专辑**
        #     的目录下（那说明用户就是按专辑建的目录，哪怕标签里作者也不对）
        if not dest and group_by == "album" and album:
            hit = next((p for p in pool_info
                        if _same_folder_name(p["folder"], album)
                        and not _parent_looks_like_artist(p)
                        and _parent_is_group_artist(p)), None)
            if hit:
                dest, kind = hit["folder"], "album_exact"
                why = f"库内目录「{hit['leaf']}」与这张专辑同名"
        # ④ 名字最相似的既有目录
        #    按作者分组时**不做这一步**：目标是作者目录本身，
        #    去比"库里哪个目录名跟作者最像"很容易（作者之间的名字相似度
        #    本来就不低，`HOYO-MiX` 和某张专辑名毫无关系也会判相似）。
        if not dest and group_by != "artist":
            sim = similar_folder(probe)
            if sim:
                dest, kind = sim["folder"], "similar"
                why = (f"库内「{sim['leaf']}」与其名称最相似"
                       f"（{sim['sim'] * 100:.0f}%），建议并入")
        # ⑤ 兜底门槛：默认保留原位（不改动既有合集目录）；
        #    也可选择收进收纳区。
        if not dest:
            if enough:
                dest, kind = canonical, "new"
                why = self_new_reason(group_by, canonical, len(in_scope_items), min_group)
            elif small_policy == "sink":
                dest, kind = sink_dest, "sink"
                why = (f"该组仅 {len(in_scope_items)} 个（少于 {min_group}），"
                       f"归入收纳区「{sink_dest}」")
            else:
                keep.extend(in_scope_items)
                small.append({"album": album, "artist": artist, "key": key,
                              "count": len(in_scope_items),
                              "folder": in_scope_items[0]["_folder"],
                              "why": f"该组仅 {len(in_scope_items)} 个"
                                     f"（少于 {min_group}），库内也无对应目录，保留原位"})
                continue

        # 已经在目标目录里的不用动
        need = [i for i in in_scope_items if i["_folder"] != dest]
        already = [i for i in in_scope_items if i["_folder"] == dest]
        keep.extend(already)
        for rec in need:
            moves.append(_move(rec, dest, why))
        actions.append({
            "album": album, "artist": artist, "key": key,
            "dst": dest, "kind": kind, "why": why,
            "count": len(need), "already": len(already),
            "total": len(in_scope_items),
            "from_folders": sorted(src_folders),
            "bytes": sum(int(i.get("size") or 0) for i in need),
            "items": [{"path": i.get("path"), "name": i["_name"],
                       "artist": i.get("artist") or "", "album": i.get("album") or "",
                       "size": int(i.get("size") or 0),
                       "cover_url": (f"/api/cover/{i.get('id')}?size=96"
                                     if i.get("id") and i.get("cover_hash") else None)}
                      for i in need],
        })

    actions = _merge_same_destination(actions)

    summary = {
        "total": len(rows),
        "keep": len(keep),
        "move": len(moves),
        "albums": len(actions),
        "group_by": group_by,
        "keep_flat_below": bool(keep_flat_below),
        "albums_to_move": len([a for a in actions if a["count"]]),
        "small_kept": len(small),
        # ★ 没有元数据、不参与智能整理的曲目数（界面要单独显示并提示手工处理）
        "manual": len(manual),
        "manual_bytes": sum(int(i.get("size") or 0) for i in manual),
        "new_dirs": len([a for a in actions if a["kind"] == "new" and a["count"]]),
        "move_bytes": sum(int(m.get("size") or 0) for m in moves),
        "min_group": int(min_group),
        "small_policy": small_policy,
        "ignored": sorted(ignore_set),
        "sinks": sorted(sink_set),
        "singles_target": sink_dest,
        "custom": custom,
    }
    moves.sort(key=lambda m: (m["dst_dir"], m["name"]))
    return {
        "mode": "album",
        "summary": summary,
        "actions": actions,
        "small": sorted(small, key=lambda x: -x["count"])[:300],
        "moves": moves,
        "keep": [{"path": i.get("path"), "name": i["_name"], "folder": i["_folder"]}
                 for i in keep],
        # 无元数据曲目：给界面一份明细，方便用户直接去手工处理
        "manual": [{"path": i.get("path"), "name": i["_name"],
                    "folder": i["_folder"],
                    "size": int(i.get("size") or 0),
                    "id": i.get("id"),
                    "why": "没有 artist / album 标签，无法判断归属"}
                   for i in manual][:500],
        "folders_now": len([f for f in folders if f]),
        "library_folders": [{"folder": f, "count": folders[f]["count"],
                             "album": (max(folders[f]["albums"].items(),
                                           key=lambda x: x[1])[0] if folders[f]["albums"] else ""),
                             "artist": (max(folders[f]["artists"].items(),
                                            key=lambda x: x[1])[0] if folders[f]["artists"] else ""),
                             "sink": f in sink_set}
                            for f in sorted(folders) if f][:500],
    }


def OG_folder_matches(folder: str, want: str) -> bool:
    """`folder_matches_name` 的别名（历史上叫过两个名字，统一一下）。"""
    return folder_matches_name(folder, want)


def suggest_folder_merges(rows: Sequence[Dict[str, Any]], root: str,
                          threshold: float = 0.9,
                          core_threshold: float = 0.85) -> List[Dict[str, Any]]:
    """找出"其实是同一个东西、却被拆成两个目录"的文件夹。

    这是"≥5 首就合并"真正该管的场景 —— 不是按专辑去重排，而是抓这类
    名字几乎一样、内容也重合的重复目录：

      · `Vicetone/` 与 `Vexento/`（目录名拼错了，里面都是 Vexento 的歌）
      · `Elektornomia/` 与 `Elektronomia/`

    这类目录里往往都已经攒了 5 首以上同一作者的歌，却因为拼写差异被算法
    当成两个不同的作者 —— 与其让整理逻辑在它们之间乱搬，不如直接把
    "这两个目录该并成一个"报给用户。

    两道必须同时过的闸门（都是被真实误报逼出来的）：
      1. **卷号必须相同**：`星空剧场2` 与 `星空剧场3`、`上篇` 与 `下篇`
         名字相似度 94%，但它们是同系列的不同卷，合并等于把两张专辑搅在一起；
      2. **名字要几乎一样**（默认 ≥0.9），**而且去掉通用词之后剩下的"特征部分"
         也得几乎一样**（≥0.85）：`Minecraft - Nether Update` 与
         `Minecraft - The Wild Update` 整名相似度 92%，可它们是两个不同的官方
         更新专辑；只看整名相似度就会误报。
         这一条还顺带否掉了 `Vicetone/` 与 `Vexento/` —— 它们其实是两个不同的
         音乐人（特征部分相似度只有 0.53），不该建议合并。
    宁可漏报也不要误报 —— 用户看到一条假建议就可能真的合错。真正该报的是
    `Elektornomia/` 与 `Elektronomia/`（0.92）这种一眼就是拼错的。
    """
    root_n = P.norm(root)
    folders = build_folders(rows, root_n)
    names = [(f, f.rsplit("/", 1)[-1]) for f in folders if f]
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, (fa, la) in enumerate(names):
        for fb, lb in names[i + 1:]:
            if fa == fb or (fa, fb) in seen or (fb, fa) in seen:
                continue
            # 只看顶层目录才谈得上"同一个作者拼错两次"；嵌套目录不掺和
            if "/" in fa or "/" in fb:
                continue
            # 闸门 1：不同卷 → 不是重复，是系列里的不同专辑
            if volume_signature(la) != volume_signature(lb):
                continue
            sim = P.similarity(P.normalize_text(la), P.normalize_text(lb))
            if sim < threshold:
                continue
            # 闸门 2b：去掉通用词后剩下的"特征部分"也得像。
            # `Vicetone`/`Vexento` 去掉通用词还是一样，相似度 0.875；
            # `Minecraft - Nether Update`
            # `minecraft nether` vs `minecraft wild`，相似度只有 0.67。
            core_a, core_b = distinctive_name(la), distinctive_name(lb)
            if core_a and core_b:
                core_sim = P.similarity(core_a, core_b)
                if core_sim < core_threshold:
                    continue
            da, db = folders[fa], folders[fb]
            art_a = max(da["artists"].items(), key=lambda x: x[1])[0] if da["artists"] else ""
            art_b = max(db["artists"].items(), key=lambda x: x[1])[0] if db["artists"] else ""
            # 内容也得像：主流作者能对上（模糊比较），或者作者名出现在对方目录名里
            content_sim = (P.similarity(P.normalize_text(art_a), P.normalize_text(art_b))
                           if art_a and art_b else 0.0)
            if content_sim < 0.84 and not (
                    art_a and (P.normalize_text(art_a) in P.normalize_text(lb)
                               or P.normalize_text(art_b) in P.normalize_text(la))):
                continue
            want = art_a if art_a else art_b
            keep = fa if len(da["items"]) >= len(db["items"]) else fb
            drop = fb if keep == fa else fa
            # 谁的目录名更像标签里的作者，就以谁为主
            if want and P.similarity(P.normalize_text(
                    drop.rsplit("/", 1)[-1]), P.normalize_text(want)) > \
                    P.similarity(P.normalize_text(
                        keep.rsplit("/", 1)[-1]), P.normalize_text(want)):
                keep, drop = drop, keep
            seen.add((fa, fb))
            out.append({
                "keep": keep, "drop": drop,
                "keep_count": len(folders[keep]["items"]),
                "drop_count": len(folders[drop]["items"]),
                "name_similarity": round(sim, 3),
                "artist_hint": want,
                # 名字只有九成多像时，有可能是"同作者的两张不同专辑"。
                # 让用户自己判断，而不替他下结论。
                "confident": sim >= 0.95 or want == keep.rsplit("/", 1)[-1],
                "detail": f"「{keep.rsplit('/', 1)[-1]}」和"
                          f"「{drop.rsplit('/', 1)[-1]}」名字几乎一样"
                          f"（{sim * 100:.0f}%），内容也高度重合，"
                          "很可能是同一批歌被放在了两个目录里",
                "suggest": f"把「{drop}」里的歌并入「{keep}」",
            })
    out.sort(key=lambda x: (-int(x["confident"]), -x["drop_count"]))
    return out


def stats_by_folder(tracks: Sequence[Dict[str, Any]], root: str) -> List[Dict[str, Any]]:
    """每个目录里有多少首、是不是"纯"的（同一作者+专辑）。供界面预览现状。"""
    root_n = P.norm(root)
    out = []
    for folder, d in build_folders(tracks, root_n).items():
        out.append({
            "folder": folder,
            "count": d["count"],
            "artists": len(d["artists"]),
            "albums": len(d["albums"]),
            "pure": len(d["artists"]) <= 1 and len(d["albums"]) <= 1,
            "sink": d["sink"],
            "top_artist": max(d["artists"].items(), key=lambda x: x[1])[0]
            if d["artists"] else "",
        })
    out.sort(key=lambda x: (-x["count"], x["folder"]))
    return out

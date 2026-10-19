"""跨平台路径工具。

Windows 的目录分隔符是 `\\`，Linux/macOS 是 `/`，WebDAV/HTTP 一律是 `/`。
这里统一成"内部使用正斜杠、展示时按源类型转换"的策略。
"""
from __future__ import annotations

import os
import posixpath
import re
import unicodedata
from typing import Union, Iterable, List

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus",
    ".wav", ".aiff", ".aif", ".ape", ".wv", ".wma", ".dsf", ".dff",
    ".mp4", ".m4b", ".mpc", ".tta", ".mka", ".caf",
}
LOSSLESS_EXTS = {".flac", ".alac", ".ape", ".wv", ".aiff", ".aif", ".wav", ".dsf", ".dff", ".tta"}
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
COVER_STEMS = {
    "cover", "folder", "front", "album", "albumart", "albumartsmall",
    "artwork", "thumb", "poster", "case", "碟片", "封面",
}


def norm(p: str) -> str:
    """统一为内部形式：正斜杠、去掉多余斜杠、Windows 盘符保留。"""
    if not p:
        return ""
    p = str(p).replace("\\", "/")
    # 保留 UNC 前缀 //host/share
    unc = p.startswith("//")
    p = re.sub(r"/{2,}", "/", p)
    if unc and not p.startswith("//"):
        p = "/" + p
    # 保留 Windows 盘符大小写形式
    m = re.match(r"^([a-zA-Z]):/(.*)$", p)
    if m:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


def _is_windows_abs(p: str) -> bool:
    """Windows **绝对**路径：`D:/Music`、`D:\\Music` 或 UNC（`//host/share`）。

    ★ `C:foo` 这种"驱动器相对路径"**不算绝对路径**。

    踩过：原来只判断 `p[1] == ":"`，于是 `C:foo` 被判成绝对路径，
    `abs_path()` 会原样返回它。`C:foo` 的含义是"当前进程在 C 盘的
    工作目录下的 foo" —— 落点取决于**进程的工作目录**，完全不可预测。
    审计时构造完整逃逸链验证过它目前**不可利用**（`_inside_root` 挡住了），
    但 `_do_copy` / `_do_remove` 并没有这一步检查，属于"哪天换个调用路径
    就会出事"的类型。这里从源头收紧：认绝对路径就要求 `D:/` 或 `D:\\`。
    """
    if not p:
        return False
    if p.startswith("//") or p.startswith("\\\\"):
        return True
    if len(p) > 2 and p[1] == ":" and p[0].isalpha():
        # 必须跟一个分隔符才算绝对；`D:` 单独出现（盘符本身）也接受
        return p[2] in ("/", "\\")
    return len(p) == 2 and p[1] == ":" and p[0].isalpha()


def join(base: str, *parts: str) -> str:
    out = norm(base)
    for part in parts:
        part = norm(part)
        if not part:
            continue
        if not out:
            out = part
        else:
            out = out.rstrip("/") + "/" + part.lstrip("/")
    return norm(out)


def parent(p: str) -> str:
    p = norm(p)
    if not p or p.endswith(":/"):
        return ""
    return norm(posixpath.dirname(p))


def basename(p: str) -> str:
    return posixpath.basename(norm(p))

def stem(name: str) -> str:
    b = basename(name)
    dot = b.rfind(".")
    return b[:dot] if dot > 0 else b


def ext(name: str) -> str:
    b = basename(name)
    dot = b.rfind(".")
    return b[dot:].lower() if dot > 0 else ""


def is_audio(name: str) -> bool:
    return ext(name) in AUDIO_EXTS


def is_cover(name: str) -> bool:
    e = ext(name)
    if e not in COVER_EXTS:
        return False
    return stem(name).strip().lower() in COVER_STEMS


def display_path(p: str, sep: str = "/") -> str:
    """按目标平台的风格展示路径。"""
    return norm(p).replace("/", sep)


def is_windows_abs(p: str) -> bool:
    """是否 Windows 绝对路径：`D:/Music`、`D:` 或 UNC（`//host/share`、`\\\\host\\share`）。"""
    return _is_windows_abs(p)


def human_size(n: Union[int, float, None]) -> str:
    if not n:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.2f} {units[i]}"


# ------------------------------------------------------------------ 文本归一化

_PUNCT = re.compile(r"[_\-–—·・\.\+\&,，、\(\)\[\]\{\}/\\!！\?？'\"“”‘’:：;；~～@#\*]+")
_QUALITY = re.compile(
    r"""(?ix)
    (?:
       \b(?:hq|sq|hi[-_ ]?res|hd|cd|dsd|hires|remaster(?:ed)?|deluxe|reissue|re-?rip|web[-_ ]?dl|
          vinyl|flac|ape|wav|alac|aiff|mp3|320|256|192|128|vbr|cbr|kbps|khz|hz|bit|lossless|无损|
          高音质|发烧|试音|母带|完整版|原版|珍藏|精选|合集|dj版|伴奏|纯音乐)\b
     | (?<=\s)\d{2,3}\s?(?:k|kbps)
     | \[[^\]]{0,24}\]
     | 【[^】]{0,24}】
     | \b(?:19|20)\d{2}\b(?=\s*[-_\)]?\s*$)
    )
    """
)
_TRACKNO = re.compile(r"^\s*(?:cd\s*\d+\s*[-_ ]?)?\d{1,3}\s*[-_.、 ]\s*")

# ------------------------------------------------------------------ 版本标记
# 伴奏 / 重制 / 现场 / 翻唱…… 这些是"不同的录音"，不是同一条录音的多份副本。
# 归一化会**故意**把它们当噪声删掉（这样跨库同名匹配才稳），所以查重必须
# 单独把它们提出来当分组依据，否则原曲、伴奏、remix 会被并成一组。
_VER_PATTERNS = [
    # (规范化标签, 正则)
    ("instrumental", r"instrumental|off[\s_-]?vocal|karaoke|backing[\s_-]?track|"
                     r"mr[\s_-]?remix|伴\s*奏|纯\s*音\s*乐|无人声|去人声|消音版"),
    ("remaster", r"remaster(?:ed)?|重制|重置版|重新录制|母带重制"),
    ("remix", r"re[\s_-]?mix|rmx|混音版|混音|改编版|bootleg|mashup|"
              r"extended[\s_-]?(?:mix|version|edit)|club[\s_-]?mix|radio[\s_-]?edit|dj\s*版|加长版"),
    ("live", r"live|现场版|演唱会|live[\s_-]?version|unplugged"),
    ("acoustic", r"acoustic|不插电|原声版"),
    ("cover", r"cover(?:ed)?|翻唱|模仿秀"),
    ("demo", r"demo|小样"),
    ("remake", r"re[\s_-]?(?:recording|recorded)|重唱|再版"),
    ("language", r"国\s*语|粤\s*语|日\s*语|英\s*语|韩\s*语|闽\s*南\s*语|台\s*语|"
                 r"mandarin|cantonese|japanese|english[\s_-]?version|korean"),
    ("edit", r"\bedit\b|剪辑版|短版|长版|radio[\s_-]?cut"),
    # ★ 下面这几类是"网络改版"常见的产物，**不同的录音**，
    #   但原来一个都没认出来 —— 于是 `Song.mp3` 和 `Song (Sped Up).mp3`
    #   版本标记都是空串，被当成同一首并进同一组，用户点"清理重复"
    #   就会丢掉其中一份真正不同的版本。宁可分开让用户自己判断。
    ("speed", r"sped[\s_-]?up|speed[\s_-]?up|\bslowed\b|slow(?:ed)?[\s_-]?(?:down|ver)|"
              r"night[\s_-]?core|加速版|减速版|慢速版|降速版|鬼畜"),
    ("clip", r"\btv[\s_-]?(?:size|ver|version|edit|cut)\b|\bshort[\s_-]?ver\b|"
             r"short[\s_-]?edit|片段版|试\s*听"),
    ("version", r"版|版本"),
]
# "xxx版" 兜底：只认中文字修饰词（英文版本词上面已覆盖，避免误伤 "320k" 这类
# 码率写法被切成 "k" 再撞上 "版"）。修饰词至少 2 个字，排除"高版"这种碎片。
_VER_PREFIX = re.compile(r"([\u4e00-\u9fa5]{2,4})版")
_VER_ZHTW = re.compile(r"繁\s*体|简\s*体|traditional|simplified")
_VER_COMPILED = [(tag, re.compile(rx, re.I)) for tag, rx in _VER_PATTERNS]

# 供界面展示 / 配置校验用的版本类别清单
VERSION_KINDS = [
    ("instrumental", "伴奏 / 纯音乐 / 卡拉OK"),
    ("remaster", "重制版 / 母带重制"),
    ("remix", "Remix / 混音 / DJ版 / 加长版"),
    ("live", "现场版 / 演唱会"),
    ("acoustic", "不插电 / 原声版"),
    ("cover", "翻唱"),
    ("demo", "Demo / 小样"),
    ("remake", "重唱 / 重新录制"),
    ("language", "国语/粤语/日语版等语言版本"),
    ("edit", "剪辑版 / 短版 / Radio Edit"),
    ("speed", "加速 / 减速 / Nightcore"),
    ("clip", "TV Size / 片段 / 试听"),
    ("version", "其他「xx版」"),
    ("script", "简体 / 繁体"),
]


def version_tags(*texts: str) -> str:
    """提取"版本标记"，用来把伴奏 / 重制版 / 现场版 / 翻唱 等区分开。

    `晴天.mp3`              -> `""`
    `晴天 (伴奏).mp3`        -> `"instrumental"`
    `晴天 (伴奏) (Live).mp3` -> `"instrumental|live"`
    `Time (Remastered).mp3` -> `"remaster"`

    返回排序拼接的稳定字符串，便于直接参与分组键比较。
    """
    hay = " ".join(str(t) for t in texts if t)
    if not hay:
        return ""
    hay = unicodedata.normalize("NFKC", hay)
    found = set()
    for tag, rx in _VER_COMPILED:
        if rx.search(hay):
            found.add(tag)
    if _VER_ZHTW.search(hay):
        found.add("script")
    for m in _VER_PREFIX.finditer(hay):
        word = m.group(1).strip().lower()
        if len(word) <= 4 and word:
            found.add("v:" + word)
    return "|".join(sorted(found))


# 版本标记 -> 中文短标签，供界面直接显示
VERSION_LABELS = {
    "instrumental": "伴奏/纯音乐",
    "remaster": "重制版",
    "remix": "Remix/混音",
    "live": "现场版",
    "acoustic": "不插电",
    "cover": "翻唱",
    "demo": "Demo",
    "remake": "重唱",
    "language": "语言版本",
    "edit": "剪辑版",
    "speed": "变速/Nightcore",
    "clip": "TV/片段",
    "version": "其他版本",
    "script": "简繁",
}

_BRACKET_KEEP = re.compile(r"[\(\[（【]\s*(?:feat|ft|with)\.?\s*[^\)\]）】]*[\)\]）】]", re.I)

# "重复下载计数"尾巴：`Song (1)` / `Song（2）` / `Song [3]`。
# 要求括号里**只有数字**（可带空格或 `copy`），且括号前还有实际内容 ——
# 这样 `(1)` 这种真实曲名、`(Live)`、`(Remix)` 都不会被误伤。
# NFKC 之后全角括号已变成半角，所以这里只需要认 `(` `[`。
_DUP_COUNTER = re.compile(
    r"(?<=[^\s\(\[\-–—_])\s*[\(\[]\s*(?:copy\s*)?\d{1,2}\s*[\)\]]\s*$", re.I)


def normalize_text(s: str) -> str:
    """音频文本归一化：全角转半角、小写、去标点、压缩空白。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def name_key(name: str) -> str:
    """把文件名还原成"同一首歌"的键。

    `01 - 周杰伦 - 晴天 (320k).mp3` -> `周杰伦 晴天`

    ★ 还要干掉"重复下载计数"尾巴：`Song (1).mp3` / `Song（2）.flac`。

    为什么必须在这里做：浏览器和下载工具遇到同名文件会自动加 `(1)`，
    于是同一首歌在库里出现两份（一份 `Song.mp3`、一份 `Song (1).mp3`）。
    这正是查重最该抓到的情况，但两个名字归一化后是 `song` 和 `song 1`
    —— 键不同、名称相似度只有 0.80，跨键合并的门限是 0.62 也救不回来
    （实测 `Song.mp3` 与 `Song (1).mp3` 一个组都组不起来）。

    只在 `name_key` 这一处归一，`dup_key` / `song_key` / 对比配对 / 跨键合并
    就全都一致了 —— 分散到各处去补丁很容易又变成"两套规则打架"。

    只剥"纯计数的括号尾巴"，且**要求前面还有实际内容**：
    真正的曲名 `(1)`、`Song (Live)`、`Song (Remix)` 都不受影响。
    """
    if not name:
        return ""
    s = stem(name)
    s = unicodedata.normalize("NFKC", str(s))
    s = _DUP_COUNTER.sub(" ", s)
    s = _BRACKET_KEEP.sub(" ", s)
    s = _TRACKNO.sub("", s)
    # 先干掉 "320kbps" / "320K" / "96khz" 这类紧贴数字的音质标记（后面按词删不掉）
    s = re.sub(r"(?i)\d{2,4}\s?(?:k|kbps|khz|hz|bit)\b", " ", s)
    s = _QUALITY.sub(" ", s)
    s = re.sub(r"(?i)\b\d{1,3}\s?k(?:bps)?\b", " ", s)
    s = re.sub(r"[\(\[（【]\s*[\)\]）】]", " ", s)
    s = normalize_text(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_ARTIST_SEPS = (" feat. ", " feat ", " ft. ", " ft ", " & ", " and ", " vs ", " x ", "、", "，", ",", "; ", " / ")


def primary_artist(artist: str) -> str:
    if not artist:
        return ""
    a = str(artist)
    low = a.lower()
    cut = len(a)
    for sep in _ARTIST_SEPS:
        idx = low.find(sep)
        if idx > 0:
            cut = min(cut, idx)
    return a[:cut].strip()


def tokens(s: str) -> List[str]:
    return [t for t in normalize_text(s).split(" ") if t]


def _is_subsequence(needle: str, hay: str) -> bool:
    """needle 的字符是否按顺序出现在 hay 里（用于 "zjl" 匹配 "zhoujielun"）。"""
    if not needle:
        return False
    it = iter(hay)
    return all(ch in it for ch in needle)


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))


def fuzzy_match(query: str, *fields: str, threshold: float = 0.62) -> float:
    """模糊匹配打分，0 表示不匹配，越高越像。用于搜索框。

    依次尝试（取最高分）：
      1.0  原样子串命中（最快也最准，"晴天" 命中 "周杰伦 - 晴天"）
      0.95 归一化后的子串命中（忽略大小写/标点/全半角）
      0.9  归一化后按词全部命中（"周杰伦 晴天" 这种多词查询）
      0.78 中文逐字命中（中文没有词边界，只能按字比）
      ≤0.9 逐字段算相似度（容忍错别字："青花瓷" vs "青花词"）
      0.5  子序列命中（"zjl" → "zhoujielun"，适合打缩写）
    """
    q = (query or "").strip()
    if not q:
        return 0.0
    cands = [str(f) for f in fields if f]
    if not cands:
        return 0.0

    # 1) 原样子串
    ql = q.lower()
    for f in cands:
        if ql in f.lower():
            return 1.0

    nq = normalize_text(q)
    if not nq:
        return 0.0
    qt = [t for t in nq.split(" ") if t]
    # 中文查询才做"逐字"匹配：中文没有空格词边界，只能按字比；
    # 拉丁文字若也这么比，"adele" 会因为字母零散出现在
    # "04 queen bohemian flac" 里而误命中（踩过这个坑）。
    cjk_query = _has_cjk(nq)

    best = 0.0
    for f in cands:
        nf = normalize_text(f)
        if not nf:
            continue
        # 2) 归一化子串
        if nq in nf:
            best = max(best, 0.95)
            continue
        # 3) 多词查询：每个词都出现就算命中
        if len(qt) > 1 and all(t in nf for t in qt):
            best = max(best, 0.9)
            continue
        # 3b) 中文逐字命中（只在中文查询时启用）
        if cjk_query and len(nq) >= 2 and all(ch in nf for ch in nq):
            best = max(best, 0.78)
        # 4) 相似度（容忍错别字）。要按长度打折：
        #    "adele" 与 "03 adele hello flac" 的 difflib 相似度有 0.6 左右，
        #    但那只是"沾边"；短查询命中一个长字符串必须匹配得**更完整**才算数。
        #    直接用相似度会让搜索框到处蹦出无关结果。
        sim = similarity(nq, nf)
        if sim >= threshold:
            coverage = len(nq) / max(len(nq), len(nf))
            best = max(best, min(0.9, sim * (0.5 + 0.5 * coverage)))
        # 5) 缩写（子序列）：中文用拼音首字母（"zjl"→"zhoujielun"），
        #    拉丁查询要求至少 3 个字符且彼此邻接得比较紧，否则三个字母
        #    到处都是"子序列"，会满屏误命中。
        if _is_subsequence(nq, nf) and (cjk_query or len(nq) >= 3):
            if cjk_query or _subseq_is_tight(nq, nf):
                best = max(best, 0.5)
    return round(best, 4)


def _subseq_is_tight(needle: str, hay: str, max_span_ratio: float = 3.5) -> bool:
    """子序列命中的字符是否"挨得够近"。

    这是为了区分真正的缩写和"字母恰好散落在文件名里"：
      · `zjl` 在 `zhoujielun qingtian flac` 里跨越 9 个字符 —— 这是拼音首字母
        缩写（周杰伦 → z-j-l），应当命中；
      · 随机三个字母落在长文件名里通常跨越二三十个字符 —— 不该命中。
    理论上限取 `max(12, 查询长度 * 3.5)`，正好把上面两种情况分开。
    """
    if not needle:
        return False
    pos = []
    start = 0
    for ch in needle:
        i = hay.find(ch, start)
        if i < 0:
            return False
        pos.append(i)
        start = i + 1
    span = pos[-1] - pos[0] + 1
    return span <= max(12, int(len(needle) * max_span_ratio))


def similarity(a: str, b: str) -> float:
    """混合相似度：字符级 + 词级，针对中英混排文件名。"""
    a = normalize_text(a)
    b = normalize_text(b)
    if not a and not b:
        # ★ 两侧都归一化成空串时**不是**"完全一致"，而是"没有证据"。
        #
        #   归一化会把 `320k` / `[FLAC]` / `伴奏` / `纯音乐` / `无损` 这类词整体删掉，
        #   于是两个毫不相关的文件都能得到空 `name_key`。这里返回 1.0 会让
        #   `name_similarity` 直接拿到满分，压过 `FUZZY_NAME_MIN` / 0.85 / 0.9
        #   那些名称门禁的全部意义 —— 实测 `320k.flac` 与 `无损.flac` 被判成
        #   `name_sim=1.0, score=0.92, identical=True`，单库里还会被塞进同一个
        #   `"name:"` 兜底组并建议"保留一份、清理其它"。
        return 0.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    import difflib

    char_ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(tokens(a)), set(tokens(b))
    if ta and tb:
        jac = len(ta & tb) / len(ta | tb)
        contain = 1.0 if (a in b or b in a) and min(len(a), len(b)) >= 4 else 0.0
        return max(char_ratio, 0.45 * char_ratio + 0.4 * jac + 0.15, contain * 0.93)
    return char_ratio


# ------------------------------------------------------------------ Windows 命名规则
# ★ 唯一来源。以前 `upload_api` / `station_api` 各有一份保留名清单，
#   而新建目录、目录改名、文件改名、智能整理的目录名**一份都没有**。
#
# 实测（Win11 + Python 3.13，见 docs/SECURITY_REVIEW_2026-10-19.md 第六节）：
#   · `mkdir CON` 成功 —— 得到一个资源管理器里打不开、删不掉的目录
#   · `NUL` 当目录名：makedirs 静默"成功"但目录不存在，之后往里搬文件
#     全部 `WinError 3`；整张专辑改名成 `nul` 会**逐个文件报错、半途而废**
#   · `nul`（不带扩展名）当文件名：写入不报错，数据被设备吞掉（0 字节）
#   · 名字末尾的 `.` / 空格会被系统**静默剥掉**：`Album.` 实际落成 `Album`，
#     可能撞上已有的同名目录；返回给界面的路径和磁盘上的对不上
#   · 控制字符（\x00-\x1f）直接 `WinError 123`
#
# 这些规则在 Linux 上不是硬限制，但数据源可能是映射盘 / SMB 共享 / 之后拷到
# Windows 上的 U 盘，所以**不分平台一律拒绝**：库里的名字应该在哪都能用。
WIN_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    # 上标数字形态（COM¹ 等）在新版 Windows 上同样是设备名
    | {f"COM{c}" for c in "¹²³"}
    | {f"LPT{c}" for c in "¹²³"}
)
WIN_INVALID_CHARS = '<>:"/\\|?*'
_WIN_CTRL = re.compile(r"[\x00-\x1f]")


def is_win_reserved(name: str) -> bool:
    """这个名字是不是 Windows 保留设备名（含 `CON.flac`、`nul .mp3` 这类形态）。

    规则：取第一个 `.` 之前的部分、去掉尾随空格后比较（不区分大小写）——
    Windows 判设备名就是这么切的，所以 `CON.tar.gz` 也是设备名。
    """
    stem = str(name or "").split(".", 1)[0].rstrip(" ")
    return stem.upper() in WIN_RESERVED_NAMES


def name_problem(name: str) -> str:
    """单段文件/目录名在 Windows 上的问题；没问题返回空串。

    只检查**一段**名字（不含路径分隔符）。用于"用户/标签给的名字 → 落盘"
    之前的拒绝式校验：返回的文字直接给用户看。
    """
    s = str(name or "")
    if not s.strip():
        return "名字不能为空"
    if s in (".", ".."):
        return "名字不能是 . 或 .."
    bad = sorted({ch for ch in s if ch in WIN_INVALID_CHARS})
    if bad:
        return "名字不能包含 " + " ".join(bad)
    if _WIN_CTRL.search(s):
        return "名字不能包含控制字符"
    if s[-1] in ". ":
        return "名字不能以 . 或空格结尾（Windows 会自动去掉，实际名字会和你看到的不一样）"
    if is_win_reserved(s):
        return f"「{s}」是 Windows 保留的设备名（CON / NUL / COM1 等），不能用作文件或目录名"
    if len(s) > 255:
        return "名字太长（单段不能超过 255 个字符）"
    return ""


def path_problem(rel: str) -> str:
    """相对路径里**每一段**的命名问题；没问题返回空串。"""
    for seg in norm(rel).strip("/").split("/"):
        if not seg:
            continue
        msg = name_problem(seg)
        if msg:
            return f"{msg}（{seg}）"
    return ""


def safe_name(name: str, fallback: str = "") -> str:
    """把**不可信来源**的名字（标签值、上传文件名）改造成可落盘的名字。

    与 `name_problem` 的分工：用户亲手输入的名字 → 拒绝并提示（不悄悄改）；
    来自标签/上传的名字 → 用这里修正（用户没法逐个去改几百个标签）。
    """
    s = _WIN_CTRL.sub(" ", str(name or ""))
    for ch in WIN_INVALID_CHARS:
        s = s.replace(ch, "_")
    s = s.strip().rstrip(". ").strip()
    if s in ("", ".", ".."):
        return fallback
    if is_win_reserved(s):
        # 加前缀而不是替换：保留原名可读，同时不再是设备名
        s = "_" + s
    if len(s) > 200:
        stem, ext = os.path.splitext(s)
        s = stem[:200 - len(ext)].rstrip(". ") + ext
    return s or fallback


def has_dotdot(p: str) -> bool:
    """路径里是否含 `..` 段。

    `norm()` **不折叠** `..`（它只统一分隔符与首尾），所以
    `root + "/../../etc"` 在 `startswith(root)` 面前是能通过的。
    任何"拼出来再判前缀"的地方都必须先过这里。
    """
    s = str(p or "").replace("\\", "/")
    return any(seg == ".." for seg in s.split("/"))


def inside_root(root: str, path: str) -> bool:
    """`path` 是否确实落在 `root` 内部。

    和 `ops._inside_root` 同一套判据（这里是唯一实现，那边只做转发）：
      · 必须是绝对路径
      · 不能含 `..`
      · Windows 大小写不敏感（配置里的 root 大小写常和扫描结果不一致）
    """
    p = norm(path)
    if not p or not (p.startswith("/") or is_windows_abs(p)):
        return False
    if has_dotdot(p):
        return False
    r = norm(root).rstrip("/")
    if not r:
        # ★ root 是文件系统根（`/`）时，`rstrip("/")` 会得到空串。
        #   原来这里直接 `return False`，于是"把 Linux 根或容器挂载点 `/`
        #   设为数据源"的情况下，**所有路径都被判成越界**，移动/删除全部报错。
        #   用一个单字符哨兵表示"根"，后面的前缀判断就自然成立了。
        r = "/" if norm(root) in ("/", "") else r
        if not r:
            return False
    if os.name == "nt":
        p, r = p.casefold(), r.casefold()
    if r == "/":
        # root 就是文件系统根：任何绝对路径都在它下面
        return p.startswith("/")
    return p == r or p.startswith(r + "/")


def inside_root_real(root: str, path: str) -> bool:
    """在 `inside_root` 基础上再用 realpath 比一次 —— 挡 junction / 符号链接。

    ★ 为什么必须有：`inside_root` 是**纯字符串**判断。Windows 的目录联接
      （`mklink /J lib\\jlink D:\\outside`）在 `os.path.islink()` 眼里**不是**
      符号链接（`is_symlink()` 返回 False），但它是 reparse point，读写会
      被带到源根之外。实测顺着一个 junction 可以：

        · 扫描把库外文件索引进来
        · 列出库外目录
        · 移动/改标签/新建目录/永久删除 全部落到库外
        · 上传入库写到库外

      这里把两边解析成真实路径再比前缀。路径不存在时 realpath 仍会解析
      已存在的父级组件，所以"准备写一个新文件"的检查同样有效。
    """
    if not inside_root(root, path):
        return False
    try:
        r = os.path.realpath(norm(root))
        p = os.path.realpath(norm(path))
    except (OSError, ValueError):
        return False
    rr = r.rstrip("\\/") or r
    pp = p.rstrip("\\/") or p
    if os.name == "nt":
        return pp.lower() == rr.lower() or pp.lower().startswith(rr.lower() + os.sep)
    return pp == rr or pp.startswith(rr + os.sep)

"""生成端到端测试用的小音乐库（真实 WAV，可被 mutagen 解析）。

    uv run --with mutagen --with Pillow python tests/make_sample_libs.py /tmp/msh-e2e
"""
from __future__ import annotations

import math
import random
import struct
import sys
import wave
from pathlib import Path

SR = 22050


def tone(seconds: float, freq: float, amp: float = 0.35, sr: int = SR) -> bytes:
    n = int(sr * seconds)
    rnd = random.Random(int(freq * 1000) + n + int(amp * 1000))
    out = bytearray()
    for i in range(n):
        t = i / sr
        v = amp * math.sin(2 * math.pi * freq * t)
        v += 0.02 * (rnd.random() - 0.5)
        s = int(max(-1.0, min(1.0, v)) * 32767)
        out += struct.pack("<hh", s, s)
    return bytes(out)


def write_wav(path: Path, seconds: float, freq: float, amp: float = 0.35,
              sr: int = SR, channels: int = 2) -> None:
    """写一个真实的 PCM WAV。`sr`/`channels` 用来模拟不同音质（体积随之变化）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = tone(seconds, freq, amp, sr=sr)
    if channels == 1:
        frames = frames[::4]  # 取每个 16bit 立体声帧的左声道
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)


def write_junk_cover(path: Path, size: int = 700) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0" + bytes((i * 37) % 256 for i in range(size)) + b"\xff\xd9")


SONGS = [
    ("周杰伦 - 晴天", 12.0, 440.0),
    ("Adele - Hello", 9.0, 330.0),
    ("Daft Punk - Get Lucky", 7.0, 220.0),
    ("久石让 - Summer", 6.0, 550.0),
    ("Pink Floyd - Time", 15.0, 180.0),
]


def build(root: Path) -> None:
    """构造一个结果唯一确定的对称场景。

    设计原则：每个"差异点"只引入一次，避免互相干扰。
      * 5 首两边都有（文件名完全相同）
      * 1 首 Windows 独有 / 1 首 Linux 独有      → 考"查漏"
      * 1 首两边都有但文件名不同                  → 考"模糊匹配 + 改名冲突"
      * Windows 侧有 1 份同名副本（字节相同）      → 考"精确查重"
      * Windows 侧有 1 份低音质版（字节不同）      → 考"相似但不算同一份"
      * 1 个损坏文件                              → 考"单个失败不影响整体"

    最终：win = 10 首，linux = 7 首；
          对比后 matched = 6、only_left = 4、only_right = 1（方向左→右补齐）；
          补齐后两边都是 11 首，一一对应。
    """
    import shutil as _shutil
    if root.exists():
        _shutil.rmtree(root)
    win = root / "win" / "Music"
    lin = root / "linux" / "music"

    # 1) 两边都有的 5 首
    for i, (name, dur, f) in enumerate(SONGS, start=1):
        write_wav(win / "Pop" / f"{i:02d} - {name}.wav", dur, f)
        write_wav(lin / "Pop" / f"{i:02d} - {name}.wav", dur, f)
    write_junk_cover(win / "Pop" / "cover.jpg")
    write_junk_cover(lin / "Pop" / "cover.jpg", 900)

    # 2) 各自独有
    write_wav(win / "Rock" / "Queen - Bohemian Rhapsody.wav", 4.0, 700.0)
    write_wav(lin / "Jazz" / "Miles Davis - So What.wav", 5.0, 800.0)

    # 3) 同一首歌、两边文件名不同（考模糊匹配）
    write_wav(win / "Rock" / "08 - Renamed Artist - Same Song.wav", 8.0, 260.0)
    write_wav(lin / "Rock" / "Renamed Artist - Same Song.wav", 8.0, 260.0)

    # 4) Windows 侧内部重复（考查重）
    #    (a) 字节完全相同的副本 → 应被判定为"可以安全删除"
    src_dup = win / "Pop" / "03 - Daft Punk - Get Lucky.wav"
    dst_dup = win / "Backup" / "03 - Daft Punk - Get Lucky.wav"
    dst_dup.parent.mkdir(parents=True, exist_ok=True)
    _shutil.copyfile(src_dup, dst_dup)
    #    (b) 低音质版（体积/内容不同）→ 只能算"相似"，需人工决定
    write_wav(win / "Dup" / "Daft Punk - Get Lucky (低音质).wav", 7.0, 220.0, amp=0.12, sr=11025)

    # 5) 损坏文件
    bad = win / "Broken" / "corrupt.mp3"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\x00" * 3000)

    print(f"生成完成：{root}")
    for p in sorted(root.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(root)}  ({p.stat().st_size} B)")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/msh-e2e")
    if target.exists():
        import shutil

        shutil.rmtree(target)
    build(target)

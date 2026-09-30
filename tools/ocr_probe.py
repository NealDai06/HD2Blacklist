# -*- coding: utf-8 -*-
"""真实 OCR 验证脚本（不属于交付物，仅用于联调时人工确认识别效果）。

渲染几张"像游戏聊天框 / 玩家列表"的图片，用真实的 RapidOCR 引擎识别，
再把识别结果喂给 matcher.match_text，验证整条链路。

用法：
    python tools/ocr_probe.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PIL import Image, ImageDraw, ImageFont          # noqa: E402

from app import config  # noqa: E402
from app.core.database import BlacklistDB                     # noqa: E402
from app.core.matcher import Matcher                          # noqa: E402
from app.capture.ocr_engine import OCREngine                     # noqa: E402

OUT_DIR = os.path.join(config.LOG_DIR, "ocr_probe")
FONT_CANDIDATES = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
                   r"C:\Windows\Fonts\arial.ttf")


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:                        # noqa: BLE001
                continue
    return ImageFont.load_default()


def render(text_lines, size, bg, fg, font_size=18, line_gap=6):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    font = load_font(font_size)
    y = 6
    for line in text_lines:
        d.text((8, y), line, font=font, fill=fg)
        y += font_size + line_gap
    return img


def main() -> int:
    print("=" * 70)
    print("真实 OCR 验证")
    print("=" * 70)

    eng = OCREngine()
    t0 = time.time()
    if not eng.warmup():
        print("✗ RapidOCR 不可用")
        return 1
    print(f"引擎加载耗时: {time.time() - t0:.2f}s")

    os.makedirs(OUT_DIR, exist_ok=True)

    cases = [
        ("英文加入提示", ["PlayerX has joined the game"], (420, 160)),
        ("中文加入提示", ["阴影猎手 回归战场"], (420, 160)),
        ("中文离开提示", ["John Doe 回归平民生活"], (420, 160)),
        ("玩家列表", ["SamplePlayer_01", "SamplePlayer_02", "RandomGuy"],
         (400, 120)),
    ]

    all_text = []
    for name, lines, size in cases:
        img = render(lines, size, (16, 18, 16), (235, 235, 225))
        path = os.path.join(OUT_DIR, f"{name}.png")
        img.save(path)

        # 预热一次，再测 5 次取稳态耗时
        eng.recognize_raw(img)
        times = []
        results = []
        for _ in range(5):
            t = time.time()
            results = eng.recognize_raw(img)
            times.append((time.time() - t) * 1000)
        text = " ".join(txt for txt, _ in results)
        all_text.append((name, text))
        print(f"\n[{name}] {size[0]}x{size[1]}   "
              f"OCR 耗时 min={min(times):.0f}ms avg={sum(times)/len(times):.0f}ms")
        print(f"  期望: {lines}")
        for txt, conf in results:
            print(f"  识别: {txt!r}  conf={conf:.3f}")
        if not results:
            print("  识别: （无）")

    # ---- 匹配链路 ----
    print("\n" + "-" * 70)
    print("匹配链路验证（match_text，阈值 %d）" % config.MATCH_THRESHOLD)
    db = BlacklistDB()
    added = []
    for pid, nm in (("probe1", "PlayerX"), ("probe2", "阴影猎手"),
                    ("probe3", "John Doe"), ("probe4", "SamplePlayer_01")):
        try:
            added.append(db.add(pid, nm))
        except ValueError:
            pass
    matcher = Matcher(db)
    print("（命中结果按条目去重；若同一名字存在多条记录，会各命中一次）")
    for name, text in all_text:
        hits = matcher.match_text(text)
        pretty = [(h[0]["player_name"], round(h[1], 1), h[2]) for h in hits]
        print(f"  {name:12s} → {pretty}")

    print("\n列表匹配（check）验证 —— OCR 把名字拆成两行的情况：")
    for names in (["SamplePlayer", "01"], ["John", "Doe"],
                  ["Random", "Guy", "Here"]):
        hits = matcher.check(names)
        pretty = [(h[0]["player_name"], round(h[1], 1)) for h in hits]
        print(f"  {str(names):32s} → {pretty}")

    # ---- 真实引擎的「同行碎片合并」验证 ----
    print("\n真实引擎 recognize_lines（按 OCR 框把同一行的碎片拼回）：")
    for name, lines, size in cases:
        img = render(lines, size, (16, 18, 16), (235, 235, 225))
        raw = eng.recognize_boxes(img)
        merged = eng.recognize_lines(img)
        shown = []
        for text, _conf, box in raw:
            if box:
                shown.append((text, tuple(int(round(v)) for v in box)))
            else:
                shown.append(text)
        print(f"  [{name}]")
        print(f"    原始框: {shown}")
        print(f"    合并后: {merged}")
    for eid in added:
        db.delete(eid)
    db.close()

    print(f"\n调试图已保存到: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

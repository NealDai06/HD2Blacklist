# -*- coding: utf-8 -*-
"""diagnose_ocr.py —— 拿**真实证据截图**复盘 OCR 识别效果（仅开发用）。

用途：把 data/evidence/*.jpg（真实游戏里抓到的区域图）重新喂给当前的
OCR + 名字过滤链路，逐张打印「原始碎片 → 合并后的名字 → 被丢掉的碎片」，
用来定位「ESC 菜单经常识别不出文字」到底是哪一步丢的。

用法：
    python tools/diagnose_ocr.py                 # 全部证据图
    python tools/diagnose_ocr.py 5               # 只看最近 5 张
    python tools/diagnose_ocr.py --region esc_menu
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PIL import Image                                   # noqa: E402

from app import config  # noqa: E402
from app.capture.ocr_engine import OCREngine                         # noqa: E402
from app.scanning.scan_session import group_into_lines, is_valid_player_name  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:]]
    limit = 0
    for a in list(args):
        if a.isdigit():
            limit = int(a)
            args.remove(a)
    ev = config.EVIDENCE_DIR
    files = sorted(
        (os.path.join(ev, f) for f in os.listdir(ev)
         if f.lower().endswith((".jpg", ".png"))),
        key=os.path.getmtime,
    )
    if limit:
        files = files[-limit:]
    if not files:
        print("没有证据截图可分析:", ev)
        return 1

    eng = OCREngine()
    if not eng.warmup():
        print("RapidOCR 不可用")
        return 1

    print("=" * 78)
    print(f"共 {len(files)} 张图，OCR_UPSCALE={config.OCR_UPSCALE} "
          f"CONF_MIN={config.OCR_CONFIDENCE_MIN}")
    print("=" * 78)

    kept_total = 0
    for path in files:
        img = Image.open(path)
        t0 = time.time()
        boxes = eng.recognize_boxes(img)
        dt = (time.time() - t0) * 1000
        names = group_into_lines(boxes)
        kept_total += len(names)
        print(f"\n--- {os.path.basename(path)}  {img.size[0]}x{img.size[1]} "
              f"OCR={dt:.0f}ms 碎片={len(boxes)} ---")
        for text, conf, box in boxes:
            mark = "OK" if is_valid_player_name(text) else ".."
            print(f"   {mark} {text!r:40s} conf={conf:.3f} "
                  f"box={tuple(int(v) for v in box) if box else None}")
        print(f"   → 合并后名字: {names}")

    print("\n" + "=" * 78)
    print(f"合计保留名字 {kept_total} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""ocr_engine.py —— RapidOCR 封装（全局单例 + 轻量预处理）。

- 模型只在首次使用时加载（懒加载），并提供 warmup() 供启动时后台预热
- 所有调用串行化（OCR 本身吃 CPU，并发没有收益）
- rapidocr 未安装时优雅降级：available=False，recognize_raw 返回 []
"""
from __future__ import annotations

import threading

import numpy as np
from PIL import Image, ImageOps

from app.config import (OCR_INTRA_OP_THREADS, OCR_UPSCALE, OCR_USE_ANGLE_CLS,
                    get_logger)

# 预处理后允许的最大边长（避免超大图拖慢 OCR）
_MAX_EDGE = 2560


def _bbox_of(box):
    """把各种框格式统一成 (x0, y0, x1, y1)，无法解析时返回 None。"""
    if box is None:
        return None
    try:
        pts = list(box)
        if not pts:
            return None
        # 平铺的 8 个数字 [x0,y0,x1,y1,...]
        if all(isinstance(v, (int, float)) for v in pts):
            if len(pts) < 8:
                return None
            xs = pts[0::2]
            ys = pts[1::2]
            return (min(xs), min(ys), max(xs), max(ys))
        # 4 个点 [[x,y], ...]
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:                                    # noqa: BLE001
        return None


class OCREngine:
    """RapidOCR 单例封装。"""

    def __init__(self, upscale: int = OCR_UPSCALE):
        self.upscale = max(1, int(upscale))
        self.log = get_logger("ocr")
        self._engine = None
        self._load_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._failed = False
        self.last_elapse = 0.0

    # ------------------------------------------------------------ 模型加载
    @property
    def available(self) -> bool:
        return not self._failed

    def _ensure_engine(self):
        if self._engine is not None or self._failed:
            return self._engine
        with self._load_lock:
            if self._engine is not None or self._failed:
                return self._engine
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                try:                                     # 兼容新版包名
                    from rapidocr import RapidOCR
                except ImportError as e:
                    self._failed = True
                    self.log.error(
                        "未安装 rapidocr-onnxruntime，OCR 功能不可用: %s", e
                    )
                    return None
            try:
                try:
                    self._engine = RapidOCR(
                        intra_op_num_threads=OCR_INTRA_OP_THREADS,
                        use_angle_cls=OCR_USE_ANGLE_CLS,
                    )
                except TypeError:
                    # 不同版本的构造参数不同，退回默认构造
                    self._engine = RapidOCR()
                self.log.info("RapidOCR 初始化完成")
            except Exception as e:                       # noqa: BLE001
                self._failed = True
                self.log.exception("RapidOCR 初始化失败: %s", e)
                return None
        return self._engine

    def warmup(self) -> bool:
        """在后台线程提前加载模型，避免第一次扫描时卡顿。"""
        return self._ensure_engine() is not None

    # -------------------------------------------------------------- 预处理
    def preprocess(self, img: Image.Image) -> np.ndarray:
        """放大 + 灰度 + 自动对比度，再转成 3 通道 ndarray。"""
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        scale = self.upscale
        if w <= 0 or h <= 0:
            return np.zeros((1, 1, 3), dtype=np.uint8)
        while scale > 1 and (w * scale > _MAX_EDGE or h * scale > _MAX_EDGE):
            scale -= 1
        gray = img.convert("L")
        if scale > 1:
            gray = gray.resize((w * scale, h * scale), Image.LANCZOS)
        gray = ImageOps.autocontrast(gray)
        return np.asarray(gray.convert("RGB"))

    # ---------------------------------------------------------------- 识别
    def recognize_boxes(self, img: Image.Image) -> list:
        """识别图像，返回 [(text, confidence, (x0, y0, x1, y1)), ...]。

        框坐标是在 **预处理后** 的图像坐标系里的（即已乘以放大倍数），
        仅用于判断"哪些碎片属于同一行文字"，不需要换算回原图。
        """
        if img is None:
            return []
        engine = self._ensure_engine()
        if engine is None:
            return []
        try:
            arr = self.preprocess(img)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("OCR 预处理失败: %s", e)
            return []
        try:
            with self._call_lock:
                out = engine(arr)
            return self._parse_full(out)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("OCR 识别失败: %s", e)
            return []

    def recognize_raw(self, img: Image.Image) -> list:
        """识别图像，返回 [(text, confidence), ...]。

        任何异常都被吞掉并记录日志（返回 []），保证后台扫描线程不会挂死。
        """
        return [(t, c) for t, c, _ in self.recognize_boxes(img)]

    def recognize_lines(self, img: Image.Image, allowlist=None) -> list:
        """识别并把「同一行的碎片」拼回完整名字，返回有效玩家名列表。

        OCR 经常把 "SamplePlayer_01" 拆成 "SamplePlayer" + "01" 两个框；
        单纯逐框过滤会把名字打散。这里按纵向重叠 + 横向间距把碎片并回一行，
        再交给 is_valid_player_name 过滤。

        allowlist 是「黑名单里确实存在的名字」白名单 —— 让 `?` 这种
        天生不像名字的名字也能通过过滤器（见 scan_session.is_valid_player_name）。
        """
        from app.scanning.scan_session import evaluate_boxes
        return evaluate_boxes(self.recognize_boxes(img), allowlist=allowlist)

    def _parse(self, out) -> list:
        """兼容旧接口：只返回 (text, confidence)。"""
        return [(t, c) for t, c, _ in self._parse_full(out)]

    def _parse_full(self, out) -> list:
        """兼容 rapidocr 各版本的返回结构，返回 (text, conf, bbox|None)。"""
        if out is None:
            return []
        result, elapse = (out if isinstance(out, tuple) else (out, None))
        if elapse is not None:
            try:
                self.last_elapse = float(sum(elapse)) if hasattr(elapse, "__iter__") \
                    else float(elapse)
            except Exception:                            # noqa: BLE001
                self.last_elapse = 0.0
        if result is None:
            return []

        # 新版 rapidocr 返回对象（含 .txts / .scores / .boxes）
        txts = getattr(result, "txts", None)
        if txts is not None:
            scores = getattr(result, "scores", None) or []
            boxes = getattr(result, "boxes", None)
            pairs = []
            for i, t in enumerate(txts):
                s = float(scores[i]) if i < len(scores) else 1.0
                box = None
                if boxes is not None and i < len(boxes):
                    box = _bbox_of(boxes[i])
                pairs.append((str(t), s, box))
            return pairs

        pairs = []
        for item in result:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    pairs.append((str(item[1]), float(item[2]),
                                  _bbox_of(item[0])))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    pairs.append((str(item[0]), float(item[1]), None))
            except Exception:                            # noqa: BLE001
                continue
        return pairs

    def recognize_text(self, img: Image.Image,
                       min_conf: float = 0.0) -> str:
        """识别并拼成一行文本（用空格分隔），便于分词匹配。"""
        return " ".join(t for t, c in self.recognize_raw(img) if c >= min_conf)

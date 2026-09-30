# -*- coding: utf-8 -*-
"""主题与外关（暗色配色 / 应用图标 / 托盘图标）测试。

运行： python -m unittest tests.test_theme -v
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                                    # noqa: E402
import theme                                     # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


def _tk_available() -> bool:
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:                                # noqa: BLE001
        return False


TK_OK = _tk_available()

#: 应用图标是使用者自己放的素材（data/ 不进仓库），
#: 所以「从 GitHub 克隆下来」的人不会有它 —— 相关的用例要优雅跳过，
#: 而不是一上来就一片 FAIL。
ICON_READY = (os.path.exists(config.APP_ICON_PATH)
              and os.path.exists(config.APP_ICON_ICO))


class TestIconAssets(unittest.TestCase):
    """应用图标文件本身。"""

    def test_icon_paths_configured(self):
        self.assertTrue(os.path.isabs(config.APP_ICON_PATH))
        self.assertTrue(os.path.isabs(config.APP_ICON_ICO))
        self.assertTrue(config.APP_ICON_PATH.endswith("app_icon.png"))
        self.assertTrue(config.APP_ICON_ICO.endswith("app_icon.ico"))

    @unittest.skipUnless(ICON_READY, "缺少 data/assets/app_icon.*（自定义素材）")
    def test_icon_files_exist(self):
        self.assertTrue(os.path.exists(config.APP_ICON_PATH),
                        "缺少 data/assets/app_icon.png")
        self.assertTrue(os.path.exists(config.APP_ICON_ICO),
                        "缺少 data/assets/app_icon.ico")

    @unittest.skipUnless(ICON_READY, "缺少 data/assets/app_icon.*（自定义素材）")
    def test_png_is_square_and_large_enough(self):
        from PIL import Image
        with Image.open(config.APP_ICON_PATH) as im:
            self.assertEqual(im.width, im.height)
            self.assertGreaterEqual(im.width, 256)
            self.assertEqual(im.mode, "RGBA")

    @unittest.skipUnless(ICON_READY, "缺少 data/assets/app_icon.*（自定义素材）")
    def test_ico_has_multiple_sizes(self):
        from PIL import Image
        with Image.open(config.APP_ICON_ICO) as im:
            sizes = getattr(im, "info", {}).get("sizes") or set()
        # PIL 打开 ico 时会把各尺寸列在 info["sizes"]
        self.assertTrue(sizes, "ICO 里应当包含多个尺寸")
        self.assertIn((16, 16), sizes)          # 任务栏小图标
        self.assertIn((256, 256), sizes)        # 大图标

    @unittest.skipUnless(ICON_READY, "缺少 data/assets/app_icon.*（自定义素材）")
    def test_load_icon_image_returns_square(self):
        for size in (16, 32, 64, 256):
            img = theme.load_icon_image(size)
            self.assertIsNotNone(img, f"size={size}")
            self.assertEqual(img.size, (size, size))
            self.assertEqual(img.mode, "RGBA")

    def test_load_icon_image_handles_missing_file(self):
        # theme 里是 from config import APP_ICON_PATH，所以要改 theme 的名字空间
        old = theme.APP_ICON_PATH
        theme.APP_ICON_PATH = os.path.join(TMP_ROOT, "nope.png")
        try:
            self.assertIsNone(theme.load_icon_image(32))   # 不抛异常
        finally:
            theme.APP_ICON_PATH = old


class TestPalette(unittest.TestCase):
    def test_required_keys(self):
        for key in ("bg", "panel", "field", "border", "fg", "fg_muted",
                    "accent", "danger", "ok", "flash", "row_odd", "row_sel"):
            self.assertIn(key, theme.PALETTE, key)

    def test_colors_are_hex(self):
        import re
        pat = re.compile(r"^#[0-9a-fA-F]{6}$")
        for key, value in theme.PALETTE.items():
            self.assertTrue(pat.match(value), f"{key}={value}")

    def test_dark_background(self):
        """底色要足够深，才能算"暗色主题"。"""
        bg = theme.PALETTE["bg"].lstrip("#")
        r, g, b = (int(bg[i:i + 2], 16) for i in (0, 2, 4))
        self.assertLess((r + g + b) / 3, 80)

    def test_foreground_contrasts_with_background(self):
        def lum(hexcolor):
            c = hexcolor.lstrip("#")
            vals = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            vals = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
                    for v in vals]
            return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]

        fg = lum(theme.PALETTE["fg"])
        bg = lum(theme.PALETTE["bg"])
        ratio = (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)
        self.assertGreater(ratio, 7.0, f"正文对比度偏低: {ratio:.1f}:1")


class TestTaskbarIdentity(unittest.TestCase):
    def test_set_taskbar_identity_does_not_raise(self):
        theme.set_taskbar_identity(config.APP_ID)     # 不应抛异常
        theme.set_taskbar_identity("")                # 空值也不应抛


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestThemeApplication(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass

    def test_apply_theme_returns_palette(self):
        p = theme.apply_theme(self.root)
        self.assertEqual(p["bg"], theme.PALETTE["bg"])

    def test_clam_theme_selected(self):
        """Treeview 行背景 tag（命中闪烁）只在 clam 下渲染。"""
        from tkinter import ttk
        theme.apply_theme(self.root)
        self.assertEqual(ttk.Style(self.root).theme_use(), "clam")

    def test_root_background_applied(self):
        theme.apply_theme(self.root)
        self.assertEqual(str(self.root.cget("bg")).lower(),
                         theme.PALETTE["bg"].lower())

    def test_ttk_styles_configured(self):
        from tkinter import ttk
        theme.apply_theme(self.root)
        style = ttk.Style(self.root)
        self.assertEqual(str(style.lookup("TFrame", "background")).lower(),
                         theme.PALETTE["bg"].lower())
        self.assertEqual(
            str(style.lookup("Hit.TLabel", "foreground")).lower(),
            theme.PALETTE["ok"].lower())
        self.assertEqual(
            str(style.lookup("Paused.TLabel", "foreground")).lower(),
            theme.PALETTE["accent"].lower())
        # 主操作按钮是金色实底
        self.assertEqual(
            str(style.lookup("Accent.TButton", "background")).lower(),
            theme.PALETTE["accent"].lower())

    def test_treeview_supports_row_tags(self):
        from tkinter import ttk
        theme.apply_theme(self.root)
        tv = ttk.Treeview(self.root, columns=("a",), show="headings")
        tv.tag_configure("hit", background=theme.PALETTE["flash"])
        tv.tag_configure("odd", background=theme.PALETTE["row_odd"])
        self.assertEqual(str(tv.tag_configure("hit", "background")).lower(),
                         theme.PALETTE["flash"].lower())
        tv.destroy()

    def test_make_menu_is_themed(self):
        theme.apply_theme(self.root)
        m = theme.make_menu(self.root)
        try:
            self.assertEqual(str(m.cget("bg")).lower(),
                             theme.PALETTE["panel"].lower())
            self.assertEqual(str(m.cget("fg")).lower(),
                             theme.PALETTE["fg"].lower())
            self.assertEqual(m.cget("tearoff"), 0)
        finally:
            m.destroy()

    def test_style_text_applies_colors(self):
        import tkinter as tk
        theme.apply_theme(self.root)
        t = tk.Text(self.root)
        theme.style_text(t)
        try:
            self.assertEqual(str(t.cget("bg")).lower(),
                             theme.PALETTE["field"].lower())
            self.assertEqual(str(t.cget("fg")).lower(),
                             theme.PALETTE["fg"].lower())
        finally:
            t.destroy()

    def test_style_text_mono(self):
        import tkinter as tk
        theme.apply_theme(self.root)
        t = tk.Text(self.root)
        theme.style_text(t, mono=True)
        try:
            self.assertIn("Consolas", str(t.cget("font")))
        finally:
            t.destroy()

    @unittest.skipUnless(ICON_READY, "缺少 data/assets/app_icon.*（自定义素材）")
    def test_apply_window_icon_sets_icon(self):
        theme.apply_theme(self.root)
        theme.apply_window_icon(self.root)
        self.root.update_idletasks()
        # iconphoto 之后 Tk 会记住图标（wm iconphoto 查询不到，改验证引用被持有）
        self.assertTrue(theme._icon_refs, "PhotoImage 必须被持有，否则会被 GC")

    def test_apply_window_icon_survives_missing_file(self):
        old_png, old_ico = theme.APP_ICON_PATH, theme.APP_ICON_ICO
        theme.APP_ICON_PATH = os.path.join(TMP_ROOT, "x.png")
        theme.APP_ICON_ICO = os.path.join(TMP_ROOT, "x.ico")
        try:
            theme.apply_window_icon(self.root)       # 不应抛异常
        finally:
            theme.APP_ICON_PATH = old_png
            theme.APP_ICON_ICO = old_ico


if __name__ == "__main__":
    os.makedirs(TMP_ROOT, exist_ok=True)
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

# -*- coding: utf-8 -*-
"""data/assets 资源导入测试：默认目录 / 复制一份 / 去重 / 改名 / 上限。

对应需求：
- 选自定义文件时对话框默认停在 data/assets
- 选完图片（音频）自动复制一份进 data/assets，方便下次直接选
- 音频格式放宽（WAV / MP3 / OGG / FLAC）

运行： python -m unittest tests.test_assets -v
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.core import assets as astore                        # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


class AssetCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = os.path.join(TMP_ROOT, "assets_" + self._testMethodName)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.src_dir = os.path.join(self.tmp, "src")
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.src_dir)
        self._p = mock.patch.object(astore, "ASSETS_DIR", self.assets)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name, data=b"data", where=None):
        p = os.path.join(where or self.src_dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def listing(self):
        return sorted(os.listdir(self.assets)) if os.path.isdir(self.assets) else []


class TestAssetsDir(AssetCase):
    def test_creates_and_returns_dir(self):
        self.assertFalse(os.path.isdir(self.assets))
        got = astore.assets_dir()
        self.assertEqual(got, self.assets)
        self.assertTrue(os.path.isdir(got))

    def test_filetypes_are_wellformed(self):
        for group in (astore.IMAGE_FILETYPES, astore.AUDIO_FILETYPES):
            self.assertTrue(group)
            for item in group:
                self.assertEqual(len(item), 2)
                label, pattern = item
                self.assertTrue(label and isinstance(pattern, str) and pattern)

    def test_audio_includes_mp3_and_more(self):
        pattern = astore.AUDIO_FILETYPES[0][1]
        for ext in (".wav", ".mp3", ".ogg", ".flac"):
            self.assertIn("*" + ext, pattern)
            self.assertIn(ext, astore.AUDIO_EXTS)

    def test_hints_mention_copy_and_formats(self):
        self.assertIn("data/assets", astore.IMAGE_HINT)
        self.assertIn("data/assets", astore.AUDIO_HINT)
        self.assertIn("MP3", astore.AUDIO_HINT)


class TestImport(AssetCase):
    def test_copies_into_assets(self):
        src = self.make("alert.mp3", b"ID3fake")
        got = astore.import_asset(src)
        self.assertEqual(got, os.path.join(self.assets, "alert.mp3"))
        self.assertEqual(self.listing(), ["alert.mp3"])
        with open(got, "rb") as f:
            self.assertEqual(f.read(), b"ID3fake")
        # 原文件仍在（是复制，不是移动）
        self.assertTrue(os.path.isfile(src))

    def test_defaults_to_data_assets_when_dir_missing(self):
        src = self.make("a.png", b"x")
        self.assertFalse(os.path.isdir(self.assets))
        got = astore.import_asset(src)                    # 目录不存在也能自动建
        self.assertEqual(os.path.dirname(got), self.assets)

    def test_unicode_filename(self):
        src = self.make("预警 音效.mp3", b"u")
        got = astore.import_asset(src)
        self.assertEqual(os.path.basename(got), "预警 音效.mp3")
        self.assertTrue(os.path.isfile(got))

    def test_second_identical_pick_reuses_existing(self):
        src = self.make("a.png", b"same")
        first = astore.import_asset(src)
        second = astore.import_asset(src)
        self.assertEqual(first, second)
        self.assertEqual(self.listing(), ["a.png"])

    def test_same_name_different_content_gets_suffix(self):
        a = astore.import_asset(self.make("a.png", b"one"))
        other = os.path.join(self.tmp, "src2")
        os.makedirs(other, exist_ok=True)
        b = astore.import_asset(self.make("a.png", b"two", where=other))
        self.assertEqual(os.path.basename(a), "a.png")
        self.assertEqual(os.path.basename(b), "a (2).png")
        self.assertEqual(self.listing(), ["a (2).png", "a.png"])
        with open(b, "rb") as f:
            self.assertEqual(f.read(), b"two")

    def test_file_already_in_assets_is_not_copied(self):
        os.makedirs(self.assets, exist_ok=True)
        src = self.make("inside.png", b"z", where=self.assets)
        got = astore.import_asset(src)
        self.assertEqual(got, os.path.abspath(src))
        self.assertEqual(self.listing(), ["inside.png"])

    def test_missing_file_raises(self):
        with self.assertRaises(astore.AssetError):
            astore.import_asset(os.path.join(self.src_dir, "nope.png"))
        with self.assertRaises(astore.AssetError):
            astore.import_asset("")

    def test_directory_raises(self):
        with self.assertRaises(astore.AssetError):
            astore.import_asset(self.src_dir)

    def test_oversized_raises(self):
        src = self.make("big.mp3", b"x" * 100)
        with mock.patch.object(astore, "MAX_ASSET_BYTES", 10):
            with self.assertRaises(astore.AssetError) as ctx:
                astore.import_asset(src)
        self.assertIn("过大", str(ctx.exception))
        self.assertEqual(self.listing(), [])              # 什么都没写进去

    def test_unknown_extension_still_copied(self):
        """放宽限制：对话框里选「所有文件」时不拦。"""
        src = self.make("weird.tiff", b"t")
        got = astore.import_asset(src)
        self.assertTrue(os.path.isfile(got))


class TestHelpers(AssetCase):
    def test_is_managed(self):
        os.makedirs(self.assets, exist_ok=True)
        inside = self.make("i.png", where=self.assets)
        sub = os.path.join(self.assets, "sub")
        nested = self.make("n.png", where=sub)
        outside = self.make("o.png")
        self.assertTrue(astore.is_managed(inside))
        self.assertTrue(astore.is_managed(nested))
        self.assertFalse(astore.is_managed(outside))
        self.assertFalse(astore.is_managed(""))

    def test_unique_path(self):
        os.makedirs(self.assets, exist_ok=True)
        self.make("f.png", where=self.assets)
        got = astore.unique_path(self.assets, "f.png")
        self.assertEqual(os.path.basename(got), "f (2).png")
        self.make("f (2).png", where=self.assets)
        self.assertEqual(os.path.basename(astore.unique_path(self.assets, "f.png")),
                         "f (3).png")
        self.assertEqual(os.path.basename(astore.unique_path(self.assets, "new.png")),
                         "new.png")


if __name__ == "__main__":
    unittest.main()

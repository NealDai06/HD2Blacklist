# -*- coding: utf-8 -*-
"""assets.py —— data/assets 资源目录：选图/选音频的统一落地点。

为什么要有这一层：

1. **默认目录**。用户点 [浏览…] 时，文件对话框默认停在 `data/assets`，
   而不是上次去过的某个乱七八糟的目录。
2. **复制一份**。用户从桌面/U盘选中的图片、音频会被复制进 `data/assets`，
   下次直接在默认目录里就能选到；原文件被移动、删除、U盘拔掉，通知也不会
   变成空白。
3. **格式白名单**。图片与音频对话框的过滤器、以及「支持哪些格式」的提示
   文案都从这里取，避免 UI 与播放器两边写法漂移。

本模块只依赖 config，不做任何 GUI 操作，便于单测。
"""
from __future__ import annotations

import filecmp
import os
import shutil

from app.config import ASSETS_DIR

# --------------------------------------------------------------------------
# 文件对话框过滤器（顺序 = 对话框里的显示顺序）
# --------------------------------------------------------------------------
IMAGE_FILETYPES = (
    ("图片", "*.png *.jpg *.jpeg *.bmp *.gif"),
    ("所有文件", "*.*"),
)
AUDIO_FILETYPES = (
    ("音频", "*.wav *.mp3 *.ogg *.flac"),
    ("所有文件", "*.*"),
)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
#: 播放走 pygame.mixer（SDL2_mixer），以下格式在打包后的 exe 里同样可用
AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".flac")

IMAGE_HINT = "支持 PNG / JPG / BMP / GIF，选中后自动复制一份到 data/assets。"
AUDIO_HINT = ("支持 WAV / MP3 / OGG / FLAC，选中后自动复制一份到 data/assets；"
              "WAV 建议 16bit PCM 44.1kHz。")

#: 单个资源文件复制上限：防止误选超大文件（视频/无损整轨）把界面卡住
MAX_ASSET_BYTES = 64 * 1024 * 1024


class AssetError(RuntimeError):
    """资源导入失败（文件不存在 / 过大 / 无权限 / 磁盘错误）。"""


def assets_dir() -> str:
    """返回 `data/assets` 绝对路径，并确保目录存在。

    直接把它当 filedialog 的 initialdir 用即可。
    """
    os.makedirs(ASSETS_DIR, exist_ok=True)
    return ASSETS_DIR


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(child: str, parent: str) -> bool:
    """child 是否就在 parent 目录里（含子目录）。"""
    child, parent = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:                       # 不同盘符
        return False


def is_managed(path: str) -> bool:
    """该文件是否已经在 data/assets 里（是的话不必再复制一份）。"""
    if not path:
        return False
    return _inside(path, assets_dir())


def unique_path(directory: str, filename: str) -> str:
    """`foo.png` 已被占用时，依次尝试 `foo (2).png`、`foo (3).png`…"""
    stem, ext = os.path.splitext(filename)
    target = os.path.join(directory, filename)
    n = 2
    while os.path.exists(target):
        if n > 999:
            raise AssetError(f"同名文件过多：{filename}")
        target = os.path.join(directory, f"{stem} ({n}){ext}")
        n += 1
    return target


def import_asset(path: str, target_dir: str | None = None) -> str:
    """把选中的文件复制进 data/assets，返回**最终应该写进配置的路径**。

    - 文件本来就在 data/assets 里 → 原样返回，不复制
    - data/assets 里已有**内容完全相同**的同名文件 → 直接复用，不复制
    - 同名但内容不同 → 改名 `xxx (2).ext` 存成新的一份
    """
    if not path:
        raise AssetError("没有选择文件")
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise AssetError(f"文件不存在或不是普通文件：{path}")

    directory = target_dir or assets_dir()
    os.makedirs(directory, exist_ok=True)

    if _inside(src, directory):              # 已经在这个目录里了
        return src

    try:
        size = os.path.getsize(src)
    except OSError as e:
        raise AssetError(f"无法读取文件：{e}") from e
    if size > MAX_ASSET_BYTES:
        raise AssetError(f"文件过大（{size / 1048576:.1f} MB），"
                         f"上限 {MAX_ASSET_BYTES // 1048576} MB")

    target = os.path.join(directory, os.path.basename(src))
    if os.path.exists(target):
        try:
            if filecmp.cmp(src, target, shallow=False):
                return os.path.abspath(target)      # 同一份，已导入过
        except OSError:
            pass
        target = unique_path(directory, os.path.basename(src))

    try:
        shutil.copy2(src, target)
    except OSError as e:
        raise AssetError(f"复制失败：{e}") from e
    return os.path.abspath(target)

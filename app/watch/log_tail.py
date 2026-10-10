# -*- coding: utf-8 -*-
"""log_tail.py —— 按字节偏移尾随一个「只追加」的文本文件。

插件 HD2Tracker 写 playerLog.txt 的方式是 `io.open(path, "a")` + 每行 flush，
也就是说这个文件**只会变长**，于是"读到哪儿了"这个偏移量就是全部需要的状态。

三条硬要求（都是真会遇到的）：
    * **末尾的半行不消费**：插件正在写一行的时候我们去读，只能读到半截。
      偏移必须停在最后一个换行符之后，等下一轮再读那半行 —— 否则那行 JSON
      会被腰斩，解析失败，而且**永远不会被补回来**。
    * **截断 / 轮转要能自愈**：文件长度比偏移还小（用户删了重建、被别的工具
      截断）→ 偏移归零重读，不能死在那里假装"没有新内容"。
      （注意这里的能力边界：如果替换上来的新文件**不比旧偏移小**，光看长度
      是分不出来的 —— 插件只会追加，所以这个角落不存在；真要处理得靠内容指纹，
      不值当。）
    * **坏字节不许拖垮整轮**：按字节读、按 b"\\n" 切，再用 errors="replace"
      解码。某一行里有一个坏字节，只影响那一行，后面的行照常出来。
"""
from __future__ import annotations

import os

from app.config import WATCH_BACKFILL_BYTES, WATCH_TAIL_MAX_LINES, get_logger


class LogTail:
    """一个文件的字节偏移尾随器。线程不安全 —— 由 watcher 单线程使用。"""

    #: 单次最多读多少字节。文件如果突然涨了几百 MB（比如用户往日志里
    #: 粘贴了东西），也不至于把内存吃光；剩下的一轮一轮接着读。
    MAX_CHUNK = 4 * 1024 * 1024

    def __init__(self, path: str):
        self.path = path or ""
        self.offset = 0            # 绝对偏移：从这里往后都还没被消化
        self.read_bytes = 0        # 统计：累计读了多少字节
        self.skipped_lines = 0     # 统计：因为超出 max_lines 被丢掉的行数

    # ------------------------------------------------------------------ 状态
    def size(self) -> int:
        """文件字节数；不存在或读不到返回 -1。"""
        if not self.path:
            return -1
        try:
            return os.path.getsize(self.path)
        except OSError:
            return -1

    @property
    def exists(self) -> bool:
        return self.size() >= 0

    def reset(self) -> None:
        """把偏移归零（下次从文件头开始读）。"""
        self.offset = 0

    # ------------------------------------------------------------------ 读取
    def start_at_tail(self, max_lines: int = None,
                      back_bytes: int = None) -> list:
        """首启：偏移直接顶到文件末尾，并返回末尾若干行。

        为什么要这几行：应用可能是游戏已经在跑的时候才打开的，那"现在队里有谁"
        就只在文件末尾那几行里。给这几行是为了让界面**立刻**显示当前队伍。

        为什么不从头重放整个文件：应用自己的数据库里已经存着"遇到过谁"，
        重放一个几十万行的历史没有任何收益，只会白等几秒。

        返回的行由调用方按 `live=False` 处理 —— **不许据此弹告警**，
        否则打开应用的一瞬间会因为历史里的 join 事件炸出一堆通知。
        """
        if not self.path:
            return []
        back_bytes = (WATCH_BACKFILL_BYTES if back_bytes is None
                      else max(0, int(back_bytes)))
        size = self.size()
        if size <= 0:
            self.offset = 0
            return []
        start = max(0, size - back_bytes)
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                data = f.read()
        except OSError as e:
            get_logger("watch").warning("读取日志失败（首启）：%s", e)
            self.offset = 0
            return []
        self.offset = size
        self.read_bytes += len(data)
        lines = self._split(data.decode("utf-8", errors="replace"))
        if start > 0 and lines:
            lines = lines[1:]              # 第一行大概率是从中间截断的
        return self._cap(lines, max_lines)

    def poll(self, max_lines: int = None) -> list:
        """读一批新出现的**完整**行；没有新内容返回 []。"""
        if not self.path:
            return []
        size = self.size()
        if size < 0:
            return []                      # 文件还没出现：正常，等插件写
        if size < self.offset:
            get_logger("watch").info(
                "日志文件变小了（%d → %d），从头重读", self.offset, size)
            self.reset()
        if size == self.offset:
            return []

        want = min(size - self.offset, self.MAX_CHUNK)
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(want)
        except OSError as e:
            get_logger("watch").warning("读取日志失败：%s", e)
            return []
        if not data:
            return []
        self.read_bytes += len(data)

        cut = data.rfind(b"\n")
        if cut < 0:
            # 整段都是没写完的半行 —— 一个字都不消费，等下一次
            if len(data) >= self.MAX_CHUNK:
                # 极端情况：单行超过 4MB。不可能是插件写的（一行几百字节），
                # 多半是文件被写坏了；跳过这段，否则偏移会永远卡在这里。
                get_logger("watch").warning(
                    "单行超过 %d 字节，跳过这段数据", self.MAX_CHUNK)
                self.offset += len(data)
            return []

        complete = data[:cut + 1]
        self.offset += len(complete)
        text = complete.decode("utf-8", errors="replace")
        return self._cap(self._split(text), max_lines)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _split(text: str) -> list:
        return [ln for ln in text.split("\n") if ln.strip()]

    def _cap(self, lines: list, max_lines) -> list:
        """一次给太多行时只留最新的那些（并记账）。"""
        limit = WATCH_TAIL_MAX_LINES if max_lines is None else int(max_lines)
        if limit and len(lines) > limit:
            self.skipped_lines += len(lines) - limit
            return lines[-limit:]
        return lines

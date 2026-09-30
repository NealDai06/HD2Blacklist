# -*- coding: utf-8 -*-
"""main.py —— HD2 黑名单系统入口。

    python main.py                     正常启动（GUI + 后台监控）
    python main.py --check             环境自检（不开 GUI，逐项打印结果）
    python main.py --capture-debug     抓取各监视区域并输出调试截图
    python main.py --preview-notification  离线渲染一张通知预览图
    python main.py --debug             打开 DEBUG 日志

触发源（最终版）：
    1. 聊天框**按需扫描**（点 [扫描聊天框] 按钮，或可选 F8 热键）
       —— 没有循环、没有定时器；不点就完全不产生开销
    2. ESC 菜单扫描（按下 ESC 且菜单已打开）
    3. 冷启动扫描（游戏启动后 12 秒一次）

已删除：聊天框 1 秒 tick / 10 秒定期扫描 / 触发词系统 /
        2 分钟兜底扫描 / F9 快捷键扫描。

硬性约束：不注入、不读内存、不改包、不抢焦点、不打断游戏。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback

import config
from config import (COLD_START_DELAY, COLD_START_SESSION, get_logger)


# ==========================================================================
class HD2BlacklistApp:
    """把各个模块组装起来。"""

    def __init__(self, gui_enabled: bool = True):
        config.ensure_dirs()


        # ---- 首次启动生成示例配置 ----
        from hotkey_config import HotkeyConfig, ensure_hotkey_file
        from notification_config import NotificationConfig, ensure_notification_file
        ensure_notification_file()
        ensure_hotkey_file()

        from database import BlacklistDB
        from gui import BlacklistGUI
        from matcher import Matcher
        from notifier import Notifier, ensure_default_icon
        from notification_config import NotificationConfig
        from ocr_engine import OCREngine
        from region_config import RegionConfig
        from scan_scheduler import ScanScheduler
        from screen_capture import ScreenCapture

        self.log = get_logger("app")
        self._stop = threading.Event()
        self._gui_enabled = gui_enabled
        # 线程登记表：必须在**任何** _spawn() 之前建好，
        # 否则一旦热键在构造阶段就启动，_spawn 会抛
        # AttributeError: 'HD2BlacklistApp' object has no attribute '_threads'
        self._threads = []
        self._cold_start_thread = None

        self.db = BlacklistDB()
        self.notification_cfg = NotificationConfig()
        self.region_cfg = RegionConfig()
        self.hotkey_cfg = HotkeyConfig()

        self.matcher = Matcher(self.db)
        self.ocr = OCREngine()
        self.capture = ScreenCapture(self.region_cfg)
        ensure_default_icon()

        self.notifier = Notifier(self.notification_cfg)

        # 聊天框：按需扫描（无循环、无定时器）。scheduler 稍后注入。
        from chat_scanner import ChatScanner
        self.chat_scanner = ChatScanner(None, self.capture, self.ocr)

        self.gui = BlacklistGUI(
            db=self.db,
            notification_config=self.notification_cfg,
            region_config=self.region_cfg,
            matcher=self.matcher,
            capture=self.capture,
            notifier=self.notifier,
            chat_scanner=self.chat_scanner,
            hotkey_config=self.hotkey_cfg,
            on_quit=self.shutdown,
            on_pause=self.on_pause,
            on_chat_hotkey=self.apply_hotkey_config,
        ) if gui_enabled else None

        self.scheduler = ScanScheduler(
            db=self.db,
            matcher=self.matcher,
            notifier=self.notifier,
            ocr=self.ocr,
            capture=self.capture,
            region_config=self.region_cfg,
            on_encounter=(self.gui.enqueue_encounter_update
                          if self.gui else None),
        )
        # ChatScanner 需要 scheduler 才能匹配 / 计数 / 弹提示
        self.chat_scanner.scheduler = self.scheduler

        if self.gui is not None:
            self.gui.scheduler = self.scheduler
            self.gui.app = self

        from chat_hotkey import ChatScanHotkey
        from esc_trigger import EscTrigger
        from process_watcher import ProcessWatcher

        # 聊天框扫描热键（默认关闭；绑定由 data/hotkey.json 决定）
        self.chat_hotkey = None
        if self.hotkey_cfg.enabled:
            self._start_hotkey()

        self.esc_trigger = EscTrigger(
            self.scheduler, self.capture, self.ocr,
            game_active=self.scheduler.game_active,
            active_check=self.scheduler.is_active,
        )
        self.proc_watcher = ProcessWatcher(
            on_start=self._on_game_start,
            on_stop=self._on_game_stop,
        )

        # 区域框得太小 = 永远识别不到东西。这种情况必须主动说出来，
        # 否则用户只会觉得"扫描没反应"（真实案例：HUD 区域被框成 10x13 像素）。
        for warning in self.region_cfg.warnings():
            self.log.warning("[区域] %s", warning)

    # ------------------------------------------------------------ 游戏事件
    def _on_game_start(self):
        """由 WMI 监听线程调用 —— 绝不能在这里碰 tkinter。"""
        self.scheduler.set_game_active(True)
        self._gui_call("reset_session_stats")
        self._gui_call("set_monitoring", True)
        self._gui_call("set_status", "检测到游戏进程，监视已激活")

        if self._cold_start_thread and self._cold_start_thread.is_alive():
            return
        self._cold_start_thread = threading.Thread(
            target=self._cold_start_scan, daemon=True, name="ColdStart")
        self._cold_start_thread.start()

    def _on_game_stop(self):
        self.scheduler.set_game_active(False)
        self._gui_call("set_monitoring", False, "游戏未运行")
        self._gui_call("set_status", "游戏已退出，监视已暂停")

    def _cold_start_scan(self):
        """冷启动扫描：游戏启动后 12 秒扫一次 HUD 玩家列表。"""
        self.log.info("冷启动扫描将在 %.0f 秒后开始", COLD_START_DELAY)
        if self._stop.wait(COLD_START_DELAY):
            return
        if not self.scheduler.is_active():
            self.log.info("冷启动扫描取消（游戏已退出或监控已暂停）")
            return
        self.scheduler.start_session("player_list_hud", "cold_start",
                                     dict(COLD_START_SESSION))

    # ---------------------------------------------------------------- 暂停
    def on_pause(self, paused: bool):
        """暂停 / 恢复后台监控。

        聊天框已改成按需扫描，没有循环需要唤醒，这里只需要改调度器状态。
        """
        self.scheduler.set_paused(paused)

    # ------------------------------------------------------------ 聊天框热键
    def _start_hotkey(self, wait_ready: bool = True):
        """按当前配置启动热键（系统全局热键，注册不了自动退回轮询）。"""
        from chat_hotkey import ChatScanHotkey
        self.chat_hotkey = ChatScanHotkey(
            self.chat_scanner,
            hotkey=self.hotkey_cfg.binding(),
            active_check=None,          # 手动扫描不要求游戏在跑
        )
        self._spawn(self.chat_hotkey.start, "ChatScanHotkey")
        if wait_ready:
            self.chat_hotkey.wait_ready(timeout=1.5)
        self.log.info("聊天框扫描热键：%s", self.chat_hotkey.status_text())

    def apply_hotkey_config(self, cfg: dict) -> str:
        """GUI 改了快捷键 / 开关后调用：重启或停掉热键。

        返回一句状态说明，GUI 会直接显示在状态栏里。
        """
        if self.chat_hotkey is not None:
            self.chat_hotkey.stop()
            self.chat_hotkey = None
        if not cfg.get("enabled"):
            self.log.info("聊天框扫描热键已停用")
            return "扫描热键已停用"
        self._start_hotkey()
        if self.chat_hotkey is None:
            return "扫描热键启动失败"
        return self.chat_hotkey.status_text()

    # ---------------------------------------------------------------- 工具
    def _gui_call(self, method: str, *args, **kwargs):
        """把 GUI 调用排到主线程（后台线程只能这么做）。"""
        if self.gui is None:
            return
        func = getattr(self.gui, method, None)
        if func is None:
            return
        self.gui.post(lambda: func(*args, **kwargs))

    # ---------------------------------------------------------------- 启动
    def start(self):
        self.log.info("=" * 60)
        self.log.info("%s v%s 启动", config.APP_NAME, config.VERSION)
        self.log.info("数据目录: %s", config.DATA_DIR)

        # OCR 模型预热放到后台，避免第一次扫描卡顿
        self._spawn(self._warmup_ocr, "OCRWarmup")
        # 进程监控（WMI 事件订阅，阻塞）→ 守护线程
        self._spawn(self.proc_watcher.start, "ProcessWatcher")
        # ESC 按键轮询
        self._spawn(self.esc_trigger.start, "EscTrigger")
        # 聊天框 **不再有** 常驻循环：只在用户点按钮/按热键时才扫描
        if self.chat_hotkey is not None:
            self.log.info("%s", self.chat_hotkey.status_text())
        else:
            self.log.info("聊天框为按需扫描：点 [扫描聊天框] 按钮或按 %s",
                          self.hotkey_cfg.display)

        if self.gui is not None:
            self.gui.set_monitoring(False, "等待游戏启动")
            self.gui.set_status("就绪：等待《绝地潜兵2》启动")
            self.gui.run()
        else:
            self._stop.wait()

    def _spawn(self, target, name: str):
        t = threading.Thread(target=self._guarded, args=(target, name),
                             daemon=True, name=name)
        t.start()
        # 兜底：即使将来有人在构造顺序上动手脚，也不再抛 AttributeError
        threads = getattr(self, "_threads", None)
        if threads is None:
            threads = self._threads = []
        threads.append(t)
        return t

    def _guarded(self, target, name: str):
        try:
            target()
        except Exception:                                # noqa: BLE001
            self.log.error("线程 %s 异常退出:\n%s", name, traceback.format_exc())

    def _warmup_ocr(self):
        ok = self.ocr.warmup()
        if ok:
            self.log.info("OCR 引擎预热完成")
        else:
            self.log.warning("OCR 引擎不可用（未安装 rapidocr？），"
                             "匹配功能仍可手工测试")

    # ---------------------------------------------------------------- 关闭
    def shutdown(self):
        if self._stop.is_set():
            return
        self.log.info("正在退出…")
        self._stop.set()
        try:
            if self.chat_hotkey is not None:
                self.chat_hotkey.stop()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.esc_trigger.stop()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.proc_watcher.stop()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.scheduler.shutdown()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.notifier.close()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.capture.close()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.db.close()
        except Exception:                                # noqa: BLE001
            pass
        self.log.info("已退出")


# ==========================================================================
def run_self_check() -> int:
    """环境自检：不开 GUI，逐项打印。返回退出码（0=通过）。"""
    print("=" * 66)
    print(f"{config.APP_NAME} v{config.VERSION} —— 环境自检")
    print("=" * 66)
    problems = []

    def check(label, fn):
        try:
            result = fn()
            if result is True or result is None:
                print(f"[ OK ] {label}")
                return True
            print(f"[ OK ] {label}: {result}")
            return True
        except Exception as e:                           # noqa: BLE001
            print(f"[FAIL] {label}: {e}")
            problems.append(label)
            return False

    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"程序目录    : {config.BASE_DIR}")
    print(f"数据目录    : {config.DATA_DIR}")
    print("-" * 66)

    check("创建数据目录", config.ensure_dirs)
    check("DPI 感知", config.enable_dpi_awareness)

    def _optional(name):
        def inner():
            __import__(name)
            return True
        return inner

    mods = [("PIL", "Pillow"), ("numpy", "numpy"), ("mss", "mss"),
            ("rapidfuzz", "rapidfuzz"), ("wmi", "wmi"), ("win32api", "pywin32"),
            ("pystray", "pystray"), ("winotify", "winotify"),
            ("pygame", "pygame"), ("tkinter", "tkinter")]
    missing = []
    for mod, label in mods:
        try:
            __import__(mod)
            print(f"[ OK ] 依赖 {label}")
        except Exception as e:                           # noqa: BLE001
            print(f"[WARN] 依赖 {label} 缺失: {e}")
            missing.append(label)

    print("-" * 66)

    from notification_config import NotificationConfig, ensure_notification_file
    from region_config import RegionConfig
    from database import BlacklistDB
    from matcher import Matcher

    check("生成 notification.json", ensure_notification_file)
    check("区域配置读取", lambda: f"{len(RegionConfig().get_all())} 个区域")
    check("通知配置读取", lambda: NotificationConfig().get()["image_mode"])

    def _chat_scanner_check():
        from chat_scanner import ChatScanner
        ChatScanner(None, None, None)          # 构造即可用，无循环无定时器
        return "按需扫描就绪（无循环、无定时器）"

    check("聊天框按需扫描", _chat_scanner_check)

    def _hotkey_check():
        from hotkey_config import HotkeyConfig, ensure_hotkey_file
        ensure_hotkey_file()
        cfg = HotkeyConfig()
        data = cfg.get()
        state = "已启用" if data["enabled"] else "已关闭"
        # 试一下按键名解析往返
        from hotkey_config import combo_name, keysym_to_vk, vk_to_name
        vk = keysym_to_vk("F8")
        if vk is None or "F8" not in combo_name(vk):
            raise RuntimeError("keysym→VK 映射异常")
        return f"{cfg.display}（{state}），可自定义并持久化"

    check("聊天框扫描快捷键", _hotkey_check)

    def _priority_check():
        """优先级 + 扫描节流：这两组开关就是「触发时游戏掉帧」的解药。"""
        import priority as pri
        mode = ("开 → 启动时压到 " + config.PROCESS_PRIORITY
                if config.GAME_FRIENDLY_PRIORITY else "关（用系统默认）")
        return f"游戏友好模式{mode}；当前进程={pri.process_priority_name()}"

    check("游戏友好优先级", _priority_check)
    check("扫描节流",
          lambda: (f"抓屏间隔 {config.ESC_SESSION['interval']}s；"
                   f"静止跳过OCR={config.SESSION_SKIP_UNCHANGED}；"
                   f"静止 {config.SESSION_MAX_STATIC_FRAMES} 帧收工"))

    def _hotkey_register_check():
        """真机探测：临时注册一次全局热键再立刻注销。

        这是「游戏里按 F8 有没有反应」的关键——轮询式按键读取在游戏
        前台时会失效，系统热键（RegisterHotKey）不会。
        """
        from chat_hotkey import ChatScanHotkey
        from hotkey_config import HotkeyConfig as _HC

        class _Noop:
            def scan_now(self, source=None):
                return {"status": "ok"}

        hk = ChatScanHotkey(_Noop(), hotkey=_HC().binding())
        try:
            hk.start()
            hk.wait_ready(timeout=3.0)
            if hk.mode != ChatScanHotkey.MODE_REGISTER:
                raise RuntimeError(hk.last_error or "无法注册系统热键")
            return hk.status_text()
        finally:
            hk.stop(wait=True, timeout=2)

    try:
        print(f"[ OK ] 系统全局热键: {_hotkey_register_check()}")
    except Exception as e:                               # noqa: BLE001
        print(f"[WARN] 系统全局热键不可用: {e}（将退回按键轮询模式，"
              "游戏内可能无效）")

    def _db_check():
        db = BlacklistDB()
        try:
            n = db.get_count()
            db.record_encounter
            mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
            return f"{n} 条记录, journal_mode={mode}"
        finally:
            db.close()

    check("数据库 / WAL 模式", _db_check)
    check("匹配器", lambda: f"{Matcher(BlacklistDB()).reload()} 条索引")

    def _io_check():
        """导入导出接口自检：导出 1 条再原样导入（skip），确认不报错。"""
        import tempfile
        from database import BlacklistDB as DB
        tmp = os.path.join(config.LOG_DIR, "_io_check.db")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        db = DB(tmp)
        try:
            db.add("selftest", "自检条目", "导入导出自检", 1)
            rows = db.export_all()
            if len(rows) != 1:
                raise RuntimeError(f"导出条数异常: {len(rows)}")
            res = db.import_entries(rows, strategy="skip")
            if res["skipped"] != 1:
                raise RuntimeError(f"重复导入应全部跳过，实际 {res}")
            res2 = db.import_entries(
                [{"player_id": "new", "player_name": "新增",
                  "note": "x", "tk_count": "3"}], strategy="skip")
            if res2["inserted"] != 1:
                raise RuntimeError(f"新条目应被插入，实际 {res2}")
            res3 = db.import_entries([{"player_id": "", "note": "no id"}])
            if res3["skipped"] != 1:
                raise RuntimeError("空 player_id 应被跳过")
            return "export_all / import_entries 就绪"
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    check("名单导入导出", _io_check)

    def _matcher_check():
        db = BlacklistDB()
        try:
            m = Matcher(db)
            hits = m.match_text("PlayerX has joined the game")
            return f"match_text 可用（当前黑名单命中 {len(hits)} 条）"
        finally:
            db.close()

    check("match_text 接口", _matcher_check)

    def _symbol_check():
        """全符号玩家名（例如 `?`）必须能被索引、能过 OCR 名字过滤器。

        回归：旧实现把「去掉所有非字母数字」当作唯一归一化手段，
        `?` 会变成空串然后被整个丢出索引 —— 这类名字永远匹配不上。
        """
        import tempfile
        from database import BlacklistDB as DB
        from matcher import Matcher as M
        from scan_session import is_valid_player_name
        fd, tmp = tempfile.mkstemp(suffix=".db", dir=config.LOG_DIR)
        os.close(fd)
        db = DB(tmp)
        try:
            db.add("-", "?")
            m = M(db)
            if not m.check(["?"]):
                raise RuntimeError("黑名单里的 `?` 无法被 check() 命中")
            if not m.check(["？"]):                 # 全角也要能对上
                raise RuntimeError("全角 `？` 无法命中半角 `?` 条目")
            if not m.match_text("? : hello"):
                raise RuntimeError("聊天文本里的 `?` 无法命中")
            if not is_valid_player_name("?", m.name_allowlist()):
                raise RuntimeError("OCR 名字过滤器会把 `?` 丢掉")
            if is_valid_player_name("???", m.name_allowlist()):
                raise RuntimeError("`???` 不应命中只有 `?` 时放行的白名单")
            return "全符号玩家名（如 `?`）可索引、可命中、可过过滤器"
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    check("全符号玩家名", _symbol_check)

    def _region_check():
        rc = RegionConfig()
        warns = rc.warnings()
        if warns:
            raise RuntimeError("；".join(warns))
        sizes = "，".join(f"{k}={v['width']}x{v['height']}"
                          for k, v in rc.get_all().items())
        return f"三个区域尺寸合理（{sizes}）"

    check("监视区域尺寸", _region_check)

    try:
        from notifier import ensure_default_icon
        check("生成默认提示图", lambda: ensure_default_icon())
    except Exception as e:                               # noqa: BLE001
        print(f"[FAIL] 生成默认提示图: {e}")
        problems.append("默认提示图")

    def _icon_check():
        """应用图标：PNG（窗口/托盘）+ ICO（exe/任务栏小图标）。"""
        from PIL import Image
        missing = [p for p in (config.APP_ICON_PATH, config.APP_ICON_ICO)
                   if not os.path.exists(p)]
        if missing:
            raise RuntimeError("缺少图标文件: "
                               + ", ".join(os.path.basename(m) for m in missing))
        with Image.open(config.APP_ICON_PATH) as im:
            size = im.size
        return f"应用图标就绪 {size[0]}x{size[1]}（PNG + ICO）"

    check("应用图标", _icon_check)

    def _capture_check():
        from screen_capture import ScreenCapture
        cap = ScreenCapture(RegionConfig())
        try:
            vs = cap.virtual_screen()
            img = cap.grab_region({"left": vs["left"], "top": vs["top"],
                                   "width": 64, "height": 64})
            if img is None:
                raise RuntimeError("截图返回空")
            return f"虚拟桌面 {vs['width']}x{vs['height']}, 截图 {img.size}"
        finally:
            cap.close()

    check("屏幕捕获", _capture_check)

    def _ocr_check():
        from ocr_engine import OCREngine
        from PIL import Image
        eng = OCREngine()
        if not eng.warmup():
            raise RuntimeError("RapidOCR 不可用（未安装或模型缺失）")
        out = eng.recognize_raw(Image.new("RGB", (200, 40), (0, 0, 0)))
        return f"RapidOCR 就绪，空图返回 {len(out)} 个结果"

    check("OCR 引擎", _ocr_check)

    def _overlay_check():
        from notifier import _OverlayWindow
        w = _OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                raise RuntimeError("分层窗口创建失败")
            GWL_EXSTYLE = -20
            from notifier import user32
            style = user32.GetWindowLongW(w.hwnd, GWL_EXSTYLE)
            need = {"NOACTIVATE": 0x08000000, "TRANSPARENT": 0x00000020,
                    "TOOLWINDOW": 0x00000080, "TOPMOST": 0x00000008,
                    "LAYERED": 0x00080000}
            lack = [k for k, v in need.items() if not style & v]
            if lack:
                raise RuntimeError("缺少样式: " + ", ".join(lack))
            return "无焦点 Overlay 样式齐全"
        finally:
            w.close()

    check("无焦点 Overlay", _overlay_check)

    def _sound_check():
        import winsound
        winsound.Beep(1200, 120)
        return True

    check("提示音 (winsound)", _sound_check)

    print("-" * 66)
    if missing:
        print("缺失依赖：" + ", ".join(missing))
        print("安装：pip install -r requirements.txt")
    if problems:
        print(f"自检未通过项：{len(problems)} 个 → " + ", ".join(problems))
        return 1
    print("自检通过 ✅")
    return 0


def run_capture_debug() -> int:
    """抓取全部监视区域 + OCR，输出调试截图供人工确认坐标与识别效果。

    产物（data/logs/）：
        debug_screen.png        —— 整屏缩略图，用红框标出三个监视区域
        debug_<region>.png      —— 每个区域的原始截图
    """
    from PIL import Image, ImageDraw
    from notifier import ensure_default_icon

    config.enable_dpi_awareness()
    from ocr_engine import OCREngine
    from region_config import RegionConfig
    from screen_capture import ScreenCapture

    rc = RegionConfig()
    cap = ScreenCapture(rc)
    ocr = OCREngine()
    ocr_ok = ocr.warmup()

    print("=" * 66)
    print("监视区域调试截图")
    print("=" * 66)
    vs = cap.virtual_screen()
    print(f"虚拟桌面: {vs['width']}x{vs['height']} @ ({vs['left']},{vs['top']})")
    print(f"OCR 引擎: {'可用' if ocr_ok else '不可用（未安装 rapidocr）'}")
    print("-" * 66)

    full = cap.grab_region(vs)
    annotated = full.copy() if full is not None else None
    draw = ImageDraw.Draw(annotated) if annotated is not None else None

    for key in RegionConfig.keys():
        r = rc.get(key)
        src = rc.source_of(key)
        print(f"\n[{RegionConfig.label(key)}]  ({key}, 来源={src})")
        print(f"  left={r['left']} top={r['top']} "
              f"width={r['width']} height={r['height']}")
        img = cap.grab(key)
        if img is None:
            print("  ✗ 截图失败（坐标可能超出屏幕）")
            continue
        out = os.path.join(config.LOG_DIR, f"debug_{key}.png")
        img.save(out)
        print(f"  ✓ 截图已保存: {out}  ({img.width}x{img.height})")

        if ocr_ok:
            results = ocr.recognize_raw(img)
            if results:
                for text, conf in results:
                    print(f"    OCR: {text!r}  conf={conf:.2f}")
            else:
                print("    OCR: （未识别到任何文字）")
        else:
            print("    OCR: 跳过（引擎不可用）")

        if draw is not None:
            x0 = r["left"] - vs["left"]
            y0 = r["top"] - vs["top"]
            draw.rectangle([x0, y0, x0 + r["width"], y0 + r["height"]],
                           outline=(255, 0, 0), width=3)
            draw.text((x0 + 4, max(0, y0 - 14)), key, fill=(255, 0, 0))

    if annotated is not None:
        max_w = 1600
        if annotated.width > max_w:
            ratio = max_w / annotated.width
            annotated = annotated.resize(
                (max_w, int(annotated.height * ratio)), Image.LANCZOS)
        out = os.path.join(config.LOG_DIR, "debug_screen.png")
        annotated.save(out)
        print(f"\n整屏标注图: {out}")

    ensure_default_icon()
    cap.close()
    print("\n请打开上述 PNG 确认：区域框是否覆盖聊天框 / 玩家列表。")
    print("若不正确，请在 GUI 里用 [校准区域] 重新框选。")
    return 0


def run_notification_preview() -> int:
    """离线渲染一张通知预览图（不需要显示器也能看效果）。"""
    config.enable_dpi_awareness()
    from PIL import Image
    from notification_config import NotificationConfig
    from notifier import render_overlay

    cfg = NotificationConfig().get()
    ap = cfg.get("appearance") or {}
    width = max(160, int(ap.get("width", 360)))
    height = max(60, int(ap.get("height", 100)))
    entry = {"player_name": "SamplePlayer_01", "player_id": "76561198000000001",
             "note": "示例条目：疑似故意 TK 队友", "tk_count": 2,
             "last_seen": "2026-01-02 03:04:05"}
    img = render_overlay(cfg, entry, 94.0, "chat", width, height)
    out = os.path.join(config.LOG_DIR, "notification_preview.png")
    os.makedirs(config.LOG_DIR, exist_ok=True)
    # 铺一层深色底，方便看清半透明效果
    canvas = Image.new("RGB", (img.width + 40, img.height + 40), (24, 26, 30))
    canvas.paste(img, (20, 20), img)
    canvas.save(out)
    print(f"通知预览图已保存: {out}")
    return 0


class _CheckTee:
    """把自检输出同时写进日志。

    打包成 `--noconsole` 的 exe 之后 `sys.stdout` 是 None，直接跑
    `HD2Blacklist.exe --check` 什么都看不到 —— 那样自检等于没用。
    """

    def __init__(self, logger, real=None):
        self.logger = logger
        self.real = real
        self._buf = ""

    def write(self, text):
        if self.real is not None:
            try:
                self.real.write(text)
            except Exception:                            # noqa: BLE001
                pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.logger.info(line)

    def flush(self):
        if self._buf:
            self.logger.info(self._buf)
            self._buf = ""
        if self.real is not None:
            try:
                self.real.flush()
            except Exception:                            # noqa: BLE001
                pass


def run_self_check_logged() -> int:
    """跑自检，并把结果一并写进 data/logs/app.log（打包版唯一的查看途径）。"""
    real = sys.stdout
    tee = _CheckTee(get_logger("check"), real)
    sys.stdout = tee
    try:
        return run_self_check()
    finally:
        try:
            tee.flush()
        finally:
            sys.stdout = real


# ==========================================================================
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hd2_blacklist",
        description=f"{config.APP_NAME} —— 屏幕截图 + OCR 的本地黑名单提示工具")
    parser.add_argument("--check", action="store_true",
                        help="只做环境自检，不启动 GUI")
    parser.add_argument("--capture-debug", action="store_true",
                        help="抓取各监视区域并输出调试截图到 data/logs/")
    parser.add_argument("--preview-notification", action="store_true",
                        help="离线渲染一张通知预览图到 data/logs/")
    parser.add_argument("--debug", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args(argv)

    if args.debug:
        config.LOG_LEVEL = "DEBUG"
        import logging
        get_logger("app").setLevel(logging.DEBUG)

    if args.check:
        return run_self_check_logged()
    if args.capture_debug:
        return run_capture_debug()
    if args.preview_notification:
        return run_notification_preview()

    # 必须在任何窗口 / 截图之前开启 DPI 感知
    config.enable_dpi_awareness()
    # 把整个进程压到「低于正常」优先级：抓屏 + OCR 是突发吃 CPU 的活，
    # 让游戏线程始终优先拿到 CPU，游戏的最低帧不再被拖（见 priority.py）
    if config.GAME_FRIENDLY_PRIORITY:
        try:
            from priority import apply_game_friendly
            apply_game_friendly(config.PROCESS_PRIORITY)
        except Exception as e:                           # noqa: BLE001
            get_logger("app").warning("设置进程优先级失败: %s", e)
    # 设置 AppUserModelID：否则任务栏会把程序当成 python.exe，图标也不对
    try:
        from theme import set_taskbar_identity
        set_taskbar_identity(config.APP_ID)
    except Exception:                                    # noqa: BLE001
        pass

    # ---- 单实例：同一时间只允许跑一个进程 ----
    # （命令行诊断模式已在上面 return，不受限制，方便程序开着时排查）
    from single_instance import SingleInstance, notify_existing_instance
    single = SingleInstance()
    if not single.acquire():
        notify_existing_instance()
        return 1

    app = None
    try:
        app = HD2BlacklistApp(gui_enabled=True)
        app.start()
    except KeyboardInterrupt:
        if app is not None:
            app.shutdown()
    except Exception:                                    # noqa: BLE001
        get_logger("app").error("启动失败:\n%s", traceback.format_exc())
        if app is not None:
            try:
                app.shutdown()
            except Exception:                            # noqa: BLE001
                pass
        _fatal_dialog("启动失败", traceback.format_exc())
        return 1
    finally:
        single.release()
    return 0


def _fatal_dialog(title: str, detail: str) -> None:
    """把异常弹成可读的对话框（打包成 exe 后也不再是 PyInstaller 的原始报错框）。"""
    try:
        import tkinter as tk
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showerror(title, detail)
        root.destroy()
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                                # noqa: BLE001
        _tb = traceback.format_exc()
        try:
            get_logger("app").error("未捕获异常:\n%s", _tb)
        except Exception:                                # noqa: BLE001
            pass
        _fatal_dialog("程序异常退出", _tb)
        sys.exit(1)

# -*- coding: utf-8 -*-
"""config.py —— HD2 黑名单系统全局配置常量。

本模块只放常量与路径解析，不做任何有副作用的 IO（除 ensure_dirs()）。
所有路径都基于 BASE_DIR 解析，因此从任意工作目录启动、以及 PyInstaller
打包成 exe 之后都能正确找到 data/ 目录。
"""
from __future__ import annotations

import os
import sys

# --------------------------------------------------------------------------
# 应用信息
# --------------------------------------------------------------------------
APP_NAME = "HD2 黑名单"
APP_ID = "hd2_blacklist"
VERSION = "1.0.0"
#: 主窗口标题（GUI 与单实例「叫回窗口」都用它，避免两处写法漂移）
WINDOW_TITLE = f"{APP_NAME} v{VERSION}"

# 目标进程（进程监控只认这些名字，全部小写）
TARGET_PROCESS_NAMES = ("helldivers2.exe",)

# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------
def _resolve_base_dir() -> str:
    """返回程序根目录。

    - 开发态：config.py 所在目录
    - frozen（PyInstaller）：exe 所在目录（data/ 与 exe 同级，便于用户编辑）
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resolve_base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
ASSETS_DIR = os.path.join(DATA_DIR, "assets")
EVIDENCE_DIR = os.path.join(DATA_DIR, "evidence")
LOG_DIR = os.path.join(DATA_DIR, "logs")

DB_PATH = os.path.join(DATA_DIR, "blacklist.db")
USER_CONFIG_PATH = os.path.join(DATA_DIR, "user_config.json")
NOTIFICATION_PATH = os.path.join(DATA_DIR, "notification.json")
HOTKEY_PATH = os.path.join(DATA_DIR, "hotkey.json")
DEFAULT_ICON_PATH = os.path.join(ASSETS_DIR, "default_icon.png")
APP_ICON_PATH = os.path.join(ASSETS_DIR, "app_icon.png")
APP_ICON_ICO = os.path.join(ASSETS_DIR, "app_icon.ico")
LOG_PATH = os.path.join(LOG_DIR, "app.log")

DIRECTORIES = (DATA_DIR, ASSETS_DIR, EVIDENCE_DIR, LOG_DIR)

#: 随程序一起分发的静态资源（打包后在 sys._MEIPASS/data/assets 里）
BUNDLED_ASSETS = ("app_icon.png", "app_icon.ico", "default_icon.png")


def _seed_bundled_assets() -> None:
    """打包版：data/ 被删掉重建时，把随包分发的图标补回来。

    否则「删掉整个 data/ 彻底重置」之后应用图标就没了，自检也会报错 ——
    而 README 恰恰教用户这么重置。
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return                                   # 开发态：data/assets 本来就在
    src_dir = os.path.join(base, "data", "assets")
    if not os.path.isdir(src_dir):
        return
    for name in BUNDLED_ASSETS:
        src = os.path.join(src_dir, name)
        dst = os.path.join(ASSETS_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                import shutil
                shutil.copy2(src, dst)
            except OSError:
                pass


def ensure_dirs() -> None:
    """确保 data/ 及其子目录存在（并把随包的图标补齐）。"""
    for d in DIRECTORIES:
        os.makedirs(d, exist_ok=True)
    _seed_bundled_assets()


def enable_dpi_awareness() -> bool:
    """开启 Per-Monitor DPI 感知。

    必须在创建任何窗口 / 截图之前调用。否则在缩放不是 100% 的显示器上，
    系统会对坐标做虚拟化，导致 mss 截图区域和 Overlay 位置都对不上。
    """
    if os.name != "nt":
        return False
    import ctypes
    try:                                   # Windows 8.1+ 推荐：Per-Monitor v2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:                      # noqa: BLE001
        pass
    try:                                   # 退回 Vista+ 的 System DPI aware
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:                      # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 区域配置系统
# --------------------------------------------------------------------------
# 默认坐标以 1920x1080 无边框窗口、游戏 UI 缩放 100% 为基准，
# 其它分辨率 / 缩放比例请使用 GUI 的 [校准区域] 重新框选。
DEFAULT_REGIONS = {
    "chat_event":       {"left": 20, "top": 720, "width": 420, "height": 160},
    "player_list_hud":  {"left": 40, "top": 200, "width": 400, "height": 120},
    "menu_player_list": {"left": 60, "top": 120, "width": 500, "height": 300},
}

REGION_META = {
    "chat_event":       {"label": "聊天框事件区域", "desc": "检测玩家加入/离开提示文字"},
    "player_list_hud":  {"label": "HUD 玩家列表", "desc": "任务中显示的队友名称区域"},
    "menu_player_list": {"label": "ESC 菜单玩家列表", "desc": "按 ESC 后左上角玩家信息区域"},
}

# 区域最小可用尺寸（防止用户框选到 0 像素区域）
REGION_MIN_SIZE = 8

# --------------------------------------------------------------------------
# 通知配置系统
# --------------------------------------------------------------------------
DEFAULT_NOTIFICATION = {
    "version": 1,
    "title_template": "⚠️ 黑名单玩家",
    "body_template": "{player_name}\n备注：{note}\n匹配度：{match_score}%",
    "show_fields": {
        "note": True, "tk_count": False, "match_score": True,
        "time": False, "source": False,
    },
    "image_mode": "default",       # default / custom / none
    "image_path": "",
    "image_size": [64, 64],
    "appearance": {
        "background_color": "#2b2b2b",
        "text_color": "#ffffff",
        "title_color": "#ff5555",
        "opacity": 0.85,
        "width": 360, "height": 100,
        "title_font_size": 16,     # 标题字号（像素）
        "body_font_size": 13,      # 正文字号（像素）
        "position": "top_right",   # top_left/top_center/top_right/
                                   # bottom_left/bottom_center/bottom_right/center
        "monitor": 0,              # 0=整个虚拟桌面（可覆盖副屏），1..N=指定显示器
    },
    "timing": {"duration": 4.0},
    "sound": {
        "enabled": True, "mode": "beep",   # beep / wav / none
        "path": "",
        "beep_freq": 1200, "beep_duration": 120,
    },
}

# 通知可用占位符（GUI 提示 & 校验用）
NOTIFICATION_PLACEHOLDERS = (
    "{player_name}", "{match_score}", "{note}", "{tk_count}",
    "{source}", "{time}", "{last_seen}",
)

NOTIFICATION_POSITIONS = (
    "top_left", "top_center", "top_right",
    "bottom_left", "bottom_center", "bottom_right",
    "center",
)

# 通知文字字号（像素）。窗口高度会随字号自动增高，不会截断文字。
DEFAULT_TITLE_FONT_SIZE = 16
DEFAULT_BODY_FONT_SIZE = 13
FONT_SIZE_MIN = 8
FONT_SIZE_MAX = 72

# --------------------------------------------------------------------------
# 聊天框扫描（按需触发，不再有 1 秒 tick / 10 秒定期扫描）
# --------------------------------------------------------------------------
CHAT_SCAN_REGION_KEY = "chat_event"
CHAT_SCAN_SOURCE = "chat_manual"    # 手动点按钮
CHAT_SCAN_HOTKEY_SOURCE = "chat_hotkey"

# 热键（默认关闭，避免与游戏内快捷键冲突；可在 GUI 里自定义并持久化）
CHAT_SCAN_HOTKEY_ENABLED = False
CHAT_SCAN_HOTKEY_VK = 0x77          # F8
CHAT_SCAN_HOTKEY_DEBOUNCE = 1.0

#: data/hotkey.json 的默认内容（用户改过就以文件为准）
DEFAULT_HOTKEY = {
    "version": 1,
    "enabled": CHAT_SCAN_HOTKEY_ENABLED,
    "vk": CHAT_SCAN_HOTKEY_VK,
    "ctrl": False,
    "alt": False,
    "shift": False,
    "name": "F8",
}

# 命中去重窗口：同一玩家在该窗口内只计数一次、只提示一次
# （30 秒：既能挡住「连点扫描按钮」造成的重复计数，又不至于漏掉
#   隔一会儿又出现一次的同一玩家）
HIT_DEDUP_WINDOW = 30

# 批量命中时的通知堆叠
MAX_NOTIFY_STACK = 5                # 最多同时弹出 5 个通知栏
NOTIFY_STACK_GAP = 8                # 通知栏之间的垂直间距（像素）
NOTIFY_STACK_BASE_Y = 40            # 第一个通知栏距离屏幕顶部的距离

# --------------------------------------------------------------------------
# 游戏友好（保护游戏帧数）—— 必须在 ESC_SESSION 之前定义
# --------------------------------------------------------------------------
# 抓屏 + OCR 会一次性吃掉一大块 CPU（实测单次可达 1~6 CPU秒，压在 1 秒内
# 跑完 ≈ 瞬时占满 3 个核）。如果和游戏同为 NORMAL 优先级，Windows 会公平
# 轮转 CPU，游戏的「最低帧」就会被拉低。把本进程压到 BELOW_NORMAL、扫描线程
# 再压到 LOWEST 之后，游戏线程始终优先拿到 CPU。
#   · 想对比效果 / 觉得扫得太慢 → 改成 False，恢复系统默认优先级
GAME_FRIENDLY_PRIORITY = True
PROCESS_PRIORITY = "below_normal"   # idle / below_normal / normal
SCAN_THREAD_PRIORITY = "lowest"     # idle / lowest / below_normal / normal

# 扫描会话里「画面没变化就跳过 OCR」：
# 菜单/列表静止时对同一张图反复 OCR 是纯浪费，还会持续拖累游戏帧数。
# 只做一次缩略图的 numpy 差分（几十微秒）就能判断画面变没变。
SESSION_SKIP_UNCHANGED = True
SESSION_DIFF_TOLERANCE = 1.0        # 缩略图平均像素差阈值（0~255），越小越敏感

# 「画面连续静止 N 帧就收工」——这条才是省游戏帧数的关键：
# 每次抓屏都会让 DWM/GPU 同步一次，游戏就可能卡一帧；抓屏次数 ≈ 卡顿次数。
# 菜单开着不动时其实一眼就看完了，没必要 8 秒里抓 26 次。
#   0 = 不启用（会话只受 max_duration 约束）
SESSION_MAX_STATIC_FRAMES = 6       # 配合 0.5s 间隔 ≈ 静止 3 秒收工

# --------------------------------------------------------------------------
# ESC 菜单扫描
# --------------------------------------------------------------------------
ESC_POLL_INTERVAL = 0.1             # 每 100ms 轮询 ESC 键
ESC_MENU_DELAY = 0.6                # 等菜单动画
ESC_DEBOUNCE = 1.0                  # 按键去抖

ESC_SESSION = {
    "interval": 0.5,                # 抓屏间隔：抓一次就可能让游戏卡一帧，
                                    # 所以宁可稀一点（滚动停下后画面仍在，照样能扫到）
    "max_duration": 8.0,
    "max_consecutive_empty": 3,
    "keep_alive_after_hit": 3.0,
    "max_static_frames": SESSION_MAX_STATIC_FRAMES,
}

# ESC 菜单是否打开的判定阈值（区域灰度标准差）
ESC_MENU_STD_THRESHOLD = 20.0

# --------------------------------------------------------------------------
# 冷启动扫描
# --------------------------------------------------------------------------
COLD_START_DELAY = 12.0
COLD_START_SESSION = {
    "interval": 1.5,
    "max_duration": 15.0,
    "max_consecutive_empty": 3,
    "keep_alive_after_hit": 0,
}

# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------
MATCH_THRESHOLD = 85
OCR_CONFIDENCE_MIN = 0.7

# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
OCR_UPSCALE = 2                     # 识别前放大倍数（2~3 倍效果最佳）
OCR_MIN_TEXT_LEN = 1
OCR_INTRA_OP_THREADS = 2            # 限制 onnxruntime 线程数，压低 CPU 占用
OCR_USE_ANGLE_CLS = False           # 聊天/HUD 文本基本无旋转，关掉省 CPU

# --------------------------------------------------------------------------
# 证据 / 日志
# --------------------------------------------------------------------------
EVIDENCE_JPEG_QUALITY = 80          # 证据截图压缩质量（省磁盘）
EVIDENCE_MAX_FILES = 500            # 超过后自动删除最旧的证据图
LOG_MAX_BYTES = 2 * 1024 * 1024     # 单个日志文件上限 2MB
LOG_BACKUP_COUNT = 2
LOG_LEVEL = "INFO"

# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
GUI_QUEUE_POLL_MS = 100             # 主线程轮询后台队列的间隔
FLASH_TIMES = 6                     # 命中后闪烁次数
FLASH_INTERVAL_MS = 250             # 闪烁间隔（6 * 250ms ≈ 3s）


# --------------------------------------------------------------------------
# 日志（放在 config 里，避免额外模块；所有模块都能 import config 使用）
# --------------------------------------------------------------------------
import logging                                       # noqa: E402
from logging.handlers import RotatingFileHandler     # noqa: E402

_log_lock = __import__("threading").RLock()
_loggers: dict = {}
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "hd2") -> "logging.Logger":
    """返回一个写入 data/logs/app.log 的 logger（轮转，限制单文件大小）。

    同一个 name 只配置一次 handler，可安全地在多线程中调用。
    """
    with _log_lock:
        if name in _loggers:
            return _loggers[name]

        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        logger.propagate = False

        if not logger.handlers:
            ensure_dirs()
            try:
                fh = RotatingFileHandler(
                    LOG_PATH, maxBytes=LOG_MAX_BYTES,
                    backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
                )
                fh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
                logger.addHandler(fh)
            except Exception as e:                       # noqa: BLE001
                print(f"[config] 日志文件初始化失败: {e}")

            if sys.stderr is not None:                   # pythonw 下 stderr 为 None
                try:
                    sh = logging.StreamHandler()
                    sh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
                    logger.addHandler(sh)
                except Exception:                        # noqa: BLE001
                    pass

        _loggers[name] = logger
        return logger

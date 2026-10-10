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
VERSION = "2.0.0"
#: 主窗口标题（GUI 与单实例「叫回窗口」都用它，避免两处写法漂移）
WINDOW_TITLE = f"{APP_NAME} v{VERSION}"

# 数据来源：游戏内插件 HD2Tracker 写出的日志文件（本应用**只读这一个文件**）。
# 不再有进程监控 —— 「游戏有没有在跑」直接由日志里的事件告诉你。

# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------
def _resolve_base_dir() -> str:
    """返回程序根目录。

    - 开发态：**仓库根目录**（= app/ 的上一级，也就是 source_code/）——
      data/ 与源码同级，方便直接编辑与备份
    - frozen（PyInstaller）：exe 所在目录（data/ 与 exe 同级，便于用户编辑）
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # 本文件在 app/config.py → 上跳两级才是仓库根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _resolve_base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
ASSETS_DIR = os.path.join(DATA_DIR, "assets")
LOG_DIR = os.path.join(DATA_DIR, "logs")

DB_PATH = os.path.join(DATA_DIR, "blacklist.db")
#: 预留：将来要允许用户改写数据来源路径（例如插件日志换个位置）就落在这里。
#: v2 目前**不读**这个文件，删掉它不会有任何影响。
USER_CONFIG_PATH = os.path.join(DATA_DIR, "user_config.json")
NOTIFICATION_PATH = os.path.join(DATA_DIR, "notification.json")
DEFAULT_ICON_PATH = os.path.join(ASSETS_DIR, "default_icon.png")
APP_ICON_PATH = os.path.join(ASSETS_DIR, "app_icon.png")
APP_ICON_ICO = os.path.join(ASSETS_DIR, "app_icon.ico")
LOG_PATH = os.path.join(LOG_DIR, "app.log")

DIRECTORIES = (DATA_DIR, ASSETS_DIR, LOG_DIR)

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

    必须在创建任何窗口之前调用。否则在缩放不是 100% 的显示器上，系统会对
    坐标做虚拟化，导致无焦点提示浮层的位置和尺寸都对不上。
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
# 数据源：插件日志文件（唯一的输入）
# --------------------------------------------------------------------------
# 游戏内插件 HD2Tracker 把每一条记录写成一行 JSON、只追加到这个文件：
#   %LOCALAPPDATA%\CowboyBingus\Helldivers2\Logs\playerLog.txt
# 本应用**只读**它：不注入、不读内存、不改包、不抢焦点。
# 字段含义见 V2\build\hd2list\docs\记录字段说明.md。
_LOCALAPPDATA = (
    os.environ.get("LOCALAPPDATA")
    or os.path.join(os.path.expanduser("~"), "AppData", "Local")
)
#: 插件日志目录（LOCALAPPDATA 取不到时按 Windows 规范退回 ~\AppData\Local）
PLUGIN_LOG_DIR = os.path.join(_LOCALAPPDATA, "CowboyBingus", "Helldivers2", "Logs")
#: 数据文件（一行一条 JSON，UTF-8，只追加）
PLAYER_LOG_NAME = "playerLog.txt"
#: 插件的诊断日志（给人看的，本应用不读；自检只用它判断"插件到底装没装"）
PLUGIN_DIAG_NAME = "playerLog.log"


def player_log_path() -> str:
    """插件日志文件的绝对路径。"""
    return os.path.join(PLUGIN_LOG_DIR, PLAYER_LOG_NAME)


def plugin_diag_path() -> str:
    """插件诊断日志的绝对路径。"""
    return os.path.join(PLUGIN_LOG_DIR, PLUGIN_DIAG_NAME)


# --------------------------------------------------------------------------
# 文件追踪
# --------------------------------------------------------------------------
#: 轮询间隔（秒）。文件只追加，1 Hz 足够；一次轮询只是一个 os.stat，几乎不耗 CPU
WATCH_POLL_INTERVAL = 1.0
#: 单轮最多消化多少行 —— 防止首启去追一个几十万行的历史文件时把界面拖住
WATCH_TAIL_MAX_LINES = 5000
#: 首次启动时最多往回读多少字节（只用于立刻在界面上显示"你现在队里有谁"）
WATCH_BACKFILL_BYTES = 512 * 1024
#: 幂等去重：记住最近这么多个 capture_id，同一行永不重复处理
WATCH_SEEN_IDS = 4096
#: 追踪线程优先级（idle / lowest / below_normal / normal）
WATCH_THREAD_PRIORITY = "lowest"

#: 插件最低版本。更老的版本字段不一样（没有 capture_id / peer_id_hex），
#: 界面必须明确报出来，否则用户只会觉得"应用没反应"。
PLUGIN_MIN_VERSION = "hd2trackerv21.2"

# --------------------------------------------------------------------------
# 事件类型（插件写在每行的 `ev` 字段里）
# --------------------------------------------------------------------------
EVENT_FILE_START = "file_start"      # 插件每次加载（= 游戏每次启动）
EVENT_MATCH_START = "match_start"    # 检测到进入一局
EVENT_JOIN = "join"                  # 有人进队（或回来了）
EVENT_LEAVE = "leave"                # 有人离队（插件侧已做 2 秒防抖）
EVENT_UPDATE = "update"              # 同一个人的信息补齐（典型：名字晚几秒才拿到）
EVENT_SQUAD = "squad"                # 全队快照 —— 这是**状态**，不是事件
EVENT_MATCH_END = "match_end"        # 对局会话消失

#: 允许触发告警的事件。铁律：**只有 join**。
#: `squad` 是快照，用它触发会让"进队就在队里的人"和"刚进来的人"混在一起反复报警。
ALERT_EVENTS = (EVENT_JOIN,)

#: 告警**只认 PeerID**：名字可以重名、可以随时改，拿它当身份会认错人。
#: 所以没有 PeerID 的老条目不会参与告警（"补上 ID 才生效"）。
ALERT_BY_PEER_ID_ONLY = True

#: 同一 (game_pid, peer_id_hex) 在这个秒数内只告警一次
#: （30 秒：挡住"同一局里反复进出"造成的连响，又不至于漏掉隔一会儿又回来的人）
HIT_DEDUP_WINDOW = 30

#: 命中来源 -> 人话。界面动态流、日志、通知栏共用一套叫法
ALERT_SOURCE_LABELS = {
    "peer_join": "PeerID 命中（有人进队）",
}

#: 名字自动同步：日志里看到某个 PeerID 的名字变了，就把名单里那条的名字改过来
#: （PeerID 是身份、名字只是显示名，写个过期名字只会误导人）。
#: 同一个人在这个秒数内最多同步一次 —— 挡的是"名册读取抖动"造成的反复改名。
NAME_SYNC_COOLDOWN = 5.0


def alert_source_label(source: str, default: str = "") -> str:
    """把内部的 source 字符串翻成能给用户看的名字。"""
    if not source:
        return default
    return ALERT_SOURCE_LABELS.get(source, default or source)


# --------------------------------------------------------------------------
# 「最近遇到」
# --------------------------------------------------------------------------
#: 界面上最多显示多少条
RECENT_KEEP = 200
#: 只显示最近这么多天遇到的人
RECENT_DAYS = 7

# --------------------------------------------------------------------------
# 通知配置系统
# --------------------------------------------------------------------------
DEFAULT_NOTIFICATION = {
    "version": 1,
    "title_template": "⚠️ 黑名单玩家",
    "body_template": "{player_name}\n备注：{note}\n匹配度：{match_score}%",
    "show_fields": {
        "note": True, "match_score": True,
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
# 名单里没有玩家ID / TK次数 / 最后遇见这些字段了，占位符同步取消
NOTIFICATION_PLACEHOLDERS = (
    "{player_name}", "{match_score}", "{note}", "{source}", "{time}",
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

# 批量命中时的通知堆叠
MAX_NOTIFY_STACK = 5                # 最多同时弹出 5 个通知栏
NOTIFY_STACK_GAP = 8                # 通知栏之间的垂直间距（像素）
NOTIFY_STACK_BASE_Y = 40            # 第一个通知栏距离屏幕顶部的距离

# --------------------------------------------------------------------------
# 触发的来源与游戏友好（保护游戏帧数）
# --------------------------------------------------------------------------
# 本应用只做「读一个文本文件 + 比对字符串」，本身开销可以忽略；但仍把本进程
# 压到 BELOW_NORMAL、追踪线程压到 LOWEST，把 CPU 优先让给游戏线程。
#   · 想对比效果 → 改成 False，恢复系统默认优先级
GAME_FRIENDLY_PRIORITY = True
PROCESS_PRIORITY = "below_normal"   # idle / below_normal / normal

# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------
# ⚠ 告警**只认 PeerID**（`ALERT_BY_PEER_ID_ONLY`）。下面这三个常量属于
#   名字匹配那一层，而它现在只服务于**离线排查**（`--replay` / 自检 / 单元测试），
#   不参与线上告警 —— 名字会重名、会随时改，拿它当身份会认错人。
#: 名字模糊匹配的相似度阈值（0~100）。
MATCH_THRESHOLD = 85

#: 允许把「纯符号名字」写进黑名单（例如玩家名就是一个问号 `?` / `？`）。
#: 这类名字无法用「去掉所有非字母数字」的老办法归一化（会被归一化成空串然后
#: 被直接丢出索引），所以单独走一层「符号层」：只做 NFKC + 去空白 + 大小写折叠，
#: 并且**只按整体相等**匹配 —— 否则一个 `?` 会在每句话里误报。
#: 关掉它会导致全符号名字永远匹配不上。
MATCH_SYMBOL_NAMES = True

#: 易混字符容错：把 0/O、1/l/I、5/S、8/B、2/Z、4/A 折叠成同一个字符
#: 再做一次「整体相等」比较。这是给**老条目**留的兜底 —— 那些条目当年是靠
#: 截图 + OCR 认名字认错的。仅在该折叠键于黑名单中**唯一**时才生效，
#: 所以不会把两个不同的玩家混成一个。
MATCH_FUZZY_CONFUSABLE = True

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
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

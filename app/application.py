# -*- coding: utf-8 -*-
"""application.py —— 把各模块组装起来 + 环境自检 + 命令行入口。

    python main.py                          正常启动（GUI + 后台追踪）
    python main.py --check                  环境自检（不开 GUI，逐项打印）
    python main.py --watch-debug            前台打印尾随到的每一行与解析结果
    python main.py --replay <日志文件>       离线回放一份日志（不写库，只看结论）
    python main.py --preview-notification   离线渲染一张通知预览图
    python main.py --debug                  打开 DEBUG 日志

数据来源只有一个：游戏内插件 HD2Tracker 写出的
`%LOCALAPPDATA%\\CowboyBingus\\Helldivers2\\Logs\\playerLog.txt`。

本应用**只读这一个文本文件**：不注入游戏、不读游戏内存、不改游戏包、不抢焦点。

告警铁律：只有 `ev == "join"`、不是你自己、且命中黑名单，才弹提示。
`squad` 是"现在队里有谁"的**状态快照**，永远不触发告警 ——
拿它报警会把进队就在队里的人反复报一遍。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback

from app import config
from app.config import get_logger


# ==========================================================================
class HD2BlacklistApp:
    """把各个模块组装起来。"""

    def __init__(self, gui_enabled: bool = True):
        config.ensure_dirs()

        from app.settings.notification_config import (NotificationConfig,
                                                      ensure_notification_file)
        ensure_notification_file()

        from app.core.database import BlacklistDB
        from app.core.matcher import Matcher
        from app.notify.notifier import Notifier, ensure_default_icon
        from app.settings.notification_config import NotificationConfig
        from app.ui.gui import BlacklistGUI
        from app.watch.watcher import PlayerLogWatcher

        self.log = get_logger("app")
        self._stop = threading.Event()
        self._gui_enabled = gui_enabled
        # 线程登记表：必须在**任何** _spawn() 之前建好
        self._threads = []

        self.db = BlacklistDB()
        self.notification_cfg = NotificationConfig()

        self.matcher = Matcher(self.db)
        ensure_default_icon()
        self.notifier = Notifier(self.notification_cfg)

        self.paused = False
        #: (game_pid, peer_id) -> 上次告警的单调时间，用于去重窗口
        self._alert_dedup: dict = {}
        #: peer_id -> 上次自动改名的单调时间（挡名册抖动造成的反复改名）
        self._rename_at: dict = {}

        self.watcher = PlayerLogWatcher(on_events=self._on_watch_events)

        self.gui = BlacklistGUI(
            db=self.db,
            notification_config=self.notification_cfg,
            matcher=self.matcher,
            notifier=self.notifier,
            watcher=self.watcher,
            on_quit=self.shutdown,
            on_pause=self.on_pause,
        ) if gui_enabled else None

        if self.gui is not None:
            self.gui.app = self

        self.log.info("数据来源：%s", config.player_log_path())

    # ------------------------------------------------------------ 追踪事件
    def _on_watch_events(self, events, live: bool):
        """尾随线程的回调 —— **在工作线程里执行，绝不能在这里碰 tkinter**。

        `live=False` 是首启补读的那一批：只记事实，不弹告警。
        """
        for ev in events:
            if ev.kind == config.EVENT_SQUAD:
                # 快照不是事件：只补"遇到过"的事实，**永远不告警**
                # （名字同步照做：名册里有名字就说明我们知道他现在叫什么）
                for peer_id, name in ev.squad:
                    if self.watcher.state.is_self(peer_id):
                        continue
                    if name:
                        self._sync_blacklist_name(peer_id, name)
                    self._touch_seen(peer_id, name, ev)
                continue

            if ev.kind == config.EVENT_FILE_START:
                self._gui_call("reset_session_stats")
                self._gui_call("set_monitoring", True)
                self._report(f"游戏启动（PID {ev.game_pid}）")
                continue
            if ev.kind == config.EVENT_MATCH_START:
                self._report("进入一局")
                continue
            if ev.kind == config.EVENT_MATCH_END:
                self._report(ev.note or "本局结束")
                continue

            if not ev.has_person or ev.is_self:
                continue

            # 名字同步放在告警之前：这样弹出来的提示里显示的就是**新名字**
            if ev.name:
                self._sync_blacklist_name(ev.peer_id, ev.name)

            if ev.kind in (config.EVENT_JOIN, config.EVENT_LEAVE,
                           config.EVENT_UPDATE):
                self._touch_seen(ev.peer_id, ev.name, ev)

            if ev.kind == config.EVENT_LEAVE:
                self._report(f"{ev.name or ev.peer_id} 离开队伍")
                continue

            if ev.kind in config.ALERT_EVENTS and live:
                self._alert(ev)

    def _sync_blacklist_name(self, peer_id, seen_name) -> None:
        """日志里看到名单中某个 PeerID 的名字变了 → **主动把名单里的名字改过来**。

        为什么值得自动改：PeerID 是身份、名字只是显示名。他改名的当下插件就能从
        名册里读到新名字，与其让用户某天发现"名单里还写着旧名字、我都认不出这是谁"，
        不如当场跟着改 —— 而且改完之后，提示里的名字也是他现在的名字。

        只在**名字非空**且**确实不同**时才写；同一人 `NAME_SYNC_COOLDOWN` 秒内最多
        改一次（挡名册读取抖动造成的反复改名）。改完必须 `matcher.reload()`，
        否则弹出来的提示里还是旧名字。
        """
        name = (seen_name or "").strip()
        if not name or not peer_id:
            return
        if self.matcher.check_peer(peer_id) is None:
            return                                   # 名单里没这个人，什么都不做

        now = time.monotonic()
        last = self._rename_at.get(peer_id)
        if last is not None and (now - last) < config.NAME_SYNC_COOLDOWN:
            return
        try:
            old = self.db.rename_by_peer_id(peer_id, name)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("同步名单名字失败（%s）: %s", peer_id, e)
            return
        if not old:
            return                                   # 名字没变
        self._rename_at[peer_id] = now
        if len(self._rename_at) > 512:                   # 顺手清理一下
            cutoff = now - config.NAME_SYNC_COOLDOWN * 60
            for k, t in list(self._rename_at.items()):
                if t < cutoff:
                    self._rename_at.pop(k, None)
        try:
            self.matcher.reload()                    # 让提示里显示新名字
        except Exception as e:                           # noqa: BLE001
            self.log.warning("改名后重建索引失败: %s", e)
        self.log.info("[改名] %s：%s → %s（名单已自动更新）",
                      peer_id, old, name)
        self._gui_call("on_rename", {"peer_id": peer_id, "old": old,
                                     "new": name})

    def _touch_seen(self, peer_id, name, ev) -> None:
        """把"遇到了这个人"落库（工作线程里跑；数据库自己有锁）。

        被忽略的人**直接跳过** —— 不然他下次一进队就又在「最近遇到」里冒出来，
        你点过的[忽略此人]等于白点。
        """
        try:
            if self.db.is_ignored(peer_id):
                return
            self.db.record_seen(peer_id, name, game_pid=ev.game_pid)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("记录「最近遇到」失败（%s）: %s", peer_id, e)

    def _alert(self, ev) -> None:
        """一条 join 事件 → 查忽略名单 → **按 PeerID** 查黑名单 → 弹提示。

        只认 PeerID：名字可以重名、可以随时改，拿它当身份会认错人。
        没有 PeerID 的老条目在这个环节**不参与**（它既不会命中、也不会误报），
        想让它生效得先补上 ID。
        """
        try:
            if self.db.is_ignored(ev.peer_id):
                self.log.debug("[忽略] %s 在忽略名单里，不提醒", ev.peer_id)
                return
        except Exception as e:                           # noqa: BLE001
            self.log.warning("查忽略名单失败（%s）: %s", ev.peer_id, e)

        hit = self.matcher.check_peer(ev.peer_id)
        if hit is None:
            return
        entry, score = hit
        if not self._should_alert(ev, entry):
            return

        who = ev.name or entry.get("player_name") or ev.peer_id
        source = "peer_join"
        try:
            acted = bool(self.notifier.alert(entry, float(score), source))
        except Exception:                                # noqa: BLE001
            self.log.exception("弹提示失败")
            acted = False

        label = config.alert_source_label(source)
        self.log.info("[命中] %s (%s) 匹配度 %.0f —— %s",
                      who, ev.peer_id, score, label)
        self._gui_call("on_alert", {
            "entry": entry, "score": float(score), "source": source,
            "name": who, "peer_id": ev.peer_id, "notified": acted,
        })

    def _should_alert(self, ev, entry) -> bool:
        """同一 (game_pid, peer) 在去重窗口内只告警一次。

        挡的是"同一局里反复进出"造成的连响 —— 插件侧 leave 有 2 秒防抖，
        但人真的可以一分钟里进出三次。
        """
        key = (ev.game_pid, ev.peer_id)
        now = time.monotonic()
        last = self._alert_dedup.get(key)
        if last is not None and (now - last) < config.HIT_DEDUP_WINDOW:
            self.log.info("[命中] %s 在 %d 秒去重窗口内，忽略",
                          ev.peer_id, config.HIT_DEDUP_WINDOW)
            return False
        self._alert_dedup[key] = now
        if len(self._alert_dedup) > 512:                 # 顺手清理过期键
            cutoff = now - config.HIT_DEDUP_WINDOW
            for k, t in list(self._alert_dedup.items()):
                if t < cutoff:
                    self._alert_dedup.pop(k, None)
        return True

    # ---------------------------------------------------------------- 暂停
    def on_pause(self, paused: bool):
        """暂停 / 恢复后台追踪。

        暂停 = 停掉尾随线程；恢复 = 重新起，并且**从文件末尾重新对齐**。
        也就是说暂停期间发生的事不会补报 —— 这正是"别打扰我"的意思。
        """
        self.paused = bool(paused)
        if self.paused:
            self.watcher.stop()
            self.log.info("已暂停追踪（期间的事件不会补报）")
            self._gui_call("set_monitoring", False, "追踪已暂停")
        else:
            self.watcher.start()
            self.log.info("已恢复追踪")
            self._gui_call("set_monitoring", True)

    # ---------------------------------------------------------------- 工具
    def _report(self, text: str, level: str = "info"):
        """把一句动态送进界面的「最近动态」（后台线程 → 主线程）。"""
        self._gui_call("notify_event", text, level)

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
        self.log.info("插件日志: %s", config.player_log_path())

        self._spawn(self.watcher.start, "PlayerLogWatcher")
        self._warn_legacy_entries()

        if self.gui is not None:
            self.gui.set_monitoring(False, "等待插件日志")
            self.gui.set_status(self.watcher.status_text())
            self.gui.run()
        else:
            self._stop.wait()

    def _spawn(self, target, name: str):
        t = threading.Thread(target=self._guarded, args=(target, name),
                             daemon=True, name=name)
        t.start()
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

    def _warn_legacy_entries(self):
        """有"没有 PeerID"的条目就在界面上说一声 —— 它们不会告警。

        这条必须说出来：从 v1 升上来的名单默认**全是**这种条目（v1 只存名字），
        不说的话用户只会看到"怎么不提醒了"，完全猜不到原因。
        """
        try:
            legacy = [e for e in self.db.get_all() if not e.get("peer_id")]
        except Exception as e:                           # noqa: BLE001
            self.log.warning("统计无 PeerID 条目失败: %s", e)
            return
        if not legacy:
            return
        text = (f"{len(legacy)} 条条目没有 PeerID → **不会告警**；"
                "到「最近遇到」里重新 [加入名单]，或右键 [补全 PeerID]")
        self.log.warning("[名单] %s", text)
        self._report(text, "warn")

    # ---------------------------------------------------------------- 关闭
    def shutdown(self):
        if self._stop.is_set():
            return
        self.log.info("正在退出…")
        self._stop.set()
        for closer, label in ((self.watcher.stop, "追踪线程"),
                              (self.notifier.close, "提示层"),
                              (self.db.close, "数据库")):
            try:
                closer()
            except Exception as e:                       # noqa: BLE001
                self.log.warning("关闭 %s 失败: %s", label, e)
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
    print(f"插件日志    : {config.player_log_path()}")
    print("-" * 66)

    check("创建数据目录", config.ensure_dirs)
    check("DPI 感知", config.enable_dpi_awareness)

    # 屏幕截图 / OCR / WMI 进程监控 / 全局热键 在 v2 全部下线：
    # 数据只来自插件日志文件，这几样依赖不再需要。
    mods = [("PIL", "Pillow"), ("numpy", "numpy"), ("mss", "mss"),
            ("rapidfuzz", "rapidfuzz"), ("pystray", "pystray"),
            ("winotify", "winotify"), ("pygame", "pygame"),
            ("tkinter", "tkinter")]
    missing = []
    for mod, label in mods:
        try:
            __import__(mod)
            print(f"[ OK ] 依赖 {label}")
        except Exception as e:                           # noqa: BLE001
            print(f"[WARN] 依赖 {label} 缺失: {e}")
            missing.append(label)

    print("-" * 66)

    from app.core.database import BlacklistDB
    from app.core.matcher import Matcher
    from app.settings.notification_config import (NotificationConfig,
                                                  ensure_notification_file)

    check("生成 notification.json", ensure_notification_file)
    check("通知配置读取", lambda: NotificationConfig().get()["image_mode"])

    def _assets_check():
        """自定义图片 / 音效的默认目录：既要存在，也要能写进去。"""
        from app.core import assets as asset_store
        d = asset_store.assets_dir()
        probe = os.path.join(d, ".write_probe.tmp")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        return (f"{d} 可写；音频格式 " +
                "/".join(e.lstrip(".") for e in asset_store.AUDIO_EXTS))

    check("自定义资源目录", _assets_check)

    def _priority_check():
        from app.core import priority as pri
        mode = ("开 → 启动时压到 " + config.PROCESS_PRIORITY
                if config.GAME_FRIENDLY_PRIORITY else "关（用系统默认）")
        return f"游戏友好模式{mode}；当前进程={pri.process_priority_name()}"

    check("游戏友好优先级", _priority_check)

    def _db_check():
        db = BlacklistDB()
        try:
            n = db.get_count()
            mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
            cols = [r[1] for r in db.conn.execute("PRAGMA table_info(blacklist)")]
            if "peer_id" not in cols:
                raise RuntimeError("blacklist 缺 peer_id 列")
            idx = {r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            if "idx_blacklist_peer" not in idx:
                raise RuntimeError("缺 idx_blacklist_peer（PeerID 唯一索引）")
            if "idx_blacklist_name" in idx:
                raise RuntimeError("旧的名字唯一索引还在，迁移没生效")
            return (f"{n} 条记录, journal_mode={mode}, 列={','.join(cols)}"
                    f"，索引=PeerID 唯一 + 无 ID 的名字唯一")
        finally:
            db.close()

    check("数据库 / WAL 模式", _db_check)

    def _matcher_check():
        """名单构成：几条能告警、几条只是"记着"。

        没有 PeerID 的条目**不会告警**（告警只认 PeerID），这件事必须在自检里
        说清楚，否则升级上来的用户只会觉得"名单里有他却不提醒"。
        """
        db = BlacklistDB()
        try:
            m = Matcher(db)
            s = m.stats()
            text = f"PeerID {s['peers']} 条（可精确命中）"
            if s["names"]:
                text += f"；{s['names']} 条只有名字（**不会告警**）"
            return text
        finally:
            db.close()

    check("匹配器", _matcher_check)

    # ---------------------------------------------------------- 数据来源
    def _plugin_dir_check():
        d = config.PLUGIN_LOG_DIR
        if not os.path.isdir(d):
            raise RuntimeError(f"找不到目录 {d}（游戏里装 HD2Tracker 了吗？）")
        return d

    check("插件日志目录", _plugin_dir_check)

    def _plugin_log_check():
        """文件在不在、末尾能不能解析、插件是不是够新。"""
        from app.watch.state import LogState

        p = config.player_log_path()
        if not os.path.exists(p):
            diag = config.plugin_diag_path()
            hint = ("；诊断日志存在，说明插件加载过但还没写下任何记录"
                    if os.path.exists(diag) else "；诊断日志也没有，插件没被加载")
            raise RuntimeError(f"还没有 {config.PLUGIN_LOG_NAME}{hint}")
        size = os.path.getsize(p)
        if size == 0:
            raise RuntimeError("文件是空的（插件刚加载，还没写东西？）")

        back = min(size, 256 * 1024)
        with open(p, "rb") as f:
            f.seek(size - back)
            data = f.read()
        lines = [ln for ln in
                 data.decode("utf-8", errors="replace").split("\n") if ln.strip()]
        if back < size and lines:
            lines = lines[1:]                # 首行可能是被截断的半行
        st = LogState()
        st.feed_lines(lines)
        if st.lines == 0:
            raise RuntimeError(f"末尾 {len(lines)} 行没有一行能解析（文件坏了？）")
        out = (f"{size} 字节；末尾 {len(lines)} 行解析成功 {st.lines} 行；"
               f"插件 {st.plugin_version or '未知'}")
        if st.plugin_outdated:
            out += f" ⚠ 版本过旧（需要 {config.PLUGIN_MIN_VERSION}）"
        if st.last_event:
            out += f"；最后事件 {st.last_event} @ {st.last_event_at or '?'}"
        return out

    check("插件日志 playerLog.txt", _plugin_log_check)

    def _seen_check():
        db = BlacklistDB()
        try:
            return (f"「最近遇到」可用：{config.RECENT_DAYS} 天内 "
                    f"{db.count_seen(days=config.RECENT_DAYS)} 人，"
                    f"累计 {db.count_seen()} 人；"
                    f"忽略名单 {len(db.ignored_peers())} 人")
        finally:
            db.close()

    check("最近遇到", _seen_check)

    def _ignore_check():
        """忽略名单：写进去 → 查得到 → 取消掉。用临时库，不动真库。"""
        import tempfile
        from app.core.database import BlacklistDB as DB

        fd, tmp = tempfile.mkstemp(suffix=".db", dir=config.LOG_DIR)
        os.close(fd)
        db = DB(tmp)
        try:
            db.record_seen("AABBCCDD00112233", "甲", game_pid=1)
            db.ignore_peer("aabbccdd00112233", "甲")
            if not db.is_ignored("AABBCCDD00112233"):
                raise RuntimeError("忽略后 is_ignored 仍然是 False")
            if db.count_seen() != 0:
                raise RuntimeError("忽略后「最近遇到」里还留着他")
            if len(db.ignored_peers()) != 1:
                raise RuntimeError("忽略名单条数不对")
            if not db.unignore_peer("AABBCCDD00112233"):
                raise RuntimeError("取消忽略失败")
            if db.is_ignored("AABBCCDD00112233"):
                raise RuntimeError("取消忽略后 is_ignored 仍然是 True")
            return "忽略 / 取消忽略都就绪（忽略后不再提醒、也不进列表）"
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    check("忽略名单", _ignore_check)

    def _peer_index_check():
        """PeerID 当身份的语义是不是真的生效了。"""
        import tempfile
        from app.core.database import BlacklistDB as DB
        from app.core.matcher import Matcher as M

        fd, tmp = tempfile.mkstemp(suffix=".db", dir=config.LOG_DIR)
        os.close(fd)
        db = DB(tmp)
        try:
            eid = db.add("自检玩家", peer_id="AABBCCDD00112233")
            m = M(db)
            hit = m.check_peer("aabbccdd 00112233")
            if hit is None or hit[0]["id"] != eid:
                raise RuntimeError("PeerID 没能命中（小写 / 带空格也不该漏）")
            if m.check(["自检玩家"]):
                raise RuntimeError("有 PeerID 的条目不该再被名字层命中")
            # 同一个人改名 → 还是同一条，只更新名字
            same = db.add("自检玩家改名了", peer_id="AABBCCDD00112233")
            if same != eid:
                raise RuntimeError("同一个 PeerID 不该新增第二条")
            if db.get(eid)["player_name"] != "自检玩家改名了":
                raise RuntimeError("同 PeerID 改名后名字没更新")
            # 同名、不同 PeerID → 是两个人，各留一条
            db.add("自检玩家改名了", peer_id="1111222233334444")
            if db.get_count() != 2:
                raise RuntimeError("同名不同 PeerID 应该各留一条")
            # 没有 PeerID 的条目：名字唯一
            db.add("没ID的人")
            try:
                db.add("没ID的人")
                raise RuntimeError("两条没 PeerID 的同名条目应该被挡住")
            except ValueError:
                pass
            return ("PeerID 唯一（改名只更新名字、同名不同 ID 各留一条）；"
                    "无 ID 条目按名字识别且不参与告警")
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    check("PeerID 身份语义", _peer_index_check)

    def _io_check():
        """导入导出接口自检：导出 1 条再原样导入（skip），确认不报错。"""
        import tempfile
        from app.core.database import BlacklistDB as DB
        tmp = os.path.join(config.LOG_DIR, "_io_check.db")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        db = DB(tmp)
        try:
            db.add("自检条目", "导入导出自检", peer_id="AABBCCDD00112233")
            rows = db.export_all()
            if len(rows) != 1:
                raise RuntimeError(f"导出条数异常: {len(rows)}")
            if rows[0].get("peer_id") != "AABBCCDD00112233":
                raise RuntimeError("导出没带上 peer_id")
            res = db.import_entries(rows, strategy="skip")
            if res["skipped"] != 1:
                raise RuntimeError(f"重复导入应全部跳过，实际 {res}")
            res2 = db.import_entries(
                [{"player_name": "新增", "note": "x"}], strategy="skip")
            if res2["inserted"] != 1:
                raise RuntimeError(f"新条目应被插入，实际 {res2}")
            res3 = db.import_entries([{"player_name": "", "note": "no name"}])
            if res3["skipped"] != 1:
                raise RuntimeError("没有名字的行应被跳过")
            return "export_all / import_entries 就绪（含 peer_id）"
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    check("名单导入导出", _io_check)

    def _symbol_check():
        """全符号玩家名（例如 `?`）必须能被索引、能命中。

        回归：旧实现把「去掉所有非字母数字」当作唯一归一化手段，
        `?` 会变成空串然后被整个丢出索引 —— 这类名字永远匹配不上。
        """
        import tempfile
        from app.core.database import BlacklistDB as DB
        from app.core.matcher import Matcher as M

        fd, tmp = tempfile.mkstemp(suffix=".db", dir=config.LOG_DIR)
        os.close(fd)
        db = DB(tmp)
        try:
            db.add("?")
            db.add("★")
            db.add("PlayerX")
            m = M(db)
            if m.reload() != 3:
                raise RuntimeError("三个名字没能全部进索引")
            if not m.check(["?"]):
                raise RuntimeError("黑名单里的 `?` 无法被 check() 命中")
            if not m.check(["？"]):                 # 全角也要能对上
                raise RuntimeError("全角 `？` 无法命中半角 `?` 条目")
            if not m.check(["★"]):
                raise RuntimeError("纯符号名 `★` 无法被命中")
            return "全符号玩家名（如 `?`）可索引、可命中"
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    check("全符号玩家名", _symbol_check)

    try:
        from app.notify.notifier import ensure_default_icon
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

    def _overlay_check():
        """无焦点提示浮层：样式必须齐全，否则会把游戏切到后台。"""
        from app.notify.notifier import _OverlayWindow, user32
        w = _OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                raise RuntimeError("分层窗口创建失败")
            GWL_EXSTYLE = -20
            style = user32.GetWindowLongW(w.hwnd, GWL_EXSTYLE)
            need = {"NOACTIVATE": 0x08000000, "TRANSPARENT": 0x00000020,
                    "TOOLWINDOW": 0x00000080, "TOPMOST": 0x00000008,
                    "LAYERED": 0x00080000}
            lack = [k for k, v in need.items() if not style & v]
            if lack:
                raise RuntimeError("缺少样式: " + ", ".join(lack))
            return "无焦点 Overlay 样式齐全（不抢游戏焦点）"
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


# ==========================================================================
def run_watch_debug(seconds: float = None) -> int:
    """前台实时打印尾随到的每一行与解析结果（不需要 GUI）。

    用来回答两个问题：**插件到底写没写**、**写的东西应用读不读得懂**。
    """
    from app.watch.watcher import PlayerLogWatcher

    path = config.player_log_path()
    print("=" * 78)
    print("插件日志实时调试")
    print("=" * 78)
    print(f"日志路径: {path}")
    print(f"文件存在: {os.path.exists(path)}"
          + (f"  大小: {os.path.getsize(path)} 字节"
             if os.path.exists(path) else ""))
    print("-" * 78)

    def on_events(events, live):
        tag = "实时" if live else "补读"
        for ev in events:
            extra = ("  " + "；".join(f"{p}={n}" for p, n in ev.squad)
                     if ev.kind == config.EVENT_SQUAD else "")
            print(f"[{tag}] {ev.ts_local or '?':19} {ev.kind:12} "
                  f"{ev.peer_id or '-':16} {ev.name or '-'}{extra}")
            if ev.note:
                print(f"        note: {ev.note}")

    watcher = PlayerLogWatcher(on_events=on_events, path=path)
    watcher.backfill()
    print(f"[状态] {watcher.status_text()}")
    print("-" * 78)
    print("继续实时尾随（Ctrl+C 退出）…")
    started = time.monotonic()
    last_squad = None
    try:
        while True:
            watcher.poll_once()
            squad = watcher.snapshot()["squad"]
            if squad != last_squad:
                last_squad = squad
                text = "；".join(f"{m['name'] or '(无名)'}" for m in squad)
                print(f"[队伍] {len(squad)} 人：{text or '（空）'}")
            if seconds is not None and (time.monotonic() - started) >= seconds:
                break
            time.sleep(config.WATCH_POLL_INTERVAL)
    except KeyboardInterrupt:
        print()
    print(f"[状态] {watcher.status_text()}")
    return 0


def run_replay(path: str) -> int:
    """离线回放一份日志：**不写数据库、不弹提示**，只看状态机会得出什么结论。

    用途：拿一份（合成的）日志验证解析、事件语义、去重口径，以及
    "如果当时应用开着，会告警几次"。不需要游戏在场。
    """
    from app.core.database import BlacklistDB
    from app.core.matcher import Matcher
    from app.watch.state import LogState

    if not path or not os.path.exists(path):
        print(f"文件不存在: {path}")
        return 1
    with open(path, "rb") as f:
        data = f.read()
    lines = [ln for ln in data.decode("utf-8", errors="replace").split("\n")
             if ln.strip()]

    st = LogState()
    events = st.feed_lines(lines)

    print("=" * 78)
    print(f"回放：{path}")
    print("=" * 78)
    print(f"行数 {len(lines)}　解析成功 {st.lines}　失败 {st.bad_lines}　"
          f"重复 {st.dup_lines}")
    print(f"插件 {st.plugin_version or '未知'}　过旧={st.plugin_outdated}")
    print(f"游戏 PID {st.game_pid}　我自己 {st.self_name or '未知'}"
          f"　self_ids={sorted(st.self_ids)}")
    print(f"事件统计 {st.counts}")
    print(f"当前队伍 {[m['name'] or m['peer_id'] for m in st.squad_list()]}")

    # 按 record_seen 的口径模拟"遇到过谁"：按 peer 归并，换一局才 +1
    seen: dict = {}
    for ev in events:
        members = ev.squad if ev.kind == config.EVENT_SQUAD else ()
        if members:
            for pid, nm in members:
                if st.is_self(pid):
                    continue
                _bump_seen(seen, pid, nm, ev.game_pid)
        elif (ev.has_person and not ev.is_self
              and ev.kind in (config.EVENT_JOIN, config.EVENT_LEAVE,
                              config.EVENT_UPDATE)):
            _bump_seen(seen, ev.peer_id, ev.name, ev.game_pid)

    print(f"\n遇到过 {len(seen)} 人：")
    for pid, r in sorted(seen.items(), key=lambda kv: (-kv[1]["games"],
                                                       kv[1]["name"])):
        print(f"  {pid}  {r['name'] or '(无名)':28} 遇到 {r['games']} 局")

    db = BlacklistDB()
    try:
        m = Matcher(db)
        s = m.stats()
        print(f"\n黑名单 {db.get_count()} 条（PeerID {s['peers']} / 名字 {s['names']}）")
        fired = 0
        for ev in events:
            if ev.kind != config.EVENT_JOIN or not ev.has_person or ev.is_self:
                continue
            hit = m.check_peer(ev.peer_id)
            source = "peer_join"
            if hit is None and ev.name:
                legacy = m.check([ev.name])
                if legacy:
                    hit, source = legacy[0], "name_join"
            if hit is None:
                continue
            fired += 1
            print(f"  [会告警·{source}] {ev.ts_local or '?'}  {ev.peer_id}  "
                  f"{ev.name or '-'}  →  #{hit[0]['id']} {hit[0]['player_name']}")
        print(f"合计会告警 {fired} 次")
    finally:
        db.close()
    return 0


def _bump_seen(seen: dict, peer_id: str, name: str, game_pid) -> None:
    """模拟 database.record_seen 的去重口径（只用于回放报告）。"""
    rec = seen.get(peer_id)
    if rec is None:
        seen[peer_id] = {"name": name or "", "games": 1, "pid": game_pid}
        return
    if name:
        rec["name"] = name
    if game_pid is not None and rec["pid"] is not None and game_pid != rec["pid"]:
        rec["games"] += 1
    if game_pid is not None:
        rec["pid"] = game_pid


def run_notification_preview() -> int:
    """离线渲染一张通知预览图（不需要显示器也能看效果）。"""
    config.enable_dpi_awareness()
    from PIL import Image
    from app.notify.notifier import render_overlay
    from app.settings.notification_config import NotificationConfig

    cfg = NotificationConfig().get()
    ap = cfg.get("appearance") or {}
    width = max(160, int(ap.get("width", 360)))
    height = max(60, int(ap.get("height", 100)))
    entry = {"player_name": "SamplePlayer_01",
             "note": "示例条目：疑似故意 TK 队友"}
    img = render_overlay(cfg, entry, 94.0, "peer_join", width, height)
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
        description=(f"{config.APP_NAME} —— 读取游戏内插件日志，"
                     "本地比对本机黑名单并给出无焦点提示"))
    parser.add_argument("--check", action="store_true",
                        help="只做环境自检，不启动 GUI")
    parser.add_argument("--watch-debug", action="store_true",
                        help="前台实时打印插件日志的每一行与解析结果")
    parser.add_argument("--replay", metavar="日志文件",
                        help="离线回放一份日志（不写库、不弹提示，只输出结论）")
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
    if args.watch_debug:
        return run_watch_debug()
    if args.replay:
        return run_replay(args.replay)
    if args.preview_notification:
        return run_notification_preview()

    # 必须在任何窗口之前开启 DPI 感知
    config.enable_dpi_awareness()
    # 把整个进程压到「低于正常」优先级：追踪线程只在空闲时间片里读文件，
    # 游戏线程永远优先拿到 CPU（见 priority.py）
    if config.GAME_FRIENDLY_PRIORITY:
        try:
            from app.core.priority import apply_game_friendly
            apply_game_friendly(config.PROCESS_PRIORITY)
        except Exception as e:                           # noqa: BLE001
            get_logger("app").warning("设置进程优先级失败: %s", e)
    # 设置 AppUserModelID：否则任务栏会把程序当成 python.exe，图标也不对
    try:
        from app.ui.theme import set_taskbar_identity
        set_taskbar_identity(config.APP_ID)
    except Exception:                                    # noqa: BLE001
        pass

    # ---- 单实例：同一时间只允许跑一个进程 ----
    # （命令行诊断模式已在上面 return，不受限制，方便程序开着时排查）
    from app.core.single_instance import SingleInstance, notify_existing_instance
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

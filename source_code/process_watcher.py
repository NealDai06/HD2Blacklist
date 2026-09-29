# -*- coding: utf-8 -*-
"""process_watcher.py —— 游戏进程监控。

主路径：WMI 事件订阅（Win32_ProcessStartTrace / Win32_ProcessStopTrace），零轮询。
    ⚠ Win32_Process*Trace 需要管理员权限，普通权限下会抛异常。

回退路径：权限不足或 WMI 不可用时，自动降级为 5 秒一次的工具快照轮询
    （ctypes 调 CreateToolhelp32Snapshot，开销极低），并写日志说明。

两条路径都会在启动时先做一次"当前是否已在运行"的判定，
避免"先开游戏再开工具"时错过 on_start。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

from config import TARGET_PROCESS_NAMES, get_logger

_POLL_FALLBACK_INTERVAL = 5.0

# ---------------------------------------------------------------- toolhelp
TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def list_process_names() -> set:
    """用 CreateToolhelp32Snapshot 列出所有进程名（小写）。失败返回空集合。"""
    names = set()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == _INVALID_HANDLE_VALUE or not snapshot:
            return names
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                names.add(entry.szExeFile.lower())
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:                                    # noqa: BLE001
        pass
    return names


class ProcessWatcher:
    """订阅游戏进程的启动 / 退出事件。"""

    def __init__(self, on_start=None, on_stop=None,
                 process_names=TARGET_PROCESS_NAMES,
                 poll_interval: float = _POLL_FALLBACK_INTERVAL):
        self.on_start = on_start
        self.on_stop = on_stop
        self.process_names = tuple(n.lower() for n in process_names)
        self.poll_interval = float(poll_interval)

        self.log = get_logger("process")
        self._stop = threading.Event()
        self._threads = []
        self._state_lock = threading.Lock()
        self._running = False          # 游戏是否在运行（去重后的状态）
        self.mode = "none"             # wmi-events / poll / none

    # ---------------------------------------------------------------- 状态
    def is_game_running(self) -> bool:
        with self._state_lock:
            return self._running

    def _set_state(self, running: bool, reason: str) -> None:
        """状态变化时才回调，避免重复触发。"""
        with self._state_lock:
            if self._running == running:
                return
            self._running = running
        self.log.info("游戏进程%s（%s）", "已启动" if running else "已退出", reason)
        try:
            if running:
                if self.on_start:
                    self.on_start()
            else:
                if self.on_stop:
                    self.on_stop()
        except Exception:                                # noqa: BLE001
            self.log.exception("进程回调异常")

    def check_now(self) -> bool:
        """立刻检查一次当前状态（用于程序启动时的初始判定）。"""
        running = bool(list_process_names() & set(self.process_names))
        self._set_state(running, "初始检查")
        return running

    # ---------------------------------------------------------------- 启动
    def start(self):
        """启动监控。**阻塞**，请放到守护线程里运行。"""
        self.log.info("进程监控启动，目标=%s", ", ".join(self.process_names))
        # 初始状态判定：解决"先开游戏再开工具"
        self.check_now()

        if self._try_wmi_events():
            self.mode = "wmi-events"
            # 兜底巡检：WMI 事件偶尔会漏，30 秒对账一次（不是主路径）
            t = threading.Thread(target=self._reconcile_loop, daemon=True,
                                 name="ProcReconcile")
            t.start()
            self._threads.append(t)
        else:
            self.mode = "poll"
            self.log.warning("WMI 事件订阅不可用，降级为 %.0fs 轮询", self.poll_interval)
            t = threading.Thread(target=self._poll_loop, daemon=True,
                                 name="ProcPoll")
            t.start()
            self._threads.append(t)

        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ WMI 事件
    def _try_wmi_events(self) -> bool:
        try:
            import pythoncom                                # noqa: F401
            import wmi                                      # noqa: F401
        except ImportError as e:
            self.log.warning("wmi/pywin32 未安装: %s", e)
            return False

        probe_ok = threading.Event()
        probe_error = {}

        def probe():
            import pythoncom
            pythoncom.CoInitialize()
            try:
                wmi_obj = wmi.WMI()
                # watch_for 只构造对象，真正的 WQL 查询在 __call__ 时才执行，
                # 所以必须真调一次才能判定是否有权限。
                watcher = wmi_obj.Win32_ProcessStartTrace.watch_for(
                    notification_type="Creation"
                )
                try:
                    watcher(timeout_ms=1)
                except wmi.x_wmi_timed_out:
                    pass                     # 查询成功，只是这段时间没有事件
                probe_ok.set()
            except Exception as e:                          # noqa: BLE001
                probe_error["err"] = e
            finally:
                pythoncom.CoUninitialize()

        t = threading.Thread(target=probe, daemon=True, name="WmiProbe")
        t.start()
        t.join(timeout=15.0)
        if not probe_ok.is_set():
            self.log.warning("WMI 事件探测失败: %s",
                             probe_error.get("err", "超时"))
            return False

        for kind in ("start", "stop"):
            th = threading.Thread(target=self._wmi_watch_loop, args=(kind,),
                                  daemon=True, name=f"WmiWatch-{kind}")
            th.start()
            self._threads.append(th)
        return True

    def _wmi_watch_loop(self, kind: str) -> None:
        import pythoncom
        import wmi
        pythoncom.CoInitialize()
        try:
            wmi_obj = wmi.WMI()
            trace = (wmi_obj.Win32_ProcessStartTrace if kind == "start"
                     else wmi_obj.Win32_ProcessStopTrace)
            watcher = trace.watch_for(
                notification_type="Creation" if kind == "start" else "Deletion"
            )
            while not self._stop.is_set():
                try:
                    event = watcher(timeout_ms=1000)
                except wmi.x_wmi_timed_out:
                    continue
                except Exception as e:                      # noqa: BLE001
                    self.log.warning("WMI %s 监听异常: %s", kind, e)
                    time.sleep(1.0)
                    continue
                if event is None:
                    continue
                name = (getattr(event, "ProcessName", "") or "").lower()
                if name not in self.process_names:
                    continue
                self._set_state(kind == "start", f"WMI:{name}")
        except Exception as e:                              # noqa: BLE001
            self.log.exception("WMI %s 监听线程结束: %s", kind, e)
        finally:
            pythoncom.CoUninitialize()

    # -------------------------------------------------------------- 回退
    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            running = bool(list_process_names() & set(self.process_names))
            self._set_state(running, "轮询")

    def _reconcile_loop(self) -> None:
        """事件模式的兜底对账：只在状态可能漂移时纠偏。"""
        while not self._stop.wait(30.0):
            running = bool(list_process_names() & set(self.process_names))
            if running != self.is_game_running():
                self._set_state(running, "对账")

# HD2 黑名单 · 绝地潜兵2 TK 者黑名单提示工具

一个 **纯本地** 的 Windows 桌面小工具：自动监视《绝地潜兵2》(Helldivers 2) 的
聊天框与玩家列表，用 **屏幕截图 + OCR + 本地黑名单比对** 的方式，
在你遇到记录在案的 TK 玩家时，用 **不抢焦点** 的音效 + 半透明 Overlay 小窗提醒你。

> **不注入进程 · 不读内存 · 不改包 · 不抢焦点 · 不打断游戏节奏**

---

## 目录

- [1. 安全声明与风险提示](#1-安全声明与风险提示)
- [2. 环境要求](#2-环境要求)
- [3. 安装](#3-安装)
- [4. 快速开始](#4-快速开始)
- [5. 触发扫描的方式](#5-触发扫描的方式)（聊天框已改为**按需扫描** + 30 秒去重 + 批量通知）
- [6. 界面说明](#6-界面说明)
- [7. 校准监视区域](#7-校准监视区域)
- [8. 名单导入导出](#8-名单导入导出)
- [9. 自定义提示（GUI 可视化）](#9-自定义提示gui-可视化)
- [10. 数据文件与目录](#10-数据文件与目录)
- [11. 数据库结构](#11-数据库结构)
- [12. 自检与调试命令](#12-自检与调试命令)
- [13. 打包成 exe](#13-打包成-exe)
- [14. 项目结构与架构](#14-项目结构与架构)
- [15. 资源占用设计](#15-资源占用设计)
- [16. 常见问题](#16-常见问题)
- [17. 验收标准对照](#17-验收标准对照)
- [18. 测试](#18-测试)

---

## 1. 安全声明与风险提示

本工具的技术手段完全被动：

| 做 | 不做 |
|---|---|
| 屏幕截图（mss） | ❌ 不注入游戏进程 |
| 本地 OCR（RapidOCR / onnxruntime） | ❌ 不读写游戏内存 |
| 本地 SQLite 比对（rapidfuzz） | ❌ 不修改 / 拦截网络包 |
| 无焦点分层窗口提示 | ❌ 不同游戏服务器发送任何请求 |
| 全局 `GetAsyncKeyState` 轮询 ESC | ❌ 不安装键盘钩子（Hook） |
| 全局热键 `RegisterHotKey`（聊天框扫描键） | ❌ 不注入进程 / 不读游戏内存 |
| | ❌ 不自动踢人 / 不自动发消息 / 不操作游戏 |

> ⚠️ **风险提示**：尽管本工具不注入、不读内存、不改包，
> **任何第三方自动化工具都可能违反游戏的服务条款（ToS）**。
> 是否使用、以及由此产生的任何后果，请自行评估与承担。

提示窗口使用 `WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST`：

- 不会夺取游戏焦点（不会让你突然无法操作鼠标/键盘）
- 鼠标点击会 **穿透** 提示窗口，不会挡住你的操作
- 不会出现在 Alt-Tab 列表里

---

## 2. 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（64 位） |
| Python | **推荐 3.10 ~ 3.12** |
| 游戏显示模式 | **必须是无边框窗口（Borderless Windowed）** |
| 权限 | 普通用户即可（使用 WMI 事件订阅需要管理员，见下方说明） |

### 关于 Python 3.13

本规范指定的 OCR 引擎 `rapidocr-onnxruntime`，其元数据标注 `Requires-Python <3.13`，
但该包本身是 **纯 Python wheel**，实测在 **Python 3.13.5 上可以正常运行**。

`requirements.txt` 已按版本自动分流：

```
rapidocr-onnxruntime>=1.3.24; python_version < "3.13"
rapidocr>=3.0.0;             python_version >= "3.13"
```

`ocr_engine.py` 同时兼容这两个包的 API。如果你在 3.13 上遇到依赖解析很慢，
可以直接使用规范指定的引擎（已验证）：

```bat
pip install --ignore-requires-python rapidocr-onnxruntime==1.4.4
pip install --only-binary=:all: opencv-python-headless pyclipper shapely PyYAML
python main.py --check
```

> **实测参考（Python 3.13.5，RapidOCR 1.4.4）**：
> 引擎加载 0.4 s；420×160 聊天框区域单次识别 **约 0.3 s**；
> 400×120 玩家列表（3 行）**约 0.42 s**；
> 中英文置信度均在 **0.98 ~ 1.00**。
>
> 无论如何，**推荐 Python 3.10 ~ 3.12**，兼容性最好。

### 关于全屏独占模式

游戏若运行在 **全屏独占（Fullscreen）** 模式，Windows 会阻止任何窗口绘制在其之上，
截图也会拿到黑屏。请务必在游戏设置里改成 **无边框窗口**。

### 关于管理员权限

进程监控优先使用 WMI 事件订阅（`Win32_ProcessStartTrace` / `Win32_ProcessStopTrace`），
**零轮询**。该接口在部分系统上需要管理员权限；若权限不足，
程序会 **自动降级** 为 5 秒一次的进程快照轮询，并在日志中写明。GUI 与其余功能不受影响。

---

## 3. 安装

```bat
:: 1) 建议先建虚拟环境
python -m venv .venv
.venv\Scripts\activate

:: 2) 安装依赖
pip install -r requirements.txt

:: 3) 环境自检（强烈建议先跑一次）
python main.py --check
```

`--check` 会逐项检查依赖、数据目录、数据库、屏幕捕获、OCR 引擎、
无焦点 Overlay 样式、提示音，并明确告诉你哪一项不通过。

---

## 4. 快速开始

```bat
python main.py
```

第一次启动会自动生成：

- `data/blacklist.db`（SQLite 数据库，WAL 模式）
- `data/notification.json`（提示外观示例）
- `data/user_config.json`（区域配置示例）
- `data/assets/default_icon.png`（默认提示图）
- `data/evidence/`（命中时的证据截图）
- `data/logs/app.log`（运行日志）

然后：

1. 在游戏里把显示模式设成 **无边框窗口**
2. 点工具栏 **[校准区域]**，依次框选三个监视区域
3. 点 **[添加]** 录入你要记录的黑名单玩家
4. 启动游戏 —— 监视会在检测到 `helldivers2.exe` 后自动激活
5. 想扫聊天框时，点工具栏 **[扫描聊天框]**（或按 F8，需先启用）

---

## 5. 触发扫描的方式

程序共有 **三个触发源**：

### 5.1 聊天框扫描（**按需触发**）

聊天框 **不再是持续扫描**，改成完全由你决定什么时候扫：

| 触发方式 | 说明 |
|---|---|
| 工具栏 **[扫描聊天框]** 按钮 | 点一次 → 抓一张聊天框截图 → 跑一次 OCR → 匹配黑名单 |
| **自定义热键**（可选，默认关闭） | 设置 → 聊天框扫描快捷键 → **自定义快捷键…** |

#### 自定义扫描热键（系统全局热键）

默认是 **F8**，可随时改成任意组合键：

- 打开 **设置 → 聊天框扫描快捷键 → 自定义快捷键…**
- **点一下按键框，然后直接按下你想用的组合键**（例如 `Ctrl+F9`、`Shift+S`）
- 只按 `Ctrl` / `Alt` / `Shift` 会切换对应的复选框，不会被当成主键
- 支持 F1–F24、A–Z、0–9、小键盘、以及常用符号键
- 保存后 **立即生效**（无需重启），并持久化到 `data/hotkey.json`

热键通过 `RegisterHotKey` 注册成 **系统全局热键**：由系统投递 `WM_HOTKEY`
到本进程的隐藏消息窗口，**与前台窗口是谁无关**，所以：

- 游戏里（前台全屏 / 高权限运行）按 F8 也能触发 ——
  这正是它比 `GetAsyncKeyState` 轮询强的地方（轮询在游戏前台时经常读不到状态，
  表现为「只有本程序窗口在前台才有效」）；
- 注册成功后该按键被系统**独占**，不再下发给前台程序，
  所以请避开游戏内的按键；
- 万一被别的程序占用（或选了系统保留键，如 `F12`），程序会**自动退回按键
  轮询模式**（轮询模式下只有本程序前台时有效），状态栏与日志会写明原因；
- 仍然 **不是键盘钩子、不注入任何进程** —— 只是向系统登记了一个热键。

匹配规则：

| 修饰键 | 规则 |
|---|---|
| Ctrl / Alt | **必须完全一致** —— 绑了就得按，没绑就不能按 |
| Shift | **只有绑定时才要求** —— 所以游戏中按着 Shift 跑动不会影响 `F8` 这类热键 |

> ESC 已被"菜单扫描"占用，**不允许绑定**，会直接提示换一个键。
> 建议避开游戏内按键（Q / E / 空格等），避免冲突。

```json
{
  "enabled": true,
  "vk": 120,
  "ctrl": true,
  "alt": false,
  "shift": false,
  "name": "Ctrl+F9"
}
```

关键特性：

- **没有 1 秒 tick、没有 10 秒定期 OCR、没有哈希缓存** —— 不点就一点开销都没有
- `_busy` 非阻塞锁：连点按钮时，后一次会立刻返回「上一次扫描尚未完成，跳过」，
  **不会并发跑 OCR**
- OCR 在后台线程执行，不阻塞界面；结果回到状态栏显示
  （`命中 N 条（xx ms）` / `未命中` / `聊天框为空`）
- 手动扫描是用户主动行为，**不要求游戏正在运行**（方便先调试坐标）

### 5.2 命中去重（30 秒窗口）

同一玩家在 `HIT_DEDUP_WINDOW`（默认 **30 秒**）内：

- **只计入一次** `encounter_count`
- **只弹一次提示**

这样可以放心连点扫描按钮，不会把「遇到次数」刷上天。
去重缓存只在内存里，程序重启即清空；也可以在
设置 → 聊天框扫描快捷键 → **清空命中去重缓存** 手动清掉。
被跳过的命中会写日志：`[Hit] 去重跳过 entry_id=X source=Y`。

### 5.3 ESC 菜单扫描

- 每 100 ms 轮询 `GetAsyncKeyState(VK_ESCAPE)`（**不使用键盘钩子**），只在上升沿触发，去抖 1 秒
- 按下后：先终止旧会话 → 等 0.6 秒菜单动画 → 判断菜单 **是否真的打开了**
- 菜单打开 → 启动扫描会话；判定成「游戏画面」就跳过（避免频繁按 ESC 造成无意义扫描）
- 判不准时最多复查 3 次（间隔 0.35 秒，躲开菜单淡入动画）；**复查完仍判不准就按"已打开"处理**
  —— 多扫一次最多浪费几帧低优先级 OCR，漏扫则等于功能失效

**判定依据（这里修过一个把 ESC 扫描几乎废掉的 bug）：**

早先只看「区域灰度标准差 > 20」，假设"菜单打开后画面更复杂"。实测真实截图后这个假设是错的：

| 画面 | 平均亮度 | 灰度标准差 |
|---|---|---|
| ESC 菜单**开着**（玩家名清晰可见，7 张实测） | 10 ~ 36 | 9.6 ~ 23.3 |
| ESC 菜单**没开**（游戏画面 / 别的窗口） | 140 | 93 |

阈值 20 正好压在前者的中间 → 菜单明明开着也有一半概率被判成"没开"，**连扫描都不启动**。
现在改成看 **平均亮度**（`ESC_MENU_MEAN_MAX = 90`：暗色面板 = 菜单开着），
标准差只用来排除"纯黑一片、根本没有文字"的情况。

### 5.4 冷启动扫描

- 检测到游戏进程启动后 **12 秒**，自动扫一次 HUD 玩家列表
- 扫到名字就收工（`keep_alive_after_hit = 0`）

### 5.5 扫描会话的生命周期

所有「玩家列表」类扫描都以 **ScanSession** 形式管理，三个终止条件（满足任一即停）：

| 终止条件 | 参数 | 默认值 |
|---|---|---|
| 连续 N 次没有识别到有效结果 | `max_consecutive_empty` | ESC 4 / 冷启动 3 |
| 总时长超过上限 | `max_duration` | ESC 8 秒 / 冷启动 15 秒 |
| 距离上一次「发现新名字」超过 X 秒 | `keep_alive_after_hit` | ESC 3.5 秒 / 冷启动 0 秒 |

> 第 3 条对应验收项「ESC 打开玩家列表，会话持续到 **滚动结束后 3 秒** 终止」——
> 只要你还在滚动列表看到新名字，会话就继续；停下几秒后自动收工。
>
> **同一时刻最多只有一个活跃会话**：再次触发会先终止旧会话。

### 5.6 批量命中 → 每个玩家一个通知栏 + 只播一次音效

一次扫描同时命中多个黑名单玩家时：

- 每个玩家 **各自一个通知栏**（**每个通知栏一个独立的无焦点窗口**），垂直堆叠、互不遮挡
- **音效只播一次**（不会 N 个玩家响 N 下）
- 每个玩家的 `encounter_count` **各自 +1**，Treeview 里 **各自闪烁**
- 堆叠上限 `MAX_NOTIFY_STACK = 5`：超过上限时 **最后一个槽位换成汇总栏**，
  把剩下的玩家名都列出来（例如 `等 3 名：Player5、Player6、Player7`），
  保证"命中了谁"永远是看得见的
- 做了屏幕上下沿保护：放不下的通知栏会被跳过，绝不会被推到屏幕外
- 一次命中的**整批名单**会打进 `data/logs/app.log`（`[Hit] 本批共命中 N 名黑名单玩家：…`），方便事后核对

> **这里修过一个真 bug**：所有通知栏原先共用 **一个** `WS_EX_LAYERED` 窗口，
> 而 `UpdateLayeredWindow` 会把整个窗口重绘成新图并搬走它 —— 后画的把先画的整个盖掉，
> 用户最终只看得见最后一条；音效又只响一次，于是现象就是
> 「一次命中多个黑名单玩家，却只播报了一个人，还说不清是哪一个」。
> 现在每个堆叠槽位一个窗口（`_OverlaySurface`），N 条就真的并排显示 N 栏。

---

## 6. 界面说明

### 工具栏

工具栏分三行（ttk 按钮较宽，挤一行会被裁掉）：

| 行 | 按钮 | 说明 |
|---|---|---|
| 1 | 添加 / 编辑 / 删除 | 黑名单增删改 |
| 1 | 搜索 / 清空 | 按 玩家ID / 名称 / 备注 模糊查询（大小写不敏感） |
| 2 | 排序下拉 + ↓↑ | 按 最后遇见 / 遇到次数 / 添加时间 / TK次数 排序（点列头也可） |
| 2 | **扫描聊天框** | **按需扫描一次聊天框**（抓图 + OCR + 黑名单匹配） |
| 2 | **导入 / 导出** | 名单备份与迁移（CSV / JSON） |
| 3 | 校准区域 | 打开区域校准器 |
| 3 | 恢复全部默认 | 清除全部区域自定义 |
| 3 | 通知设置 | 打开通知外观编辑器 |
| 3 | 最小化到托盘 | 隐藏窗口，**监控继续运行** |

菜单里也有对应入口：**文件 → 导出列表…**、
**设置 → 聊天框扫描快捷键**（立即扫描 / 开启 F8 / 清空去重缓存）。

### 列表列

玩家ID · 名称 · 备注 · TK次数 · 遇到次数 · 添加时间 · 最后遇见

### 界面外观

内置一套 **暗色主题**（`theme.py`），配色取自应用图标（白/金/红）：

| 元素 | 处理 |
|---|---|
| 整体底色 | 深炭灰 `#1b1e23`，面板 `#23272e` |
| 强调色 | 金色 `#d9b44a`（表头、标题、主按钮、焦点框） |
| 主操作按钮 | `[扫描聊天框]` 用金色实底突出 |
| 危险操作 | `[删除]` 用红色文字 |
| 表格 | 斑马纹行背景、金色表头、命中行深红闪烁 |
| 顶部标题带 | 应用图标 + 名称 + 版本 + 监控状态灯 |
| 其他 | 菜单、下拉列表、滚动条、输入框、分页全部统一配色 |

对话框（通知设置 / 校准器 / 添加条目 / 导入策略）共用同一套配色与图标。
**只改外观，不改任何行为**；`Hit.TLabel` / `Paused.TLabel` 等 style 名保持不变。

### 应用图标

图标放在 `data/assets/`：

| 文件 | 用途 |
|---|---|
| `app_icon.png` | 窗口图标、任务栏图标、标题带、系统托盘 |
| `app_icon.ico` | exe 图标、任务栏小图标（多尺寸 16→256） |

程序启动时会调用 `SetCurrentProcessExplicitAppUserModelID`，
所以任务栏会把它当独立程序、正确显示图标，而不是显示 python.exe 的图标。

想换成自己的图：直接替换 `data/assets/app_icon.png`（正方形最好），
再跑一次 `python build.py` 就会重新生成 `.ico`；只换 PNG 不重新打包也能生效。

### 右键菜单

编辑 · 删除 · **清零遇到次数** · **重置最后遇见时间**

### 底部汇总栏

`黑名单总数：N | 本局命中：N | 今日命中：N`

### 命中时的实时反馈

命中黑名单后 **100 ms 内**：

1. 对应行的「遇到次数」+1、「最后遇见」更新（每个命中玩家各自一行）
2. 该行高亮 **闪烁约 3 秒** 后恢复
3. 弹出无焦点 Overlay + 提示音（多个命中 → 多个通知栏，但音效只响一次）
4. 状态栏显示命中的玩家名、来源与匹配度

### 字段语义

| 字段 | 谁在维护 | 说明 |
|---|---|---|
| TK次数 | **用户手动录入** | 你主观记录的 TK 次数 |
| 遇到次数 | **系统自动累积** | 扫描命中该玩家的累计次数，每次命中 +1 |
| 最后遇见 | **系统自动更新** | 最近一次命中的时间 |

> **去重窗口内的命中既不计入「遇到次数」也不弹提示**（默认 30 秒）——
> 这是为了让你可以放心连点 [扫描聊天框] 而不会把次数刷上天。

---

## 7. 校准监视区域

**设置 → 校准区域**（或工具栏 **[校准区域]**）。

三个区域：

| 区域键 | 名称 | 用途 |
|---|---|---|
| `chat_event` | 聊天框事件区域 | 检测玩家加入/离开的提示文字 |
| `player_list_hud` | HUD 玩家列表 | 任务中显示的队友名称区域 |
| `menu_player_list` | ESC 菜单玩家列表 | 按 ESC 后左上角玩家信息区域 |

操作：

- **[框选新区域]** —— 屏幕变暗，按住左键拖拽框出矩形，实时显示 `x / y / 宽×高`；ESC 取消
- **[恢复默认]** / **[恢复全部默认]** —— 清除用户自定义，回到内置默认坐标
- **[实时预览]** —— 立即截取该区域当前画面，确认框得对不对

默认坐标以 **1920×1080 无边框窗口、UI 缩放 100%** 为基准：

```json
{
  "chat_event":       {"left": 20, "top": 720, "width": 420, "height": 160},
  "player_list_hud":  {"left": 40, "top": 200, "width": 400, "height": 120},
  "menu_player_list": {"left": 60, "top": 120, "width": 500, "height": 300}
}
```

其它分辨率 / 缩放比例 **必须重新校准**。坐标保存在 `data/user_config.json`，
支持多显示器（含负坐标）。程序启动时会开启 Per-Monitor DPI 感知，
保证截图区域与 Overlay 位置在缩放显示器上依然准确。

---

## 8. 名单导入导出

工具栏 **[导出]** / **[导入]**，或菜单 **文件 → 导出列表…**。
用于**备份、换机迁移、多机同步**。

### 支持格式

| 格式 | 编码 | 适用 |
|---|---|---|
| **CSV** | `utf-8-sig`（带 BOM） | Excel 直接双击打开**不乱码**；也可用记事本编辑 |
| **JSON** | `utf-8` | 结构化，便于脚本处理与人工核对 |

导出字段：`player_id, player_name, note, tk_count, encounter_count, created_at, last_seen`

JSON 结构：

```json
{
  "version": 1,
  "exported_count": 2,
  "fields": ["player_id", "player_name", "note", "tk_count",
             "encounter_count", "created_at", "last_seen"],
  "entries": [
    {
      "player_id": "76561198000000001",
      "player_name": "SamplePlayer_01",
      "note": "示例条目（可删除）：疑似故意 TK 队友",
      "tk_count": 2,
      "encounter_count": 1,
      "created_at": "2026-01-01 10:00:00",
      "last_seen": "2026-01-02 11:00:00"
    }
  ]
}
```

> 导入时也接受**裸数组** `[{...}, {...}]`，以及 `entries` / `blacklist` /
> `items` / `data` 任一字段包裹的数组。

### 导入冲突策略

以 `(player_id, player_name)` 为唯一键。导入前会弹窗让你选：

| 策略 | 行为 |
|---|---|
| **跳过已存在的条目**（默认） | 只新增数据库里没有的；同名同 ID 的跳过 |
| **更新备注与 TK 次数** | 保留本机已累积的 `遇到次数` / `最后遇见`，只覆盖 `note` / `tk_count` |
| **完全覆盖** | 连 `encounter_count` / `last_seen` 也一起用文件里的值覆盖 |

导入完成后弹窗显示：**新增 N，更新 M，跳过 K**。

### 规则与保障

- `player_id` 为空的行 **一律跳过**（计入"跳过"数）
- 同一 `player_id` 但 `player_name` 不同 → 视为**两条不同记录**（与数据库唯一键一致）
- 整个导入在 **一个事务** 内完成：任何一条出错 → **全部回滚**，数据库保持原样
- 数字字段容错：`"abc"` / 空值 → 记为 `0`
- **导入导出都在后台线程执行**，大文件不会卡住界面；完成后通过
  `queue.Queue` 回到主线程刷新列表并提示

> ⚠️ 导出文件里包含你的完整黑名单，注意保管。

---
---

## 9. 自定义提示（GUI 可视化）

**设置 → 通知设置**（或工具栏 **[通知设置]**）。左侧四个分页，右侧 **实时预览**。

### 文案

可用占位符：

| 占位符 | 含义 |
|---|---|
| `{player_name}` | 玩家名称 |
| `{match_score}` | 匹配度（0-100） |
| `{note}` | 备注描述 |
| `{tk_count}` | 你录入的 TK 次数 |
| `{source}` | 命中来源（chat / chat_trigger_join / esc_menu / cold_start） |
| `{time}` | 当前时间 |
| `{last_seen}` | 上次遇见时间 |

「字段显示开关」可以关掉某些字段：**正文模板里只包含该字段的整行会被自动去掉**。

### 图片

`使用默认提示图` / `使用自定义图片` / `不显示图片`，可设显示尺寸。
自定义图片按 mtime 缓存，改文件后自动重新加载，不会每次弹窗都读盘。

点 **[浏览…]** 时文件对话框默认停在 **`data/assets/`**；选中后会**自动复制一份**
进 `data/assets/`，配置里记的就是这份副本 —— 原图以后被移走、删掉、U 盘拔掉，
提示图也不会变空白，而且下次再选直接在默认目录里就能找到。

- 支持 `PNG / JPG / JPEG / BMP / GIF`
- 选中的文件本来就在 `data/assets/` 里 → 不重复复制
- 同名且内容相同的文件 → 直接复用，不会越选越多
- 同名但内容不同 → 存成 `xxx (2).png`，不覆盖旧的
- 单个文件上限 64 MB（防止误选超大文件卡住界面）；复制失败只弹警告，
  仍会直接引用原文件

### 外观

背景色、文字色、标题色（带取色器与色块预览）、透明度、宽高、位置（7 种）、
显示器（0 = 整个虚拟桌面，可覆盖副屏）、显示时长。

#### 文字大小（标题 / 正文独立可调）

「外观」页里有 **文字大小** 分组：

| 控件 | 说明 |
|---|---|
| 标题字号 | `appearance.title_font_size`，8 ~ 72 像素，默认 **16** |
| 正文字号 | `appearance.body_font_size`，8 ~ 72 像素，默认 **13** |
| 快捷按钮 | 小 (13/11) · 标准 (16/13) · 大 (22/18) · 特大 (30/24) |

- 直接输入数字或点快捷按钮，右侧 **实时预览立刻跟着变**
- 内边距、行高、图标对齐都会跟着字号一起缩放，**不用手动调窗口尺寸**
- 窗口高度是 **下限**：字号变大或正文过长时自动增高，**绝不会截断文字**；
  宽度不够时正文自动换行
- 非法输入（比如打了字母）自动回退默认字号；超范围自动夹到 8 ~ 72

```json
{
  "appearance": {
    "title_font_size": 22,
    "body_font_size": 18
  }
}
```

> 字号也影响批量命中的堆叠间距（`高度 + 8`），所以调大字号后
> 多个通知栏依然不会互相遮挡。

### 音效

| 模式 | 说明 |
|---|---|
| `beep` | 系统 Beep，可调频率（37–32767 Hz）与时长（30–2000 ms） |
| `wav` | 自定义音频文件（pygame.mixer / SDL2_mixer 播放），可 **[试听]** |
| `none` | 静音 |

自定义音频支持 **WAV / MP3 / OGG / FLAC**（打包后的 exe 用同一套 SDL2_mixer，
四种格式同样可用）。和自定义图片一样：对话框默认停在 `data/assets/`，
选完自动复制一份进去，同名不同内容存成 `xxx (2).mp3`。

> 配置键里模式值仍然叫 `wav`（历史命名），它现在的含义是「自定义音频文件」。
> WAV 建议 16bit PCM 44.1kHz。
>
> **[试听]** 会先检查文件是否存在，路径为空或文件已删掉会直接提示，
> 不会再出现「点了没反应」。

音效播放始终在独立线程，**绝不阻塞**扫描线程。

### 按钮

- **[实时预览]** —— 真的弹一次无焦点 Overlay
- **[测试通知]** —— 用假数据完整走一遍（Overlay + 音效）
- **[恢复默认]** —— 删除 `data/notification.json`
- **[保存]** / **[取消]**

配置采用 **深合并**，你只管在 `data/notification.json` 里写想改的字段，其余自动用默认值：

```json
{
  "appearance": { "opacity": 0.6, "position": "bottom_right" },
  "sound": { "mode": "wav", "path": "D:/sounds/alert.mp3" }
}
```

JSON 格式错误 → 回退默认，不崩溃；删除文件后重启即恢复默认。

---

## 10. 数据文件与目录

**仓库根目录就是 `source_code/`** —— GitHub 上能看到的一切（源码 / 测试 / 文档 / 许可证）
全都在这个文件夹里；分发包在工作区上一层，不进仓库。

```
Helldiver_black/                     ← 工作区（不是一个 git 仓库）
│
├── source_code/                     ← ★ 仓库根目录（git 仓库、源码、文档都在这）
│   ├── .git/                        仓库本体
│   ├── .gitignore  LICENSE  README.md
│   ├── 使用说明.txt                  分发给用户看的说明（打包时复制进发布包）
│   ├── requirements.txt
│   ├── main.py                      入口薄壳（把仓库根塞进 sys.path 后转交 app.application）
│   ├── build.py                     PyInstaller 打包脚本（产物输出到 ../发布包/）
│   │
│   ├── app/                         ★ 应用代码（全部在这里，根目录不再平铺模块）
│   │   ├── config.py                常量、路径解析、日志工厂、DPI 感知
│   │   ├── application.py           应用装配（HD2BlacklistApp + 自检 + 命令行）
│   │   ├── core/                    基础设施层
│   │   │   ├── database.py          SQLite（WAL，原子命中更新 + 导入导出）
│   │   │   ├── matcher.py           精确 / 易混字符 / 模糊 / 符号层 四层匹配
│   │   │   ├── priority.py          游戏友好优先级（进程 below_normal + 线程 lowest）
│   │   │   ├── single_instance.py   单实例互斥（命名 Mutex，重复打开叫回已有窗口）
│   │   │   ├── process_watcher.py   WMI 事件订阅 + 轮询降级
│   │   │   └── assets.py            data/assets：默认目录、格式白名单、选完复制一份
│   │   ├── settings/                配置读写层
│   │   │   ├── region_config.py     监视区域（默认 + 自定义 + 过小告警）
│   │   │   ├── notification_config.py  提示外观（深合并，热重载）
│   │   │   └── hotkey_config.py     扫描热键绑定（可自定义 + 按键名映射）
│   │   ├── capture/                 采集层
│   │   │   ├── screen_capture.py    mss 小区域截图（按线程缓存实例）
│   │   │   └── ocr_engine.py        RapidOCR 封装（懒加载 + 预处理）
│   │   ├── scanning/                扫描层
│   │   │   ├── scan_session.py      扫描会话（三个终止条件）+ 玩家名过滤器
│   │   │   ├── scan_scheduler.py    调度中枢（去重 / 批量 / 会话 / 证据 / 日志）
│   │   │   ├── chat_scanner.py      聊天框**按需**扫描（点按钮才跑，无循环无定时）
│   │   │   ├── chat_hotkey.py       聊天框热键（系统全局 RegisterHotKey + 轮询兜底）
│   │   │   └── esc_trigger.py       ESC 触发 + 菜单打开判定
│   │   ├── notify/
│   │   │   └── notifier.py          无焦点分层窗口 Overlay（每栏一窗）+ 音效 + 堆叠
│   │   └── ui/
│   │       ├── theme.py             暗色主题（配色 / ttk 样式 / 窗口与托盘图标）
│   │       ├── gui.py               主界面（Treeview + 三行工具栏 + 托盘 + 导入导出）
│   │       ├── gui_notification.py  通知设置对话框
│   │       ├── calibrator.py        区域校准器
│   │       └── hotkey_dialog.py     快捷键设置对话框（按键捕获）
│   │
│   ├── tests/                       单元 / 集成 / GUI 测试（514 个用例）
│   │   ├── test_core.py             配置 / 数据库 / 匹配（含符号名与易混字符）/ 导入导出
│   │   ├── test_pipeline.py         截图 / OCR / 会话 / 按需扫描 / 去重 / 批量 / ESC
│   │   ├── test_notifier.py         模板 / 渲染 / 无焦点窗口池 / 音效 / 堆叠 / 字号
│   │   ├── test_gui.py              主界面 / 通知设置 / 校准器 / 导入导出 GUI
│   │   ├── test_assets.py           data/assets：默认目录 / 复制 / 去重 / 改名 / 上限
│   │   ├── test_theme.py            配色 / ttk 样式 / 应用图标 / 对比度
│   │   ├── test_hotkey.py           快捷键配置 / 组合键判定 / 按键捕获 / 全局热键
│   │   ├── test_single.py           单实例互斥 / 唤醒已有窗口
│   │   ├── test_priority.py         进程与线程优先级
│   │   └── test_e2e.py              端到端装配与数据流
│   │
│   ├── tools/                       开发诊断脚本（不属于交付物）
│   │   ├── ocr_probe.py             真实 OCR 验证脚本（渲染样图 → 识别 → 匹配）
│   │   ├── perf_probe.py            抓屏 / OCR 的 CPU 与墙钟成本实测
│   │   ├── hotkey_probe.py          全局热键真机探测（注册 → 模拟按键 → 注销）
│   │   └── diagnose_ocr.py          拿真实证据截图复盘 OCR 识别效果
│   │
│   └── data/                        ⚠ 运行期数据（git 忽略，别删）
│       ├── blacklist.db            SQLite 数据库
│       ├── user_config.json        区域配置
│       ├── notification.json       提示配置
│       ├── hotkey.json             扫描热键绑定
│       ├── assets/
│       │   ├── app_icon.png        应用图标（窗口 / 任务栏 / 托盘）
│       │   ├── app_icon.ico        exe 图标（多尺寸）
│       │   ├── default_icon.png    通知默认提示图（警告三角）
│       │   └── <你的图片 / 音频>    选自定义提示图、音效时自动复制到这里的副本
│       ├── evidence/               命中证据截图（自动清理，默认保留 500 张）
│       └── logs/app.log            运行日志（轮转，单文件上限 2 MB）
│
├── 发布包/                           ← 打包 / 分发产物（不在仓库里）
│   ├── HD2Blacklist/                文件夹版产物（build.py 输出，含 data/）
│   ├── 解压版/HD2Blacklist/         发布压缩包解开后的样子（实测用）
│   ├── HD2Blacklist-v1.0.0-win64.rar  发给别人的压缩包
│   └── 使用说明.txt / LICENSE.txt    build.py 自动从仓库复制过来
│
└── _packaged_data_backup/           ⚠ 历次打包前的用户数据备份（别删）
```

> **包名约定**：所有跨模块导入一律写绝对路径（`from app.core.matcher import Matcher`），
> 不写相对导入，也不依赖“当前目录正好在 sys.path 上”。这样 `python main.py`、
> `python -m unittest`、PyInstaller 三种跑法都一致。
>
> 已删除（改造后不再存在）：`chat_monitor.py`、`keyword_config.py`、
> `data/keywords.json`。源码里也没有任何残留引用（有测试递归守着）。

**可安全删除**（都会自动重建）：`build/`、`.test_tmp/`、`.piptmp/`、任意 `__pycache__/`。

**绝不能删**：`data/`、`../发布包/*/data/`、`../_packaged_data_backup/`、
`.pylibs/`、`.devtools/`、`使用说明.txt`。

所有 `data/*.json` 都支持 **手动编辑或直接删除恢复默认**，改动 **无需重启**。

---

## 11. 数据库结构

```sql
CREATE TABLE blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    player_name TEXT,
    note TEXT,
    tk_count INTEGER DEFAULT 0,           -- 用户手动录入
    encounter_count INTEGER DEFAULT 0,    -- 系统自动累积
    evidence_path TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME,
    UNIQUE(player_id, player_name)
);

CREATE TABLE encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blacklist_id INTEGER,
    name_seen TEXT,
    match_score REAL,
    source TEXT,
    screenshot_path TEXT,
    seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

- `PRAGMA journal_mode=WAL` + `BEGIN IMMEDIATE` 事务
  → 「次数 +1、更新时间、插入 encounters」三件事 **原子完成**，并发下不会丢计数
- 数据库连接跨线程使用，内部用 `threading.RLock` 串行化
- 排序字段走 **白名单**，防止 SQL 注入

**提示**：工具内附带了 2 条 `SamplePlayer_0x` 示例数据方便你上手，
不需要的话直接在界面里选中删除即可。

---

## 12. 自检与调试命令

```bat
:: 环境自检（逐项 OK/FAIL，退出码 0=通过）
python main.py --check

:: 抓取三个监视区域 + OCR，输出调试截图到 data/logs/
::   debug_screen.png      整屏缩略图，红框标出三个区域
::   debug_<region>.png    每个区域的原始截图
::   （控制台同时打印每个区域的 OCR 识别结果与置信度）
python main.py --capture-debug

:: 离线渲染一张通知预览图（不需要显示器）
python main.py --preview-notification

:: 打开 DEBUG 日志
python main.py --debug
```

> 上面这些**诊断命令不受单实例限制**：程序已经开着也能照跑
> （`--check` 不会因为「已有一个实例在运行」而被挡下来）。

**坐标或识别效果不确定时，先跑 `--capture-debug`，打开 PNG 人工确认，再继续调整。**

`--check` 会逐项报告，其中这几项与本轮修复直接相关：

| 自检项 | 检查什么 |
|---|---|
| 监视区域尺寸 | 三个区域是否小到不可能有内容（例如被误框成 10×13 像素）→ 直接 FAIL 并说明建议尺寸 |
| 全符号玩家名 | 黑名单里的 `?` / `？` 能否被索引、命中、并通过 OCR 名字过滤器 |
| 自定义资源目录 | `data/assets` 是否存在且**可写**（不可写 = 程序放在了受保护目录），并打印当前支持的音频格式 |
| 游戏友好优先级 / 扫描节流 | 当前的掉帧保护设置 |

日志：`data/logs/app.log` —— 所有会话的开始/结束（含终止原因与耗时）、
每次命中（玩家、分数、来源、累计次数）、**一次命中多个玩家时的整批名单**
（`[Hit] 本批共命中 N 名黑名单玩家：…`）、所有异常都会写入。

---

## 13. 打包成 exe

在**仓库根目录**（= `source_code/`）执行：

```bat
python build.py --clean              :: 文件夹版（推荐：启动约 1 秒）
python build.py --clean --onefile    :: 单文件版（便携，启动要解压 5~15 秒）
python build.py --console            :: 保留控制台，排查问题用
```

产物写到**上一层的 `发布包/`**，并自动在 exe 旁边放好 `data/` 种子目录、
`使用说明.txt` 和 `LICENSE.txt`。`发布包/` 同时也是**所有对外分发东西的集中地**：

```
发布包/                                   ← 一切"给别人用"的东西都在这里（不进仓库）
├── HD2Blacklist/                        文件夹版产物（build.py 输出，292 MB，启动 ~1s）
│   ├── HD2Blacklist.exe
│   ├── _internal/
│   ├── data/                            ← 用户真实数据长在这里
│   ├── 使用说明.txt                      ← build.py 自动复制
│   └── LICENSE.txt                      ← build.py 自动复制
├── 解压版/HD2Blacklist/                 发布压缩包解开后的样子（自己实测用）
│   └── （内容同上：exe + _internal + data + 两个文档）
├── HD2Blacklist.exe                     单文件版（120 MB，启动 ~10s，可选）
├── HD2Blacklist-v1.0.0-win64.rar        发布压缩包（发给别人用这个）
└── 使用说明.txt / LICENSE.txt            顶层再放一份，翻目录时一眼可见
```

> `--clean` **只清理本次要产出的那一份**（外加 `build/` 构建缓存），
> 不会动 `发布包/` 里的解压版、发布压缩包和说明书 —— 那里是手工维护的分发区。
> 文件夹版的 `data/` 在清理前会先搬出来、构建完再搬回去，用户数据不会丢。
> `使用说明.txt` 与 `LICENSE` 的**源文件在仓库根目录**，打包时复制一份到产物旁边，
> 所以「GitHub 内容全在 source_code/ 里」和「分发包自带说明书与许可证」同时成立。

#### 体积优化（重要）

`onnxruntime` 里有一段 `try: import torch` 的 PyTorch 后端分支，
PyInstaller 会顺着它把**整个 torch** 拖进包里 —— 实测多出约 500 MB、构建时间翻倍，
而本项目的 OCR 走的是 onnxruntime 的 CPU 执行器，**完全不需要 torch**。

`build.py` 里已经排除了这些无关的大块头：

| 排除项 | 原因 |
|---|---|
| `torch` / `torchvision` / `torchaudio` / `torchgen` / `functorch` | onnxruntime 的可选后端，用不到 |
| `numba` / `llvmlite` / `sympy` / `networkx` | torch 的依赖链 |
| `transformers` / `tokenizers` / `safetensors` / `huggingface_hub` | `onnxruntime.transformers` 子包引入 |
| `matplotlib` / `scipy` / `pandas` / `IPython` / `notebook` | 与本项目无关 |
| `PyQt5/6` / `PySide2/6` / `wx` | 本项目只用 tkinter |
| `fsspec` / `aiohttp` / `pydantic` 等 | torch 分布式检查点的依赖 |

优化效果：

| 打包形式 | 优化前 | 优化后 |
|---|---|---|
| 文件夹版 | 817 MB | **288 MB** |
| 单文件版 | 299 MB | **120 MB** |

> `config.py` 以 **exe 所在目录** 作为根目录，所以 `data/` 必须和 exe 同目录。
> onedir 模式下就是 `发布包/HD2Blacklist/data/`。

#### 拷到别人电脑上能跑吗

**能**，对方不需要装 Python。包里已经自带（实测核对过）：

| 类别 | 内容 |
|---|---|
| Python 运行时 | `python313.dll`、`base_library.zip`、标准库、PYZ |
| C 运行库 | `vcruntime140.dll`、`vcruntime140_1.dll`、**`msvcp140.dll`** |
| GUI | `tcl86t.dll` / `tk86t.dll`、`_tkinter.pyd` + Tcl/Tk 脚本库 |
| 视觉/识别 | `numpy`、`onnxruntime`（+ `onnxruntime_providers_shared.dll`）、`PIL` |
| OCR 模型 | `ch_PP-OCRv4_det_infer.onnx`(4.6MB)、`ch_PP-OCRv4_rec_infer.onnx`(10.6MB)、`cls`(0.6MB) |
| 其它 | `mss`、`pywin32`、`pystray`、`winotify`、`pygame`、`rapidfuzz` |

分发清单（少一条都可能出问题）：

1. **整个 `HD2Blacklist\` 文件夹一起拷** —— 只拷 exe 起不来（缺 `_internal\`）；
2. 放在**可写目录**（桌面 / D:\）。放 `C:\Program Files` 会因为要写 `data\` 而失败；
3. 目标系统 **Windows 10 / 11 64 位**；极老的不支持 AVX 的 CPU 可能跑不了 onnxruntime；
4. 到新机器后**必须重新校准三个区域**（坐标跟分辨率/UI 缩放绑定）；
5. 首次运行 SmartScreen 可能拦（exe 无签名）→「更多信息」→「仍要运行」；
6. `data/` 里是用户数据（黑名单 / 证据 / 通知配置 / 热键）——
   要给别人干净版本就删掉 `data\`，程序会自建；随包的图标会自动补回来
   （`config._seed_bundled_assets`，所以「删 data 重置」不会丢图标）；
7. 打包版**没有控制台**，想跑自检看结果：
   `start /wait HD2Blacklist.exe --check` 然后读 `data\logs\app.log`
   （自检输出已通过 `_CheckTee` 同时写进日志）。
>
> exe 图标取自 `data/assets/app_icon.ico`；如果只有 PNG，
> `build.py` 会自动生成多尺寸 ICO 再传给 PyInstaller。

---

## 14. 项目结构与架构

```
┌─────────────────────────────────────────────────────┐
│  WMI 进程监控（事件订阅，零轮询；权限不足自动降级）   │
│    on_start → 冷启动扫描 + 激活 ESC 监听             │
└──────────────────┬──────────────────────────────────┘
                   │
     ┌─────────────┼──────────────┐
     ▼             ▼              ▼
┌──────────┐  ┌──────────┐  ┌──────────┐
│聊天框     │  │ESC 按键  │  │冷启动    │
│按需扫描   │  │触发      │  │扫描      │
│（点按钮/  │  │100ms 轮询│  │12s 后    │
│ F8 才跑） │  │菜单判定  │  │一次      │
│无循环定时 │  │          │  │          │
└────┬─────┘  └────┬─────┘  └────┬─────┘
     │             │              │
     │ matcher     │ ScanSession  │ ScanSession
     │ .match_text │ (menu_player_│ (player_
     │ 直接匹配    │  list)       │  list_hud)
     │ 聊天文本    │              │
     └─────────────┴──────────────┘
                        │
                        ▼
        handle_hits（批量） / match_and_notify_batch
                        │
         ┌──────────────┴───────────────┐
         ▼                              ▼
   ① 30 秒去重过滤（跳过并写日志）
   ② 统一标记去重
   ③ 逐个：证据落盘 → 原子更新 DB → 通知 GUI
   ④ 批量弹提示：N 个通知栏 + **只播一次音效**
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
   ┌──────────────┐          ┌────────────────────┐
   │  Notifier    │          │ GUI 主线程          │
   │ WS_EX_       │          │ queue.Queue +       │
   │ NOACTIVATE   │          │ root.after(100ms)   │
   │ 分层窗口堆叠 │          │ 逐行闪烁 + 汇总统计 │
   └──────────────┘          └────────────────────┘
```

数据流里的每个角色，对应到 `app/` 下的哪个包：

| 图里的角色 | 代码位置 | 职责 |
|---|---|---|
| 进程监控 / 单实例 / 优先级 | `app/core/` | 基础设施：WMI 事件、命名 Mutex、进程与线程优先级 |
| 数据库 / 匹配 | `app/core/database.py`、`app/core/matcher.py` | 存储与四层匹配 |
| 自定义图片 / 音效落盘 | `app/core/assets.py` | 默认目录 `data/assets`、格式白名单、选完复制一份 |
| 配置读写 | `app/settings/` | 区域、通知外观、热键绑定 |
| 抓屏 / OCR | `app/capture/` | mss 小区域截图、RapidOCR 封装 |
| 聊天框 / ESC / 冷启动 / ScanSession | `app/scanning/` | 触发、会话生命周期、调度中枢 |
| Notifier（Overlay） | `app/notify/notifier.py` | 无焦点分层窗口 + 音效 |
| GUI 主线程 | `app/ui/` | 主窗口、通知设置、校准器、快捷键对话框、主题 |
| 装配与命令行 | `app/application.py`（入口薄壳 `main.py`） | 把上面这些接起来 |

> 下文为简洁起见用**短模块名**指代文件，实际路径都在 `app/` 下按层分目录，
> 例如 `matcher.py` = `app/core/matcher.py`、`gui.py` = `app/ui/gui.py`、
> `scan_session.py` = `app/scanning/scan_session.py`。

### 匹配策略（先精确，再易混，再模糊，最后符号层）

1. **整段包含检查**：把整段文本归一化后，检查黑名单名字是否为子串 → 100 分
   （覆盖多词玩家名被标点切散的情况）
2. **分词 + 相邻 2/3 词组合** → 归一化后完全相等 → **100 分**
3. **OCR 易混字符容错** → 把 `0↔O`、`1↔l↔I`、`5↔S`、`8↔B`、`2↔Z`、`4↔A`
   折叠到同一个字符后完全相等 → **100 分**（仅当该折叠键在黑名单中**唯一**时才生效）
4. 否则用 `rapidfuzz.ratio` 比对，**≥ 85** 才返回
5. **符号层**：名字**整条就是符号**（例如玩家名就是一个 `?`）时，
   只按「整段相等 / 某个片段就是该符号串」匹配 → 100 分

关键细节：

- 归一化：`NFKC` 折叠（全角 `？` → 半角 `?`、全角字母数字 → 半角）→ 转小写 →
  去掉所有非字母数字字符（下划线也算无意义字符），
  因此 `Player_X` 与聊天框里的 `PlayerX` 能精确命中
- **精确永远优先于模糊**：所有候选先整体跑一遍精确 / 易混匹配，再跑模糊匹配。
  否则「较短的候选先被模糊命中」会抢走本该属于「较长候选精确命中」的玩家
- **全符号名字**单独索引（`Matcher.symbols`）：老实现下 `?` 会被归一化成空串、
  在 `reload()` 里被整个丢掉，这类名字永远匹配不上。
  为了不误报，符号层要求**整段符号串相等** —— 聊天里打 `???` 或 `?!` 不会命中 `?`
- **OCR 拆行自动拼接**：OCR 常把 `SamplePlayer_01` 识别成 `SamplePlayer` + `01`
  两个文本框。`scan_session.group_into_lines()` 按 **纵向重叠 + 横向间距**
  把同一行的碎片拼回完整名字（玩家列表路径），`Matcher.check / match_text`
  也会尝试相邻 2/3 项的拼接作为兜底
- **UI 噪声过滤**：`小队` / `社交` / `185级|功勋英雄` 这类菜单文字不算玩家名。
  名字和等级被 OCR 拼成一行（`PlayerX185级|功勋英雄`）时，会砍掉等级段保留 `PlayerX`
- **黑名单白名单反哺 OCR 过滤**：`Matcher.name_allowlist()` 交给
  `scan_session.is_valid_player_name()` —— 只要黑名单里真有这个名字，
  哪怕它"不像名字"也放行
- 同一次匹配中同一玩家 **只返回一次**（按条目 ID 去重）
- 返回命中时匹配到的 **原文子串**，用于证据与日志

实测（`tools/ocr_probe.py`，真实 RapidOCR）：

```
原始框: [('SamplePlayer', (15,22,267,62)), ('01', (261,22,304,55)),
         ('SamplePlayer_02', (13,64,314,114)), ('RandomGuy', (15,114,230,158))]
合并后: ['SamplePlayer01', 'SamplePlayer_02', 'RandomGuy']
匹配  : SamplePlayer_01 → 100 分
```

### GUI 线程安全（硬红线）

后台线程 **只能** 通过两个 `queue.Queue` 与主线程通信：

| 队列 | 用途 |
|---|---|
| `_update_queue` | 命中数据（`enqueue_encounter_update`） |
| `_command_queue` | 需要在主线程执行的可调用对象（`post`，托盘菜单用） |

主线程用 `root.after(100, ...)` 周期性排空队列 ——
**没有任何一处后台线程直接操作 tkinter**，因此不会有 Tcl 线程错误。

---

## 15. 资源占用设计

| 手段 | 位置 |
|---|---|
| 手段 | 位置 |
|---|---|
| **聊天框完全按需扫描：不点按钮 = 零开销** | `chat_scanner.scan_now` |
| 连点按钮不会并发 OCR（非阻塞锁） | `chat_scanner._busy` |
| 30 秒去重：同一玩家不重复计数/提示 | `scan_scheduler._is_recently_hit` |
| 会话三终止条件，静止画面自动收工 | `scan_session._run` |
| 只截小区域（几百×几百），不截整屏 | `screen_capture.grab` |
| OCR 前限制放大倍数与最大边长 | `ocr_engine.preprocess` |
| onnxruntime 线程数限制为 2 | `config.OCR_INTRA_OP_THREADS` |
| 游戏未运行 / 用户暂停 → 不启动扫描会话 | `scheduler.is_active` |
| 图片按 mtime 缓存，避免重复读盘 | `notifier.load_image_cached` |
| 批量命中只播一次音效（不是 N 次） | `notifier.alert_batch` |
| 证据截图 JPEG 压缩 + 自动清理旧文件 | `scan_scheduler._prune_evidence` |
| 日志轮转，单文件上限 2 MB | `config.get_logger` |
| 去重缓存超 256 条自动清理过期项 | `scan_scheduler._is_recently_hit` |
| 导入导出走后台线程，不卡 UI | `gui._export_worker` / `_import_worker` |
| 系统托盘暂停/退出，长期后台常驻 | `gui._ensure_tray` |
| 抓屏/OCR 全部以低优先级运行 | `priority.low_priority` |
| 画面没变就不重复 OCR | `scan_session._frame_changed` |
| 画面静止 N 帧就收工（少抓屏 = 少卡顿） | `SESSION_MAX_STATIC_FRAMES` |

### 触发扫描时游戏掉帧（1% low 被拉到 55 之类）怎么调

触发扫描会掉帧有**两个完全不同的原因**，对应两组开关：

| 原因 | 机制 | 对应开关 |
|---|---|---|
| **① CPU 被抢** | 抓屏 + OCR 是一次性吃 CPU 的活（实测单次 **1~6 CPU 秒**，压在 1 秒内跑完 ≈ 瞬时占满 3 个核）。和游戏同为 NORMAL 优先级时 Windows 会公平轮转 CPU，渲染线程被拖 → 帧时间被拉长 | `GAME_FRIENDLY_PRIORITY = True`（默认）：进程压到 `below_normal`、扫描线程压到 `lowest`，游戏永远优先拿到 CPU |
| **② 抓屏让 GPU/DWM 同步** | GDI 截屏（mss/BitBlt）要从 DWM 合成表面读回像素，**每次抓屏都可能让当帧晚一拍 —— 抓屏次数 ≈ 卡顿次数** | `ESC_SESSION["interval"]`（默认 0.5s）、`SESSION_MAX_STATIC_FRAMES`（静止 6 帧收工） |
| **③ 同一张图反复 OCR** | 菜单开着不动时画面一模一样，却一次次送去识别 | `SESSION_SKIP_UNCHANGED = True`（默认）：只做一次几十微秒的缩略图差分 |

自检会打印当前设置（`python main.py --check` 里的「游戏友好优先级 / 扫描节流」两项）；
想量化本机成本就跑 `python tools/perf_probe.py`，它会实测：

- 抓图 / 只做检测 / 检测+识别 各花多少 CPU 与墙钟
- 静止画面 N 帧里真的跑了几次 OCR
- 「改前 / 改后」一次菜单会话各抓了多少次屏

> 还想更省：把 `OCR_UPSCALE` 从 2 降到 1，识别像素少 4 倍（CPU 大约省 3 倍），
> 代价是小字号识别率可能下降 —— 建议先用 `--capture-debug` 确认能认出来再改。

---

## 16. 常见问题

**Q：截图是黑的 / Overlay 不显示？**
游戏必须是 **无边框窗口** 模式。全屏独占下 Windows 会屏蔽其它窗口与截图。

**Q：一触发扫描，游戏的最低帧（1% low）就掉到 55 之类？**
两种原因，见 [15. 资源占用设计](#触发扫描时游戏掉帧1-low-被拉到-55-之类怎么调)：

1. **CPU 被抢** —— 默认已把本进程压到 `below_normal`、扫描线程压到 `lowest`
   （`GAME_FRIENDLY_PRIORITY`），游戏始终优先拿 CPU；
2. **抓屏本身让 GPU/DWM 同步** —— 抓屏次数 ≈ 卡顿次数，默认已把菜单会话
   抓屏间隔放宽到 0.5s、画面静止 6 帧即收工、画面没变就不跑 OCR。

还想更激进：把 `OCR_UPSCALE` 降到 1（识别像素少 4 倍），
或者把 `ESC_SESSION["interval"]` 再放宽到 0.8~1.0。
想确认到底是哪一项在起作用，跑 `python tools/perf_probe.py` 看实测数字。

**Q：识别不到玩家名？**
1. 先跑 `python main.py --capture-debug`，打开 `data/logs/debug_<区域>.png`
   确认框选区域是否真的覆盖了文字
2. 在 **[校准区域]** 里重新框选，**只框文字本身**，不要框进大片背景
3. OCR 对小字号、低对比度文字识别率会下降；HUD 缩小倍率太高时可适当放大游戏 UI
4. 跑 `python main.py --check` 看 **「监视区域尺寸」** —— 区域被框得太小
   （例如单击一下留下的 10×13 像素）是永远识别不到东西的，自检会直接点名
5. 姓名被 OCR 认错个别字符（`0`↔`O`、`1`↔`l`/`I`、`5`↔`S`、`8`↔`B`、`2`↔`Z`、`4`↔`A`）
   时由 **易混字符容错层** 兜住，仍然按 100 分命中；只有该折叠键在黑名单里**唯一**时才启用

**Q：ESC 菜单里明明有玩家名，却什么都没检出？**
早先的「菜单是否打开」判定用灰度标准差 > 20，而真实菜单截图的标准差是 9.6~23.3
—— 阈值正好压在中间，一半概率直接判定"菜单没开"、**根本不扫描**。现在改成看
**平均亮度**（暗色面板 = 菜单开着），判不准还会复查 3 次、最后宁可按开着处理。
升级到本版本即可，不需要改配置。

**Q：玩家名就是一个问号 `？` / `?`，怎么不响应？**
以前这类「全符号名字」在两条路上都会被丢掉：归一化会把它变成空串（索引时直接跳过），
OCR 判读又会认为它"不含字母数字"而不像玩家名。现在：

- 黑名单里存 `?` 或 `？` 都行（全角半角等价，`NFKC` 折叠）；
- 走独立的 **符号层**，**只按整体相等**匹配 —— 所以聊天里打 "???" 或 "?!"
  不会误报，只有整个名字/整个片段就是 `?` 时才命中；
- OCR 判读过滤器带 **黑名单白名单**：只要黑名单里真有这个名字，再"不像名字"也放行。

自检里的 **「全符号玩家名」** 一项会验证这条链路（`python main.py --check`）。

**Q：误报太多？**
- 提高匹配阈值：`config.py` 里的 `MATCH_THRESHOLD`（默认 85）- 尽量录入 **完整玩家名**，太短的名字（2-3 个字母）容易模糊命中
- 也可以在黑名单里只填 `player_id`，让名字留空

**Q：提示音不响？**
检查通知设置 → 音效页是否启用了音效、模式是否为 `none`；
自定义音频支持 WAV / MP3 / OGG / FLAC，WAV 建议用 16bit PCM 44.1kHz。
路径为空或文件已删掉时，[试听] 会直接提示；运行期加载失败也会写进
`data/logs/app.log`（搜「加载音效失败」）。

**Q：扫描聊天框没反应 / 说"上一次扫描还没结束"？**
说明上一次扫描仍在跑（OCR 通常 0.3–0.5 秒）。这是 `_busy` 锁在起作用，
等它结束再点即可。如果状态栏说"聊天框为空"，说明 OCR 没在聊天框区域里
识别到文字 —— 先用 **[校准区域]** 重新框选聊天框。

**Q：为什么同一个玩家只计了一次？**
命中有 **30 秒去重窗口**。想立刻重新计数，用
设置 → 聊天框扫描快捷键 → **清空命中去重缓存**。

**Q：游戏里按 F8 没反应？**
热键现在是 `RegisterHotKey` 系统全局热键，正常在游戏里也有效。若无效请：
① 确认 设置 → 聊天框扫描快捷键 里 **已启用**；
② 看状态栏/日志是否提示「已注册为系统全局热键」还是「退回按键轮询模式」——
若被其它程序占用（例如录屏/外设驱动也用了 F8），换个键或关掉那个程序即可；
③ 运行 `python tools/hotkey_probe.py` 做真机探测（会临时独占该键几秒后注销）。

**Q：一次命中好几个玩家，只看见一个通知栏？**
升级到本版本即可。曾经所有通知栏共用 **一个** 分层窗口，而 `UpdateLayeredWindow`
会重绘整个窗口并移动它 —— 后画的把先画的整个盖掉，只看得见最后一条。
现在每个堆叠槽位一个独立窗口，N 个玩家并排显示 N 栏，音效仍然只播一次，
超过 5 个时最后一个是汇总栏（`等 N 名：A、B、C`）。
完整名单也会写进 `data/logs/app.log`。

**Q：导入会不会覆盖我现在的数据？**
取决于你选的策略：默认 **跳过**（什么都不覆盖）；
「更新备注」保留你本机累积的遇到次数/最后遇见；
只有「完全覆盖」才会连计数一起替换。而且导入是 **单事务**，
中途出错会全部回滚。

**Q：点了关闭窗口，程序去哪了？**
默认 **最小化到系统托盘**，监控继续运行。从托盘菜单「退出」才会真正结束。
（想关闭即退出，可把 `gui.on_close` 改成调用 `quit_app`。）

**Q：怎么彻底重置？**
删掉整个 `data/` 目录，下次启动会自动重建。

**Q：托盘图标不出现？**
需要 `pystray`。若安装失败，程序会在状态栏提示「系统托盘不可用」，其它功能正常。

**Q：不小心双击了好几次，会不会开出好几个进程？**
不会。程序用**命名内核互斥体**做了单实例限制（`single_instance.py`）：

- 已经在运行时再启动，**不会**多开：会先把已有窗口从托盘/后台**叫到前台**，
  然后第二个进程直接退出（退出码 1）；
- 万一找不到那个窗口，才弹一句「程序已在运行」的提示；
- 进程被任务管理器强杀 / 崩溃后，内核对象由系统自动回收，
  **不会**留下「锁文件删不掉导致再也打不开」的问题；
- 互斥体名字带 `Local\`，只在当前登录会话内互斥，多用户互不干扰；
- `--check` / `--capture-debug` / `--preview-notification` **不受限制**，
  程序开着也能跑诊断命令。

---

## 17. 验收标准对照

### 本轮改造（三项改进）

| 验收项 | 实现 | 测试 |
|---|---|---|
| 主界面有 `[扫描聊天框]` 按钮 | `gui._build_toolbar` → `scan_chat_now` | `test_scan_button_exists` |
| 点击后 1 秒内完成扫描 | OCR 实测 0.3–0.5 s | `test_scan_is_fast` |
| 连点不会并发 OCR | `_busy` 非阻塞锁 → 返回 `busy` | `test_no_concurrent_ocr` |
| 游戏运行但未点击时 CPU < 0.3% | 无循环、无定时器，不点就零开销 | `test_no_background_chat_scanning` |
| 无 1 秒 tick、无 10 秒定期 OCR | 常量已删除 | `test_no_deleted_constants` |
| `keyword_config.py` / `data/keywords.json` / `chat_monitor.py` 已删除 | 已删 | `TestDeletedModulesAreGone` |
| 代码中无 `from keyword_config import` 残留 | 全项目扫描 | `test_no_dangling_references_in_source` |
| 导出 CSV 可用 Excel 打开不乱码 | `utf-8-sig`（带 BOM） | `test_csv_has_bom_for_excel` |
| 导出 JSON 结构清晰 | `{version, exported_count, fields, entries}` | `test_json_structure` |
| 导入按所选策略处理冲突 | `skip` / `update_note` / `overwrite` | `test_import_*_strategy` |
| 导入结果显示新增/更新/跳过数量 | `_on_import_done` 弹窗 | `test_import_worker_inserts_and_refreshes` |
| 空 player_id 行被跳过 | `import_entries` 首行判断 | `test_import_skips_empty_player_id` |
| 导入失败时全部回滚 | `BEGIN IMMEDIATE` + 整体 `ROLLBACK` | `test_import_rolls_back_on_error` |
| 命中 2 / 3 / 1 个玩家 → 对应数量通知栏 + 1 次音效 | `notifier.alert_batch` | `test_batch_of_two/three/one_plays_sound_once` |
| 通知栏垂直堆叠、互不遮挡 | `_stacked_position`（gap = 高度+8） | `test_batch_stacks_vertically_without_overlap` |
| 每个命中玩家在 Treeview 独立闪烁 | `on_encounter` 逐个回调 | `test_on_encounter_called_per_hit` |
| 每个命中玩家 `encounter_count` 都 +1 | `_process_hits` 逐条 `record_encounter` | `test_handle_hits_calls_alert_batch_once` |
| 连点扫描按钮，30 秒内同一批玩家全部去重跳过 | `_is_recently_hit` | `test_repeated_clicks_are_deduped` |
| 30 秒后同一玩家再次命中可正常计数 + 弹提示 | 窗口过期即放行 | `test_dedup_expires` |
| `app.log` 中有 `[Hit] 去重跳过 entry_id=X source=Y` | `_process_hits` | `test_dedup_skip_is_logged` |
| 堆叠上限 5，超出的命中汇总成一栏列出剩余玩家 | `MAX_NOTIFY_STACK` + `alert_batch` 的汇总栏 | `test_max_stack_limit`、`test_overflow_is_summarised_in_the_last_slot` |
| 屏幕下沿保护 | `_stacked_position` 返回 `None` 跳过 | `test_offscreen_stack_is_skipped` |
| 不重写项目 / 不改无焦点样式 / 不改线程安全机制 | 分层窗口 4 样式仍由自检校验；GUI 仍只用两个 `queue.Queue` | `--check` + 全部 GUI 测试 |

### 第五轮改动（自定义资源：默认目录 / 自动复制 / 音频放宽到 MP3）

| 验收项 | 实现 | 测试 |
|---|---|---|
| 选自定义图片 / 音效时对话框默认停在 **`data/assets`** | `app/core/assets.py: assets_dir()` 直接当 `initialdir`，目录不存在会自动创建 | `test_pick_image_defaults_to_assets_and_copies`、`test_pick_sound_accepts_mp3_and_copies`、`test_creates_and_returns_dir` |
| 选中的文件**自动复制一份**进 `data/assets` | `import_asset()`：已在目录内不复制；同名且内容相同直接复用；同名不同内容存成 `xxx (2).ext`；单文件上限 64 MB；复制失败只弹警告并回退为引用原文件 | `test_copies_into_assets`、`test_second_identical_pick_reuses_existing`、`test_same_name_different_content_gets_suffix`、`test_file_already_in_assets_is_not_copied`、`test_oversized_raises`、`test_import_failure_falls_back_to_original_path` |
| 音频格式从「只有 WAV」放宽到 **WAV / MP3 / OGG / FLAC** | 过滤器与提示文案集中在 `assets.py`；`SoundPlayer._play_file` 交给 SDL2_mixer 解码（打包后用同一套 wheel）。配置里的模式值仍叫 `wav`（历史命名） | `test_real_mp3_loads_with_pygame`（真编码一段 MP3 再解码）、`test_audio_includes_mp3_and_more`、`test_play_file_accepts_non_wav_extensions` |
| `[试听]` 不再「点了没反应」 | `_test_sound()` 先检查路径存在性并提示；播放器加载失败改记 WARNING（含支持的格式），写进 `app.log` | `test_missing_mp3_is_silent`、`test_pick_sound_accepts_mp3_and_copies` |
| 新增自检项 **「自定义资源目录」** | 目录存在 + 可写（写入探针文件再删）+ 打印当前音频格式 | `python main.py --check` |
| 会话存活 / 超时计时改用 `time.perf_counter()` | Windows 上 `time.time()` 只有 ~15.6 ms 粒度且不单调，拿它比 `keep_alive_after_hit` 会提前收工 | `test_keep_alive_after_hit_stops_early`（同时把该用例的 `max_static_frames` 置 0 —— 原先 6 帧静止收工与 0.15 s 存活期是**竞跑**，谁先到看机器负载，会随机失败） |

### 第四轮修复（实机反馈：ESC 漏检 / 符号名字 / 多命中只播一个）

| 验收项 | 实现 | 测试 |
|---|---|---|
| **ESC 菜单开着也被判成"没打开"→ 根本不扫描** | 判定从「灰度标准差 > 20」改为 **平均亮度**（暗色面板 = 菜单开着）：`ESC_MENU_MEAN_MAX = 90`；标准差只用来排除纯黑画面。真实截图实测：菜单开着 mean 10~36 / std 9.6~23.3，菜单没开 mean 140 / std 93 —— 旧阈值正压在中间 | `test_menu_open_with_low_std_still_scans`、`test_menu_closed_skips`、`test_judge_menu_states` |
| 菜单淡入期判不准 → 复查后再决定 | 最多复查 `ESC_MENU_CHECK_RETRY = 3` 次、间隔 0.35 s；**复查完仍判不准就按"已打开"处理**（多扫一次 << 漏扫） | `test_unknown_state_retries_then_scans` |
| ESC 会话过早收工 | `max_consecutive_empty` 3→4、`keep_alive_after_hit` 3.0→3.5 | `test_esc_session_params` |
| **玩家名就是一个问号 `?` / `？` 时不响应** | ① `Matcher.reload()` 把「全符号名字」单独索引到 `symbols`（旧实现归一化成空串后整个丢弃）；② 符号层**只按整体相等**匹配；③ `is_valid_player_name` 接受黑名单白名单，`?` 能过 OCR 过滤器；④ `NFKC` 折叠让全角 `？` 与半角 `?` 等价 | `test_symbol_only_entry_is_indexed`、`test_check_matches_symbol_name`、`test_fullwidth_question_mark_matches_halfwidth_entry`、`test_symbol_name_needs_allowlist`、`--check` 的「全符号玩家名」 |
| 符号名字不误报（聊天里的问号不触发） | `symbol_keys_in()` 要求**整段符号串相等**：`???` / `?!` 不会命中 `?` | `test_longer_symbol_runs_do_not_false_positive` |
| OCR 认错字符（`0`↔`O`、`1`↔`l`/`I` …）导致漏检 | 易混字符折叠层（`MATCH_FUZZY_CONFUSABLE`），仅当折叠键在黑名单中**唯一**时生效 | `test_digit_letter_confusion_matches`、`test_ambiguous_confusion_is_refused`、`test_confusion_in_chat_text` |
| 菜单 UI 文本被当成玩家名（`185级\|功勋英雄`、`小队`） | `is_valid_player_name` 增加等级/头衔行正则与分栏标题词表；名字与等级被拼成一行时砍掉等级段保留名字 | `test_level_line_rejected`、`test_menu_headers_rejected`、`test_merged_name_plus_level_keeps_name_only`、`test_ui_noise_does_not_reach_matcher` |
| **一次命中多个玩家却只播报一个（且看不出是谁）** | 每个堆叠槽位一个独立 `WS_EX_LAYERED` 窗口（`_OverlaySurface`）。原先共用一个窗口，而 `UpdateLayeredWindow` 会重绘整个窗口并搬走它 → 后画的把先画的整个盖掉 | `test_each_stack_slot_gets_its_own_window`（真建 3 个窗口）/ `test_slot_beyond_limit_is_not_created` |
| 超过堆叠上限时"还有谁"仍可见 | 最后一栏换成汇总栏：`等 N 名：A、B、C`（放 `player_name`，不会因用户关掉备注字段而消失） | `test_overflow_is_summarised_in_the_last_slot`、`test_no_summary_when_everything_fits` |
| 事后能核对"这一批到底命中了谁" | `[Hit] 本批共命中 N 名黑名单玩家：…` 一次打进 `app.log` | `test_batch_hit_names_are_logged` |
| 区域被误框成极小尺寸（永远识别不到东西） | `region_config.region_warnings()`：启动时写 WARNING 日志，`--check` 里独立成一项并给出建议尺寸 | `test_region_size_warning`、`--check` 的「监视区域尺寸」 |

### 第三轮修复（实机反馈）

| 验收项 | 实现 | 测试 |
|---|---|---|
| 命中去重窗口改为 **30 秒** | `config.HIT_DEDUP_WINDOW = 30` | `test_new_chat_and_dedup_constants` |
| **F8 在游戏里（前台全屏）也能触发** | `chat_hotkey` 改用 `RegisterHotKey` 系统全局热键，系统投递 `WM_HOTKEY`，与前台窗口无关 | `test_register_mode_uses_system_hotkey`、`test_wm_hotkey_triggers_scan_once`、`tools/hotkey_probe.py` |
| 长按不连发 | `MOD_NOREPEAT` + 1 秒去抖 | `test_register_mods`、`test_debounce_blocks_rapid_repress` |
| 热键被占用时自动降级（功能不失效） | 注册失败 → 退回 `GetAsyncKeyState` 轮询并写日志/状态栏 | `test_register_failure_falls_back_to_polling`、`test_missing_message_window_falls_back` |
| 停用/改键后彻底注销 | `UnregisterHotKey` + `PostThreadMessage(WM_QUIT)` 收线 | `test_stop_unregisters_and_quits_loop` |
| **修复启动即崩**（`AttributeError: '_threads'`） | `_threads` 在**任何** `_spawn()` 之前建好；`_spawn` 再加一层兜底 | `test_app_starts_with_hotkey_enabled`、`test_spawn_creates_threads_list_if_missing` |
| 打包版不再弹 PyInstaller 原始报错框 | `main()` 把 App 构造纳入 `try`，顶层再兜一层 `_fatal_dialog` | `--check` + `test_start_hotkey_without_threads_attr` |
| 开启热键后状态栏说明当前模式 | `apply_hotkey_config` 返回状态文案 | `test_apply_hotkey_config_returns_status_text` |

### 单实例（只能开一个进程）
| 验收项 | 实现 | 测试 |
|---|---|---|
| 已有实例时第二个进程不启动、不多开 | `CreateMutexW` 命名互斥体 → `ERROR_ALREADY_EXISTS` | `test_second_acquire_is_refused`、`test_second_instance_does_not_build_app` |
| 重复打开会把已有窗口叫到前台 | `FindWindowW(标题)` → `ShowWindow(SW_RESTORE/SW_SHOW)` + 置顶 | `test_finds_and_shows_window`、`test_minimized_window_is_restored` |
| 找不到窗口时给一句可读提示 | `_dialog()`（隐藏 Tk root + messagebox） | `test_falls_back_to_dialog` |
| 进程被强杀后不会「再也打不开」 | 内核对象随进程回收，无锁文件 | `test_released_mutex_can_be_taken_again` |
| 第一个实例退出后仍能重新启动 | 句柄关闭即销毁互斥体 | `test_released_mutex_can_be_taken_again` |
| `--check` 等诊断命令不受限制 | 单实例检查放在诊断分支之后 | `test_check_mode_bypasses_single_instance` |
| 窗口标题单一来源（避免找不到窗口） | `config.WINDOW_TITLE`，GUI 与单实例共用 | `test_window_title_is_shared_constant` |
| 取不到 kernel32 时不阻断启动 | 失败即跳过检查并写日志 | `test_non_windows_is_unrestricted` |

### 掉帧优化（触发扫描时保护游戏帧数）

| 验收项 | 实现 | 测试 |
|---|---|---|
| 抓屏 + OCR 以低优先级运行，游戏优先拿 CPU | `priority.low_priority` + 进程 `below_normal` | `test_low_priority_restores_after_block`、`test_round_trip` |
| 画面没变就不重复 OCR | `scan_session._frame_changed`（缩略图差分） | `test_static_screen_skips_ocr`、`test_changing_screen_still_scans` |
| 画面静止 N 帧就收工（少抓屏） | `SESSION_MAX_STATIC_FRAMES` | `test_static_frames_end_the_session`、`test_static_limit_zero_disables_early_stop` |
| 抓屏间隔放宽到 0.5s | `ESC_SESSION["interval"]` | `test_esc_session_has_static_limit` |
| 优化可一键关掉对比 | `GAME_FRIENDLY_PRIORITY` / `skip_unchanged` | `test_skip_unchanged_can_be_disabled`、`test_config_defaults` |
| 成本可量化 | `tools/perf_probe.py` | 实测输出（非单测） |

### 提示文字大小（可编辑）

| 验收项 | 实现 | 测试 |
|---|---|---|
| 标题字号可编辑 | 外观页「标题字号」→ `appearance.title_font_size` | `test_collect_includes_font_sizes` |
| 正文字号可编辑（与标题独立） | 外观页「正文字号」→ `appearance.body_font_size` | `test_title_and_body_are_independent` |
| 字号变化实时预览 | 变量 trace → 200ms 防抖重渲染 | `test_font_size_change_updates_preview` |
| 一键预设（小/标准/大/特大） | `_apply_font_preset` | `test_font_presets` |
| 字号保存并生效 | `notification.json` 深合并 | `test_font_size_saved_and_reloaded` |
| 字号变大不被截断 | 内边距/行高/窗口高度联动 | `test_larger_font_grows_height`、`test_long_text_with_large_font_grows_a_lot` |
| 非法输入回退默认 | `clamp_font_size` + `_safe_int` 兜住 `TclError` | `test_font_size_bad_input_falls_back` |
| 超范围自动夹紧（8–72） | `FONT_SIZE_MIN/MAX` | `test_clamp_bounds`、`test_font_size_clamped_on_collect` |
| 老配置无此字段也不报错 | 深合并补默认值 | `test_default_font_size_used_when_missing` |

### 应用图标与界面美化

| 验收项 | 实现 | 测试 |
|---|---|---|
| 应用图标用于窗口 / 任务栏 | `theme.apply_window_icon`（iconphoto + iconbitmap） | `test_apply_window_icon_sets_icon` |
| 任务栏不被识别成 python.exe | `SetCurrentProcessExplicitAppUserModelID` | `test_set_taskbar_identity_does_not_raise` |
| 应用图标用于系统托盘 | `gui._ensure_tray` 读 `app_icon.png` | GUI 冒烟测试 |
| exe 带图标 | `build.py --icon app_icon.ico`（缺 ICO 自动生成） | `test_ico_has_multiple_sizes` |
| 暗色主题全局生效 | `theme.apply_theme`（clam + ttk 样式 + tk 默认色） | `test_ttk_styles_configured` |
| 正文对比度足够（可读性） | 前景/背景 WCAG 对比度 > 7:1 | `test_foreground_contrasts_with_background` |
| 命中闪烁在暗色下仍可见 | `flash = #6d2f2f` + clam 主题 | `test_treeview_supports_row_tags` |
| 只改外观、不改行为 | 全部 350 个测试仍然通过 | 全量测试 |

### 功能（原有）

| 验收项 | 实现 |
|---|---|
| 首次启动生成 notification.json | `ensure_notification_file()`，在 `main.main()` 最前面调用 |
| 无 user_config.json 时用默认区域 | `RegionConfig.get()` 回退 `DEFAULT_REGIONS` |
| 校准器可框选/恢复/预览任一区域 | `calibrator.py` |
| 黑名单 CRUD 正常持久化 | `database.py` + `gui.py` |
| 游戏启停正确响应 | `process_watcher.py` → `_on_game_start/_on_game_stop` |
| 聊天框按需扫描（按钮 / F8） | `chat_scanner.py`（+ `chat_hotkey.py`） |
| ESC 触发扫描会话，菜单未开时跳过 | `esc_trigger.py`（平均亮度 + 标准差双信号判定，判不准复查 3 次后保守扫描） |
| 冷启动扫描正常触发 | `main._cold_start_scan`（12 秒） |
| 命中弹出无焦点 Overlay + 音效 | `notifier.py` |
| 提示不抢焦点、不卡顿、不拦鼠标 | 分层窗口 + 4 个扩展样式（自检会校验） |
| 最小化到托盘后继续监控 | `gui.hide_to_tray` + `pystray` |

### 聊天框（按需扫描）

| 验收项 | 实现 |
|---|---|
| 只有用户主动触发才扫描 | `ChatScanner.scan_now()`，无循环无定时器 |
| 每次扫描都对聊天文本做黑名单匹配 | `matcher.match_text` → `handle_hits` |
| 命中时次数 +1、UI 闪烁、弹提示 | `_process_hits` → `on_encounter` → `_apply_encounter_update` |
| 一次扫描多个命中 → 批量处理 | `handle_hits`（一次 `alert_batch`） |
| 截图/OCR 异常不炸按钮回调 | `scan_now` 全包 try/except，返回 `error` 状态 |

### 匹配

| 验收项 | 实现 |
|---|---|
| `match_text("PlayerX has joined the game")` 匹配到 PlayerX | ✅ 有测试 |
| 支持带空格的玩家名（John Doe） | ✅ 相邻词组合 + 整段包含 |
| 同一玩家名一次 match_text 不重复返回 | `seen_ids` 去重 |
| 精确 100 分，模糊 ≥ 85 才返回 | `MATCH_THRESHOLD = 85` |

### 自动更新 / UI 反馈 / 重置

| 验收项 | 实现 |
|---|---|
| 命中后 encounter_count +1、last_seen 更新、写 encounters | `record_encounter`（单事务） |
| 更新是原子的、并发不丢计数 | `BEGIN IMMEDIATE` + WAL（有 8 线程 × 25 次并发测试） |
| 命中后 100 ms 内 UI 更新 | `root.after(100)` 排空队列 |
| 命中行闪烁约 3 秒后恢复 | `FLASH_TIMES 6` × `FLASH_INTERVAL_MS 250` |
| 后台线程不直接操作 tkinter | 只有 `queue.Queue` 两个入口 |
| 右键清零/重置只影响当前条目 | `reset_encounter_count` / `reset_last_seen` |

### 会话时序

| 验收项 | 实现 |
|---|---|
| ESC 设置菜单，会话数秒内自动终止 | 连续 4 次 × 0.5 s 无结果 ≈ 2 s |
| ESC 玩家列表，滚动结束后约 3.5 秒终止 | `keep_alive_after_hit = 3.5` |
| ESC 会话不超 8 秒 | `max_duration = 8.0` |
| 会话内同一玩家不重复提示 | `seen_names` 去重 + 每会话只匹配一次 |
| OCR 异常不会导致线程挂死 | 每轮 try/except + 连续异常计数终止 |
| 会话有完整开始/结束日志 | `[Session] 开始/结束 source=... 原因=... 耗时=...` |
| 上次会话未结束时再次触发 → 先终止旧会话 | `ScanScheduler.start_session` |

---

## 18. 测试

```bat
:: 全部（514 个用例）
python -m unittest discover -s tests -v

:: 分类
python -m unittest tests.test_core       :: 配置 / 数据库 / 匹配 / 导入导出
python -m unittest tests.test_pipeline   :: 截图 / OCR / 会话 / 按需扫描 / 去重 / 批量 / ESC
python -m unittest tests.test_notifier   :: 模板 / 渲染 / 无焦点窗口 / 音效 / 批量堆叠
python -m unittest tests.test_gui        :: 主界面 / 通知设置 / 校准器 / 导入导出 GUI
python -m unittest tests.test_theme      :: 配色 / 样式 / 应用图标
python -m unittest tests.test_hotkey     :: 快捷键配置 / 组合键 / 按键捕获 / 全局热键
python -m unittest tests.test_single     :: 单实例互斥 / 唤醒已有窗口 / 启动入口接线
python -m unittest tests.test_priority   :: 进程与线程优先级 / 游戏友好开关
python -m unittest tests.test_e2e        :: 端到端装配与数据流

:: 真实 OCR 验证（需要装好 rapidocr；会渲染样图并打印识别结果）
python tools/ocr_probe.py

:: 全局热键真机验证（注册 F8 → SendInput 模拟按键 → 注销）
python tools/hotkey_probe.py

:: 拿真实证据截图复盘 OCR 效果（原始碎片 / 合并后名字 / 被丢掉的碎片）
python tools/diagnose_ocr.py 10          :: 只看最近 10 张
```

测试使用 **假的截图器与假的 OCR**，不需要游戏、不需要真实屏幕内容，
其中「无焦点 Overlay」的用例会真的创建 Win32 分层窗口并校验
`WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST`
四个样式位，以及 **弹出提示前后前台窗口保持不变**（不抢焦点）。

---

## 许可与免责

代码以 [MIT 许可证](LICENSE) 开源。

仅供个人学习与自我保护使用。使用者需自行确认并承担
因使用本工具而产生的全部风险与后果。

> ⚠️ 本工具虽然不注入进程、不读内存、不改包，但**任何第三方自动化工具都可能
> 违反游戏的服务条款（ToS）**。是否使用、以及由此产生的任何后果，请自行评估。
>
> 仓库内**不包含**任何玩家数据、游戏截图或第三方素材：
> 黑名单数据库、证据截图与自定义通知图/音效都在运行期生成，且已被 `.gitignore` 排除。

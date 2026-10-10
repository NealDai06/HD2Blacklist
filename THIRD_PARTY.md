# 第三方来源与致谢

本文件记录**不属于本项目自有代码**的来源。分三块：配套的游戏内插件、应用侧的依赖、
以及许可正文。

---

## 1. 配套的游戏内插件 HD2Tracker

插件与 `source_code/` 里的应用是**两个独立的项目，必须一起用**：

- **插件（游戏内）**：[**NealDai06/HD2Tracker**](https://github.com/NealDai06/HD2Tracker)
  —— 在游戏里把结构化事件写成
  `%LOCALAPPDATA%\CowboyBingus\Helldivers2\Logs\playerLog.txt`；
- **本应用（桌面端）**：只**读**这一个文件，不注入、不读内存、不改包。

缺一不可：没有插件就没有数据源；没有本应用，日志只是一份本机文本。
插件的构建、配置与已知限制见它自己仓库的 README。

| 来源 | 关系 | 许可 |
|---|---|---|
| [**360de250/Helldivers2-SteamID-Tracker**](https://github.com/360de250/Helldivers2-SteamID-Tracker)（mod 名 **kick**，v20） | **插件的本体框架由这个项目扩张而来**：抓取、事件、输出这三段骨架沿用了它的做法与结构（原始源码留档在插件的 `ref/kickv16_source.lua`，只读不改） | **MIT** |
| [**CowboyBingus/BetterLobbyManagement**](https://github.com/CowboyBingus/BetterLobbyManagement)（v1.2） | **受它启发**；并且直接借用了它标定的 `game.dll` 布局（名册指针 `0x347ced8`、4 槽位 × `0xC0`、`+8` 为 persona name）与两段代码签名来做自校验 | **0BSD** |
| [Bingus Shared Loader](https://github.com/CowboyBingus) | 插件的**加载器**，由玩家自行安装；本项目不随包分发它 | — |

**致谢 360de250 与 CowboyBingus 两位作者。** 没有前者那份 `kick` 的骨架，
这个插件不会存在；没有后者把 `game.dll` 的布局标定出来、并在同一个游戏版本上实测过，
"直接读游戏名册拿玩家名"这条路我们也走不通。

上游 `kick` 用的是 MIT 许可，该许可要求**保留版权声明与许可正文**，
因此全文照录于下。

### 上游许可正文①：Helldivers2-SteamID-Tracker（MIT）

```
MIT License

Copyright (c) 2026 360de250

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 上游许可正文②：BetterLobbyManagement（0BSD）

0BSD **不要求**署名或保留声明，我们仍然在此致谢，并把它的许可原文一并留档：

```
Zero-Clause BSD
=============

Copyright (C) 2026 by CowboyBingus

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

> 另：`kick` 自己的 README 写明"可随意解包更改优化进一步做出更优秀 mod"，
> 我们正是在这句话的许可下把它扩成了现在这个事件驱动版本。

---

## 2. 应用侧用到的第三方库

应用**不把这些库的源码收进仓库**，而是让用户通过 `pip install -r requirements.txt`
装（开发机上另有一份 `.pylibs/`，属于本地依赖目录，`git` 忽略）。
下表取自本机安装元数据（`METADATA` 里的许可字段），并非转述：

| 库 | 用途 | 许可 |
|---|---|---|
| Pillow (pillow) | 提示浮层的渲染、图标生成 | MIT-CMU |
| numpy | 把 PIL 图转成 GDI 位图 | BSD-3-Clause（并含 0BSD / MIT / Zlib / CC0-1.0 组件） |
| mss | 取显示器几何（**不是**抓游戏画面） | MIT |
| RapidFuzz | 名字模糊匹配（老条目兼容层，不参与告警） | MIT |
| pystray | 系统托盘图标 | LGPLv3 |
| winotify | 桌面通知兜底 | MIT |
| pygame | 自定义音效播放 | LGPL |

其中 `pystray` / `pygame` 是 **LGPL**：本项目只是把它们当库用（`import` 动态加载），
没有改动、也没有把它们的代码并进本项目源码；打包成 exe 时它们以独立文件形式
放在 `_internal/` 里，可被替换。分发 exe 时请把本文件一起带上
（`build.py` 会自动复制到产物旁边）。

---

## 3. 免责

本项目仅供个人学习与自我保护使用。使用者需自行确认并承担因使用本工具
（以及配套的游戏内插件）而产生的全部风险与后果 —— 包括但不限于游戏服务条款
（ToS）方面的问题。详见 `README.md` 的「许可与免责」一节。

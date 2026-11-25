<p align="center">
  <img src="assets/banner.svg" alt="SHINOBU（忍野忍）— 零依赖本地伴生微内核" width="100%" />
</p>

<h1 align="center">Shinobu（<code>shinobu-core</code>）</h1>

<p align="center">
  <strong>零第三方依赖微内核 AI 伴侣运行时 —— 一份同源底座，既可纯本地 3 分钟即玩，也可无缝升级 7×24 隐私隔离云端中枢。</strong>
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-v1.0.0-be123c" alt="Version v1.0.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e11d48.svg" alt="License: MIT" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B%20(0%20deps)-3776AB.svg" alt="Python 3.10+ (0 deps)" /></a>
  <img src="https://img.shields.io/badge/network-127.0.0.1%20%2F%20Tailscale-059669" alt="Loopback and Tailscale" />
  <img src="https://img.shields.io/badge/telegram-4--Lock%20Isolated-0284c7" alt="Telegram 四重隔离锁" />
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> · <a href="README_ZH.md"><strong>简体中文</strong></a> · <a href="deploy/gcp_setup.md">GCP 7×24 云端部署指南</a> · <a href="CHANGELOG_ZH.md">更新日志</a>
</p>

---

## 🔒 零信任安全自检（3 秒审完底座）

`shinobu-core` 从第一行代码起即贯彻物理隔离与可审计边界，任何人都能在 3 秒内完成源码体检：

1. **核心零第三方依赖（`dependencies = []`）**：守护进程、Web 记忆本、SQLite 记忆与提醒库、混动路由、边端节点与 Telegram 桥接 100% 基于 Python `>= 3.10` 标准库构建，零外部 `pip` 供应链风险；仅可选悬浮桌宠需要安装 `.[desktop]`（`PySide6>=6.6`）。
2. **`127.0.0.1` 回环守护与边端硬护栏**：
   - 本地与云端守护默认仅绑定 `127.0.0.1` 回环地址，绝不对公网裸露端口；
   - `EdgeNodeServer`（`shinobu node`）**硬拒绝 `0.0.0.0` 与任何公网 IP**，所有执行严格限定在 `--allowed-root` 白名单目录内，并彻底禁用 `shell=True`。
3. **护 KV L0 前缀冻结**：内置记忆与提醒工具（`build_memory_tools`）采用尾插式注册，绝不改写冻结的 L0 系统提示词前缀，守护长会话 Prompt KV 缓存命中率。
4. **Telegram 四重物理隔离锁**：
   - **禁入群**：拒绝一切群组、超级群组与频道消息；
   - **单人白名单**：仅放行配置的主人 Chat ID；
   - **禁转发**：拦截转发消息，防范间接提示词注入；
   - **零媒体永不 `getFile`**：丢弃媒体附件，永不向 Telegram 拉取你的私密图片或文件。
5. **纯明文源码发布**：官方仅发布可直接审查的明文 Python 源码，不提供闭源二进制安装包。
6. **3 秒源码体检命令**：

```powershell
# PowerShell — 一键排查所有网络外联、子进程、监听地址与媒体下载边界：
Get-ChildItem .\shinobu_core -Recurse -Filter *.py | Select-String -Pattern "HTTPConnection|urlopen|subprocess|getFile|0\.0\.0\.0|shell=True"
```

```bash
# Linux / macOS
grep -rnE "HTTPConnection|urlopen|subprocess|getFile|0\.0\.0\.0|shell=True" ./shinobu_core
```

---

## ✨ 设计理念与核心优势：为什么它不只是一个“桌宠”？

市面上大多数桌面宠物或 AI 聊天前端停留在**“只能闲聊卖萌的静态人设卡”**——既缺乏真实的工程干活能力，换一个窗口或软件又立刻失忆。`shinobu-core` 的核心设计理念恰恰相反：把 **「原生工作能力」** 与 **「跨窗口长期工作记忆」** 沉进零依赖微内核，让桌宠、Web 记忆本与手机 Telegram 只是同一个“活体工作伴侣大脑”的不同入口：

- **原生工作能力（不止于闲聊）**：依托原生 Antigravity 驱动、双向 MCP 工具枢纽与受控白名单本机边端节点（`--allowed-root` + L0/L1/L2 分级审批护栏），它不仅能陪聊与提醒，更能真正调用工具、处理工作区任务，甚至在你离开电脑时通过手机远程审批并调度本机执行。
- **跨窗口长期工作记忆（告别换窗失忆）**：长期项目上下文、技术偏好、关键事实与日程经睡眠记忆蒸馏沉淀在本地 SQLite 记忆库中——不仅在桌宠、Web 记忆本、Telegram 和外部 `/v1` 前端之间实时互通，更能通过 `shinobu mcp-serve` / `POST /mcp` 反向注入任意 AI IDE 或新开会话窗口，彻底终结“开新窗就从头介绍”的上下文割裂。

具体工程特性一览：

1. **原生非反代 Google Antigravity 官方驱动（`agy_cli` / `agy_sdk`）+ 多引擎自由切换**：绝不采用逆向反代、抓包伪造或窃取 Token 的高危灰产方案，直接基于官方 `agy` CLI（持久化 `stream-json` 常驻进程、`shinobu node` 默认引擎）与官方 `google-antigravity` Python SDK（懒加载可选 `.[agy-sdk]`，支持 `gemini` / `openai_local` / `litert` 模式）驱动；全部运行目录与会话映射物理隔离在 `$SHINOBU_HOME/runtime/`，绝不污染你的主 Antigravity 工作区，同时兼容任意标准 `openai_compat`（云端 API / 本地 Ollama / vLLM）与 `--engine hybrid` 混动路由。
2. **多端共享同一个“活体灵魂”，彻底告别静态人设卡**：一个持久运行的后台大脑 + 本地 SQLite 记忆库，同时驱动内置 Web 记忆本（`shinobu ui`）、透明悬浮桌宠（`shinobu pet`）、手机 Telegram（`shinobu telegram`）以及任何外部 OpenAI `/v1` 前端——无论在哪一端聊天，记忆、提醒与语音状态实时同源。
3. **护 KV 缓存 L0 灵魂冻结 + 睡眠记忆蒸馏**：核心灵魂前缀经 SHA-256 冻结锁定，内置工具采用尾插式注册，最大程度守护长会话 Prompt KV 缓存命中率（降延迟、省 Token）；配合 `shinobu sleep` 与记忆蒸馏机制，把冗长对话提纯沉淀为结构化事实与剧集，避免无休止堆叠原始聊天记录。
4. **一份零依赖同源底座，本地即玩 ↔ 云端 7×24 混动无缝升级**：核心 `dependencies = []` 纯 Python 标准库实现；纯本机 3 分钟即可跑全套，上云后开启 `--engine hybrid` 混动路由——个人电脑开机时自动借力本机原生 `agy_cli` 算力与边端触手，关机或休眠时秒级切回云端大脑不掉线。
5. **原生双向 MCP 枢纽（既是客户端，也是服务端）**：既可通过 `shinobu mcp` 一键探测并无损安装零 Key 社区精选 MCP 工具集（支持与 `agy` 桥接），也可通过 `shinobu mcp-serve`（stdio）或云端 `POST /mcp` 将 Shinobu 的记忆与提醒反向暴露给外部 AI IDE / Agent 协作调用。
6. **主动心跳、自然语言定提醒与三端按需情感语音**：支持聊天中随口定时提醒（`schedule_reminder` / `list_reminders`）与 Telegram `/remind`；Web 记忆本、桌宠抽屉与 Telegram 语音条三端原生打通 `POST /api/tts` 按需语音合成，未配语音后端时优雅提示、零打扰。
7. **插件故障隔离与开箱自检**：外部私有扩展通过标准 `entry_points` 挂载，任何单个插件异常均被微内核隔离，绝不拖垮主进程；内置 `shinobu doctor` 一键诊断原生 `agy_sdk`/`agy_cli` 驱动、隔离目录与插件健康并自动脱敏密钥。

---

## 🧭 架构总览：一份底座，本地与云端同源一体

同一份 `shinobu-core` 代码无需分叉：在个人电脑上即可独立运行完整记忆、语音播报与桌宠；部署到云端后摇身一变为 7×24 常驻大脑，并在本机开机时自动牵手边端触手：

```text
                       ┌─────────────────────────────────────────────┐
                       │         shinobu-core（核心 0 第三方依赖）    │
                       │   微内核 · SQLite 记忆/提醒 · 按需 TTS 路由  │
                       │   编排器（护 KV L0 前缀冻结） · 远程 MCP     │
                       └──────────────────────┬──────────────────────┘
                                              │
            ┌─────────────────────────────────┴─────────────────────────────────┐
            ▼                                                                   ▼
┌───────────────────────────────┐                               ┌───────────────────────────────┐
│   Level 1：纯本地 3 分钟体验   │                               │   Level 2：7×24 云端常驻中枢   │
│   （无需云端 / 无需公网域名）   │                               │  （Tailscale Serve 零公网端口）│
├───────────────────────────────┤                               ├───────────────────────────────┤
│ • shinobu serve (127.0.0.1)   │                               │ • shinobu serve --engine      │
│ • shinobu ui（Web 记忆本 +     │      可选本机边端触手          │   hybrid（混动旗舰路由）        │
│   聊天气泡 🔊 朗读）           │  ◄──── shinobu node ────────► │ • shinobu telegram（四锁隔离、  │
│ • shinobu pet（桌面悬浮桌宠 +  │  （私有 Tailscale 组网；       │   /remind、/voice、L1 审批）   │
│   抽屉朗读，PySide6 可选）     │   本机关机秒级无缝切回云端）    │ • POST /mcp 与 GCS 每日热备     │
└───────────────────────────────┘                               └───────────────────────────────┘
```

---

## ⚡ 快速上手 —— Level 1：纯本地 3 分钟体验

不需要云服务器、不需要 Telegram Bot，也不需要开放任何端口，在本机即可直接体验。

### 1. 安装 `shinobu-core`（零依赖）

```bash
git clone https://github.com/zanex-dev/shinobu.git
cd shinobu
pip install -e .
```

### 2. 启动回环守护进程与 Web 记忆本

```bash
# 可选：一键检查本机原生 Antigravity 驱动（agy_cli / agy_sdk）与隔离目录
shinobu doctor

# 终端 1 — 在 127.0.0.1:18790 启动本地守护进程
# 默认 mock 即可离线体验；接入真实大脑可直接指定原生非反代驱动或标准 OpenAI 兼容接口：
shinobu serve --engine agy_cli        # 官方 Antigravity CLI（零反代、独立沙盒工作区）
# 或: pip install -e ".[agy-sdk]" && shinobu serve --engine agy_sdk
# 或: shinobu serve --engine openai_compat

# 终端 2 — 打开内置 Web 记忆本控制台
shinobu ui
```

打开 Web 记忆本即可对话、管理记忆与提醒；每条伴侣回复气泡自带 **`🔊 朗读`** 按钮，直连 `POST /api/tts` 原生播放（未配置 TTS 后端时会给出友好提示，不影响文字聊天）。

### 3. 可选：启用桌面悬浮桌宠

仅当你需要桌面悬浮窗与聊天抽屉时，再按需安装 PySide6 可选依赖：

```bash
pip install -e ".[desktop]"
shinobu pet
```

桌宠聊天抽屉工具栏同样内置 **`🔊 朗读`** 按钮，一键朗读最新一条伴侣回复（Windows 端基于标准库 `winsound` 零额外依赖播放，其他平台或未配置 TTS 时优雅提示）。

---

## ☁️ 进阶形态 —— Level 2：7×24 云端中枢 + 本机边端触手

当你希望手机随时能联系到常驻大脑，同时在电脑开机时自动借力本机算力与本地工作区：

1. **零公网入站端口**：在轻量云主机（例如 GCP `e2-small` 搭配 **Standard Tier**，享每月 200 GiB 免费出站）运行 `shinobu serve --engine hybrid`，通过 **Tailscale Serve** 仅在私有尾网内提供 HTTPS 接入。
2. **四重物理锁 Telegram 手机入口**：运行 `shinobu telegram`，在手机端直接聊天、开关语音条（`/voice on` / `/voice off`）、随口定提醒（`/remind 20m 起来喝水`）、查看在线节点（`/nodes`），并在触发 L1 命令时点击内联 `[✅ 批准]` / `[❌ 拒绝]` 完成远程审批。
3. **本机 PC 边端触手无缝衔接**：在本机运行 `shinobu node` 自动报到；本机在线时云端混动引擎优先调度本机原生引擎，本机关机或休眠时自动注销并秒级切回云端旗舰引擎。
4. **GCS SQLite 每日在线热备**：通过定时 `.backup` 快照自动备份记忆与提醒库。

👉 完整避坑部署手册请参阅：**[`deploy/gcp_setup.md`](deploy/gcp_setup.md)**。

---

## 📖 命令、Telegram 指令与 API 速查

### CLI 子命令（`shinobu`）

| 命令 | 场景 | 说明 |
|---|---|---|
| `shinobu init` | 初始化 | 初始化灵魂模板与本地 SQLite 记忆库。 |
| `shinobu chat` | 本地终端 | 在终端直接与 Shinobu 单轮或交互式对话。 |
| `shinobu sleep` | 记忆蒸馏 | 对当前活跃剧集执行睡眠记忆提炼、合并归档并平滑收口。 |
| `shinobu serve` | 本地 / 云端 | 启动 `127.0.0.1` 回环 HTTP/SSE/MCP 守护进程（支持 `--engine hybrid` 等）。 |
| `shinobu ui` | 本地 | 打开由守护进程托管的零依赖内置 Web 记忆本页面。 |
| `shinobu pet` | 本地（可选） | 启动透明桌面悬浮桌宠与聊天抽屉（需先安装 `.[desktop]`）。 |
| `shinobu compat` | 第三方前端 | 打印本机 OpenAI `/v1` 兼容端点与事件流连接信息（默认模型 ID：`shinobu`）。 |
| `shinobu mcp` | 工具生态 | 查看、探测、无损安装精选零 Key 社区 MCP 预设或管理 MCP 桥接。 |
| `shinobu mcp-serve` | 反向 MCP | 通过 `stdio` 将 Shinobu 暴露为标准 MCP 服务端，供外部 AI 客户端调用。 |
| `shinobu node` | 边端触手 | 将本机作为受控边端节点接入云端（`--cloud-url`、`--bind-host`、`--allowed-root`）。 |
| `shinobu telegram` | 云端 / 本地 | 启动四重物理锁隔离的 Telegram 长轮询桥接进程。 |
| `shinobu status` / `doctor` | 运维自检 | 查看运行状态与插件清单，或一键体检驱动、隔离边界与脱敏凭据健康。 |

### Telegram 内置指令

| 指令 | 说明 |
|---|---|
| `/status` | 查看云端运行状态、活跃引擎与待审批数量。 |
| `/nodes` | 查看当前在线的本机 PC / Colab GPU 节点。 |
| `/voice on` / `/voice off` | 按需开关语音条回复。 |
| `/remind` / `/reminders` | 查看 SQLite `timemap` 中的待触发提醒列表。 |
| `/remind <10m\|2h\|HH:MM\|ISO> <内容>` | 新建提醒（支持相对时长、本地 `HH:MM` 自动顺延次日、或 ISO-8601）。 |

### 核心 HTTP / MCP 与工具契约

| 接口 / 工具 | 说明 |
|---|---|
| `GET /v1/models` & `POST /v1/chat/completions` | OpenAI `/v1` 兼容对话接口（带本地回环白名单 CORS 保护，默认模型 `shinobu`）。 |
| `POST /api/tts` | 按需语音合成（入参 `{"text", "mood", "fmt": "ogg_opus"\|"mp3"\|"wav"}`），成功返回音频字节，未配置返回 `503 {"error": "tts_unavailable"}`。 |
| `GET` / `POST /api/timemap` | 读写 SQLite `timemap` 提醒/日程表。 |
| `POST /mcp` | 远程 MCP 协议入口（经 Tailscale + Bearer Token 鉴权）。 |
| `build_memory_tools`（5 工具） | 记忆读写 + 尾插式 `schedule_reminder` / `list_reminders`（严守 L0 前缀冻结）。 |

### 🔌 接入第三方前端（SillyTavern / Open WebUI / VTuber 壳）

`shinobu-core` 默认完全独立运行，不依赖任何第三方前端；如果你仍希望在 **SillyTavern（傻酒馆）**、Cherry Studio、Open WebUI 或本机 VTuber/桌宠壳（如 Project AIRI、Open-LLM-VTuber、VPetLLM）中直接复用同一套灵魂与记忆，运行 `shinobu compat` 获取本机 `/v1` 地址与 Bearer Token 填入客户端即可无缝迁入。

> **对接避坑提示**：接入第三方客户端时，请将其视为**纯粹的对话皮肤**——建议关闭前端侧的**向量记忆 / 自动摘要 / 动态世界书注入、前端状态栏宏以及客户端内置 TTS 插件**，将记忆蒸馏、语音合成与护 KV L0 前缀冻结全权交由 Shinobu 后端托管，避免双头记忆冲突或频繁改写系统提示词击穿 Prompt KV 缓存。

---

## 🧩 插件扩展与私有隔离边界

`shinobu-public`（`shinobu-core`）仅包含纯白帽通用微内核底座；个人人设、私有传感器、定制钩子或个人笔记均通过标准 Python `entry_points` 物理隔离在外部私有扩展包中加载：

```toml
[project.entry-points."shinobu.plugins"]
my_extension = "my_pkg.plugin:create_plugin"
```

---

## ⚠️ 免责声明与开源许可

- 本项目为开源技术框架，仅用于个人 AI 运行时编排与本地/云端自动化实验。请使用者自行妥善保管 API 密钥、配置云端预算告警与防火墙策略，并遵守所用第三方平台的服务条款。
- 严禁将任何真实密钥、Bearer Token、个人路径或私有 Tailscale 域名提交至公开仓库。
- 本项目基于 [MIT 许可证](LICENSE) 发布 © 2026 zanex (zanex-dev)。

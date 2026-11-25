# 更新日志

本文件记录 `shinobu-core`（`shinobu-public`）的所有重要版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.1.0] - 2026-11-25

在严守核心零第三方依赖（`dependencies = []`）的前提下，补齐语音网关、Colab GPU 语音工坊、社区前端联调、自定义差分立绘安装器与跨平台零依赖 CI。

### 新增功能
- **可选本地 OpenAI 兼容 TTS 网关（`.[tts-edge]` / `shinobu tts-gateway`）**：
  - 新增 `shinobu_core/cloud/tts_gateway.py` 与可选依赖 `tts-edge = ["edge-tts>=6.1"]`（仅在本地回落合成时懒加载，未安装不影响核心导入或 GPU 上游转发）。
  - 注册 `shinobu tts-gateway` CLI 子命令（`--host`/`--port`/`--voice`/`--upstream-config`/`--set-upstream`/`--upstream-key`/`--clear-upstream`/`--show-upstream`/`--json`），提供 `/v1/audio/speech`、`/v1/models`、`/health`，支持远程 Colab GPU 优先转发与本地免费 Edge-TTS 自动回落，并经 `validate_bind_host` 校验回环绑定。
- **Colab GPU 语音工坊与 Project AIRI 联调套件**：
  - 新增 `notebooks/shinobu_colab_tts_worker_v2.ipynb`（云端回传模式）、`notebooks/shinobu_colab_tts_local_l4.ipynb`（本机 L4/T4 直连模式）与 `notebooks/README.md`（均清空代码单元输出、零个人路径）。
  - 新增 `deploy/airi_tts_guide.md` 与全环境变量参数化启动脚本 `deploy/launch_local_airi_tts.ps1`，支持一键将本地 `shinobu tts-gateway` 接入 Project AIRI 等兼容 OpenAI TTS 协议的前端壳。
- **通用差分立绘包安装器（`shinobu avatar`）与跨平台零依赖 CI**：
  - 新增 `shinobu_core/ui/avatar_installer.py` 与 `shinobu avatar [--source DIR] [--name custom] [--force] [--json]`，支持校验并一键导入自定义多情绪 PNG/WebP 差分立绘包（仓库不含任何二进制立绘素材）。
  - 新增 `.github/workflows/ci.yml`，在 Linux / Windows / macOS × Python 3.10 / 3.11 / 3.12 矩阵上以零第三方依赖（`SHINOBU_DISABLE_PLUGINS=1`）运行全量单测与门禁校验。
- **双语自选清单文档**：在 `README.md` 与 `README_ZH.md` 补充「默认零依赖 vs 推荐自选」四维矩阵表、新增 CLI 速查项与 Colab / AIRI 指引链接。

---

## [1.0.0] - 2026-11-25

`shinobu-core` 首个正式开源稳定版：零第三方依赖的同源微内核底座，兼顾纯本地 3 分钟体验与 7×24 隐私隔离云端常驻。

### 新增功能
- **同源一体零依赖微内核（`dependencies = []`）**：内置守护进程（`shinobu serve`）、`shinobu.plugins` 插件扩展入口、SQLite 记忆与提醒存储、混动引擎路由、本机边端节点（`shinobu node`）、远程 `POST /mcp` 与可选桌面桌宠（`.[desktop]` -> `PySide6>=6.6`）。
- **提醒工具与 Telegram `/remind` 指令（T01）**：
  - 在 `build_memory_tools` 中尾插新增 `schedule_reminder` 与 `list_reminders`（共 5 个工具），严守 L0 前缀冻结不伤 KV 缓存。
  - 在 `TelegramBotBridge` 与 CLI 中接入 `/remind`（含 `/reminders`）指令及 `on_remind` 回调，支持 `<N>m`、`<N>h`、本地 `HH:MM`（已过自动顺延次日）与 ISO-8601，直接桥接 `GET/POST /api/timemap`。
- **电脑端 Web 记忆本与桌宠 `/api/tts` 语音播报（T02）**：
  - 在 `DaemonClient` 新增 `synthesize_tts(text, *, mood="calm", fmt="wav", timeout=None) -> tuple[bytes, str]` 对接 `POST /api/tts`。
  - Web 记忆本每条伴侣回复气泡新增 `🔊 朗读` 按钮（原生 `Audio` 单实例播放/停止，503 友好 toast 降级）。
  - 桌宠聊天抽屉新增 `🔊 朗读` 按钮及纯 Python `extract_last_assistant_text` / `play_audio_bytes` 辅助（Windows 标准库 `winsound.SND_MEMORY` 零新增依赖播报，非 Windows 或未配置 TTS 时优雅气泡提示）。
- **7×24 云端常驻部署套件**：提供 `deploy/gcp_setup.md`、systemd 服务模板与 GCS SQLite 每日在线热备脚本，支持 Tailscale Serve 零公网入站端口部署。
- **双语开源门面与首发收口（T03）**：补齐中英双语 `README`、`CHANGELOG`、MIT `LICENSE` 与防泄漏 `.gitignore`，版本升至 `1.0.0`。

### 安全与隐私
- 守护进程默认 `127.0.0.1` 回环绑定，`EdgeNodeServer` 硬拒绝 `0.0.0.0` 及公网 IP、禁用 `shell=True` 并强制限定 `--allowed-root`。
- Telegram 桥接内置四重物理隔离锁（禁入群、单人白名单、禁转发、零媒体永不 `getFile`）。
- 完成公开仓全量隐私脱敏体检，确保零个人路径、零真实私有域名、零密钥/Token 与零私有扩展泄漏。

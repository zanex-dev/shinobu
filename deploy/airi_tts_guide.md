# Project AIRI / 社区桌面壳与双模语音网关接入指南（自选扩展）

> **完全自选，非必需**：Shinobu 默认通过 CLI 与内置 Web Companion Studio 即可完整工作。如果你希望为 Shinobu 接上 Live2D / VRM 桌面形象（如 **Project AIRI**、**Open-LLM-VTuber**、**VPetLLM**）以及实时语音朗读，可按本指南将本地大脑与自选双模语音网关连入任意 OpenAI 兼容客户端。

---

## 1. 本地服务端口与拓扑

| 组件 | 默认地址 | 启动命令 | 作用 |
|---|---|---|---|
| **Shinobu 大脑守护进程** | `http://127.0.0.1:18790/v1` | `shinobu serve --port 18790` | 提供 OpenAI 兼容 `/v1/chat/completions`、记忆系统、工具与审批流 |
| **双模语音网关（自选）** | `http://127.0.0.1:18791/v1` | `shinobu tts-gateway --port 18791` | 统一暴露 `/v1/audio/speech`，默认走本地免费 `Edge-TTS`，可热切 Colab GPU 声线并在离线时自动回落 |
| **主动气泡转发（自选）** | 由 `--airi-bubble-url` 指定 | `shinobu serve --port 18790 --airi-bubble-url <URL>` | 将 Shinobu 的主动陪伴/提醒消息实时推送至桌面壳气泡通道 |

---

## 2. 启动本地大脑与语音网关

### 方式 A：手动终端启动（跨平台通用）

1. 启动主脑守护进程（若需把主动提醒推送给桌面壳，加上 `--airi-bubble-url`）：
   ```powershell
   shinobu serve --port 18790
   # 如需开启主动气泡转发：
   # shinobu serve --port 18790 --airi-bubble-url http://127.0.0.1:6121/api/bubble
   ```
2. 查看外部壳所需的 OpenAI 兼容连接信息（Base URL / API Key / Model ID）：
   ```powershell
   shinobu compat
   ```
3. （可选）在新终端启动双模语音网关：
   ```powershell
   shinobu tts-gateway --port 18791
   ```
   核验语音网关健康状态：
   ```powershell
   curl http://127.0.0.1:18791/health
   ```

### 方式 B：Windows 一键启动模板

仓库内置了去硬编码、全环境变量参数化的启动脚本 [`deploy/launch_local_airi_tts.ps1`](launch_local_airi_tts.ps1)，可自动完成：
1. 检查并拉起 `127.0.0.1:18790` Shinobu 大脑守护进程；
2. 检查并拉起 `127.0.0.1:18791` 双模语音网关；
3. 打印 `shinobu compat` 连接参数并打开 Companion Studio；
4. 若本机已安装 Project AIRI（默认 `$env:LOCALAPPDATA\Programs\airi\airi.exe`），自动启动桌面壳。

运行示例：
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\launch_local_airi_tts.ps1
```

常用环境变量覆盖（均可选）：
- `SHINOBU_PORT`：主脑端口（默认 `18790`）
- `SHINOBU_TTS_PORT`：语音网关端口（默认 `18791`）
- `SHINOBU_AIRI_EXE`：自定义 AIRI 可执行文件路径
- `SHINOBU_AIRI_BUBBLE_URL`：传递给 `shinobu serve --airi-bubble-url` 的主动气泡转发地址
- `SHINOBU_SKIP_TTS=1` / `SHINOBU_SKIP_AIRI=1` / `SHINOBU_SKIP_UI=1`：按需跳过对应步骤

---

## 3. Project AIRI 推荐默认配置

在 **Project AIRI → Settings → Providers** 中按以下推荐值填写：

### 3.1 大脑（Chat / LLM Provider）

| 配置项 | 推荐值 | 说明 |
|---|---|---|
| Provider 类型 | `OpenAI Compatible` | 标准 OpenAI Chat Completions 协议 |
| Base URL | `http://127.0.0.1:18790/v1` | 与 `shinobu serve --port` 一致 |
| API Key | 见 `shinobu compat` 输出 | 填入守护进程展示的本地令牌 |
| Model | 见 `shinobu compat` 输出 | 使用统一伴侣模型 ID，由 Shinobu 内部路由具体后端 |

### 3.2 语音（Speech / TTS Provider，自选）

| 配置项 | 推荐值 | 说明 |
|---|---|---|
| Provider 类型 | `OpenAI Compatible (Speech)` | 标准 `/v1/audio/speech` 协议 |
| Base URL | `http://127.0.0.1:18791/v1` | 指向本地双模语音网关 |
| API Key | `local-edge-tts` | 本地网关默认占位密钥（如自定义请保持一致） |
| Model | `tts-1` | OpenAI Speech 兼容模型名 |
| Voice | `shinobu` | 网关内置声线别名；亦可直接填 `zh-CN-XiaoyiNeural`、`ja-JP-NanamiNeural` 等 Edge-TTS 声线 |

### 3.3 主动气泡转发（`--airi-bubble-url`，自选）

如果你希望 Shinobu 的定时提醒、后台任务完成通知或主动搭话直接以气泡形式浮现在桌面角色上方：
1. 在桌面壳或桥接插件中确认接收气泡文本的 HTTP 端点（例如 `http://127.0.0.1:<port>/...`）；
2. 启动主脑时传入 `--airi-bubble-url <该地址>`（或在使用 `launch_local_airi_tts.ps1` 前设置 `$env:SHINOBU_AIRI_BUBBLE_URL="<该地址>"`）。
3. 未配置 `--airi-bubble-url` 时，该能力默认关闭，不影响正常聊天与工具调用。

---

## 4. 其他社区壳接入示例

### Open-LLM-VTuber

在 `conf.yaml` 中将大语言模型与语音配置指向本地网关：
```yaml
character_config:
  agent_config:
    llm_configs:
      openai_compatible_llm:
        base_url: "http://127.0.0.1:18790/v1"
        llm_api_key: "<从 shinobu compat 获取>"
        model: "<从 shinobu compat 获取>"
  tts_config:
    tts_model: "openai_tts"
    openai_tts:
      base_url: "http://127.0.0.1:18791/v1"
      api_key: "local-edge-tts"
      model: "tts-1"
      voice: "shinobu"
```

### VPetLLM / 其他 OpenAI 兼容桌宠壳

- **LLM 接口地址**：`http://127.0.0.1:18790/v1`
- **API Key / 模型名**：执行 `shinobu compat` 复制对应字段
- **TTS 接口地址（若支持 OpenAI Speech）**：`http://127.0.0.1:18791/v1`，模型 `tts-1`，音色 `shinobu`

---

## 5. 按需热切 Colab GPU 满血声线（零改壳配置）

当你希望从免费的本地 `Edge-TTS` 临时升级为 **IndexTTS-2.5 GPU 克隆声线** 时，完全不需要改动 AIRI 或社区壳里的任何配置：

1. 在 Google Colab 运行 [`notebooks/shinobu_colab_tts_local_l4.ipynb`](../notebooks/shinobu_colab_tts_local_l4.ipynb)（详见 [`notebooks/README.md`](../notebooks/README.md)）。
2. 复制 Cell 4 输出的热切命令在本机终端执行：
   ```powershell
   shinobu tts-gateway --set-upstream https://<your-tunnel>.trycloudflare.com/v1 --api-key <API_KEY>
   ```
3. 核验切换结果：访问 `http://127.0.0.1:18791/health`，确认 `upstream_enabled` 为 `true`。
4. 当你关闭 Colab 或网络波动超时，本地网关会在单次请求内自动无感回落至 `Edge-TTS`，不会导致桌面壳报错或静音。
5. 如需手动切回纯本地 `Edge-TTS`，执行：
   ```powershell
   shinobu tts-gateway --clear-upstream
   ```

---

## 6. 常见问题排查

- **AIRI 提示连不上 `18790`**：先运行 `shinobu ui --no-open --json --port 18790` 检查 `status` 是否为 `ready`，确认端口未被其他进程占用。
- **语音网关无声或 500**：运行 `curl http://127.0.0.1:18791/health` 检查网关存活状态，并确认已安装可选语音依赖（`edge-tts`）。
- **不想安装桌面壳**：直接执行 `shinobu ui --port 18790` 在浏览器打开内置 Companion Studio 即可使用全部核心功能。

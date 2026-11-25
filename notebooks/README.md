# Shinobu Colab GPU 声带 Notebook（纯自选扩展）

> **完全自选，非必需组件**：Shinobu 核心大脑在纯文本模式或本机免费 `Edge-TTS` 双模网关下即可完整运行。仅当你希望借助 **Google Colab 免费/按量 GPU（T4 / L4 / A100）** 体验 **IndexTTS-2.5 高表现力声线克隆** 时，才需要按需选用本目录下的 Notebook。

---

## 两份 Notebook 对比与选型

| Notebook | 适用场景 | 推荐 GPU / 精度 | 通道与接入方式 | 离线降级策略 |
|---|---|---|---|---|
| [`shinobu_colab_tts_worker_v2.ipynb`](shinobu_colab_tts_worker_v2.ipynb) | **云端部署 / Telegram 语音条**<br>已部署云端 Shinobu（见 [`deploy/gcp_setup.md`](../deploy/gcp_setup.md)），希望 Colab 开机自动接入云端 `WorkerRegistry` | T4 / L4 / A100<br>（L4/A100 自动 `bf16`，T4 自动 `fp16`） | Cloudflare 隧道或 Tailscale userspace；自动调用 `/api/workers/register` 与 `/api/workers/heartbeat` 报到，支持 `ogg_opus` / `mp3` / `wav` | 关闭单元格时自动 `unregister`，云端 `TtsRouter` 无感切回 GCP `Chirp 3: HD` 等兜底引擎 |
| [`shinobu_colab_tts_local_l4.ipynb`](shinobu_colab_tts_local_l4.ipynb) | **纯本机桌面使用**<br>本机运行 Shinobu + `127.0.0.1:18791` 双模语音网关 + Project AIRI / 社区壳，不依赖云端服务器 | L4（兼容 T4 / A100，`bf16`） | Cloudflare 临时加密隧道；在本机执行 `shinobu tts-gateway --set-upstream <URL> --api-key <KEY>` 热切换上游 | Colab 断开或超时后，本机双模语音网关自动回落本地 `Edge-TTS` |

---

## Colab Secrets 配置说明（仅 `worker_v2` 需要）

使用 `shinobu_colab_tts_worker_v2.ipynb` 时，推荐在 Google Colab 左侧边栏的 **钥匙图标（Secrets）** 中添加以下键值，并开启对应 Notebook 访问权限（也可在 Cell 4 表单临时填写，但不建议把真实密钥保存在副本中）：

| Secret 名称 | 是否必填 | 说明 |
|---|---|---|
| `SHINOBU_CLOUD_URL` | 云端模式必填 | 云端 Shinobu 服务公网地址（例如 `https://shinobu.example.com`，末尾不加 `/`）。留空时仅本地调试、不向云端报到 |
| `SHINOBU_CLOUD_TOKEN` | 强烈建议 | 云端节点注册与管理鉴权 Bearer Token，需与云端 `/etc/shinobu/cloud.env` 中的配置一致 |
| `TS_AUTHKEY` | 仅 Tailscale 模式 | 当 Cell 1 `TRANSPORT_MODE` 选 `tailscale` 时必填，填入 Tailscale Auth Key（建议开启 Ephemeral） |

> `shinobu_colab_tts_local_l4.ipynb` 无需配置任何 Colab Secrets，每次启动都会随机生成临时 Bearer API Key。

---

## 快速上手步骤

1. 在 [Google Colab](https://colab.research.google.com/) 上传所需 `.ipynb` 文件，并在顶部菜单选择 **代码执行程序 → 更改运行时类型 → 选择 GPU（T4 或 L4）**。
2. 准备一段 **5～15 秒无背景音乐的干净人声** 作为参考干音：
   - 开启 `USE_DRIVE = True`（默认）时，权重（`IndexTeam/IndexTTS-2.5`）与标准化后的 `ref.wav` 会缓存到你的个人 Google Drive（默认 `/content/drive/MyDrive/shinobu_tts`）。
   - 首次冷启动约 3～5 分钟，后续复用 Drive 缓存约 1 分钟即可就绪。
3. 点击 **全部运行**：
   - **云端版（`worker_v2`）**：等待 Cell 4 输出 `SHINOBU COLAB TTS WORKER v2 LIVE!` 且 `[Registry] register -> HTTP 200` 即可，可在 Telegram 发送 `/nodes` 确认节点在线。
   - **本地直连版（`local_l4`）**：复制 Cell 4 打印的命令在本机终端运行：
     ```powershell
     shinobu tts-gateway --set-upstream https://<your-tunnel>.trycloudflare.com/v1 --api-key <API_KEY>
     ```
     桌面壳无需修改任何设置，下一句话即刻切换 GPU 声线。

---

## 相关文档与合规提示

- 本地桌面壳与语音网关接入教程：[`deploy/airi_tts_guide.md`](../deploy/airi_tts_guide.md)
- 云端 24/7 节点与 Telegram 桥部署教程：[`deploy/gcp_setup.md`](../deploy/gcp_setup.md)
- **素材合规提醒**：参考音频仅保存在你个人的 Google Drive 中，请确保仅使用拥有合法授权的语音素材，切勿将私人干音或真实密钥提交到公共仓库。

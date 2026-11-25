# Shinobu Cloud Always-On：GCP 部署与避坑实操指南

本指南用于在 Google Cloud Platform (GCP) 上以最低成本与最高物理隔离度部署 **Shinobu 7×24 云端常驻中枢**（`HybridRouterEngine` 混动旗舰路由 + `127.0.0.1` 回环守护 + 远程 `POST /mcp` + 四重隔离 Telegram Bot 桥 + GCS SQLite 热备 + 本机 PC 边端触手）。

> [!IMPORTANT]
> 本文件为纯白帽通用模板，所有命令中的 `<YOUR_*>` 均为占位符，请替换为你的实际值，**切勿将真实密钥、Token 或私有 IP 提交至公共仓库**。

---

## 第 1 步：设置 \$35 预算告警（必做护栏）

在创建任何计算实例前，先锁定账单上限：

1. 进入 GCP Console -> **Billing（结算）** -> **Budgets & alerts（预算和提醒）** -> **Create budget**。
2. 预算金额设为 **\$35 / 月**（覆盖 `e2-small`、小容量磁盘与按需 Cloud TTS 的充足安全垫）。
3. 阈值规则配置为 **50% / 80% / 100%**（按实际与预测双触发），并勾选邮件通知。

---

## 第 2 步：创建 GCE `e2-small` 实例（务必切换 Standard Tier）

1. **规格与系统**：
   - 机型：`e2-small`（2 vCPU 共享 / 2 GB RAM，长期跑 Python 守护 + Tailscale 稳妥不 OOM）。
   - 镜像：Debian 12 或 Ubuntu 24.04 LTS（Minimal）。
   - 磁盘：20–30 GB Standard Persistent Disk（`pd-standard`）。
2. **网络层级避坑（最关键！）**：
   - 展开 **Networking -> Network interfaces -> External IPv4 -> Network Service Tier**。
   - **务必将默认的 `Premium` 切换为 `Standard Tier`**！
   - `Standard Tier` 每月包含 **200 GiB 免费出站流量**（足够全天候文字对话、MCP 调用与按需语音条）；若误用 `Premium`，跨区出站流量会产生额外费用。
3. **防火墙（0 入站端口原则）**：
   - **不要勾选** `Allow HTTP traffic` 与 `Allow HTTPS traffic`。
   - VPC 防火墙保持 **0 公网入站规则**（连 `22` / `80` / `443` / `18790` 全部不对公网暴露）；SSH 仅通过 GCP IAP 隧道或后续安装的 Tailscale SSH 访问。
4. **添加 2 GB Swap（防 `e2-small` 突发内存抖动）**：
   ```bash
   sudo fallocate -l 2G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

---

## 第 3 步：安装 Tailscale 与零信任 Serve（替代公网暴露）

`DaemonServer` 在代码层强制仅监听 `127.0.0.1:18790`。通过 **Tailscale Serve** 可自动获得私有 Tailnet 内部 HTTPS 证书，同时对外网完全不可见：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh

# 将 127.0.0.1:18790 仅发布到你的私有 Tailnet HTTPS（不开启 Funnel，不对公网开放）
sudo tailscale serve --bg http://127.0.0.1:18790
tailscale serve status
```

完成后，你将获得类似 `https://<YOUR_TAILSCALE_HOST>` 的私有 HTTPS 入口：
- 远程客户端（如未来手机端 MCP 客户端）可直接接入 `https://<YOUR_TAILSCALE_HOST>/mcp`（携带 `Authorization: Bearer <SHINOBU_TOKEN>`）。
- 你的本机 PC 边端触手也可通过同一私有 HTTPS 入口安全注册与保活。

---

## 第 4 步：安装 Shinobu 并配置 `systemd` 守护

### 4.1 创建最小权限用户与虚拟环境

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv sqlite3 git
sudo useradd --system --create-home --home-dir /var/lib/shinobu --shell /usr/sbin/nologin shinobu
sudo mkdir -p /opt/shinobu /etc/shinobu /var/lib/shinobu/home /var/lib/shinobu/sandbox /var/lib/shinobu/backups
sudo chown -R shinobu:shinobu /var/lib/shinobu

# 克隆或部署你的 shinobu-public 源码至 /opt/shinobu
sudo python3 -m venv /opt/shinobu/.venv
sudo /opt/shinobu/.venv/bin/pip install --upgrade pip
sudo /opt/shinobu/.venv/bin/pip install -e /opt/shinobu
```

### 4.2 编写 `/etc/shinobu/cloud.env`（强权限 `600`）

```bash
sudo tee /etc/shinobu/cloud.env >/dev/null <<'EOF'
# 持久化 Bearer Token（重启不失效，建议用 `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` 生成）
SHINOBU_TOKEN=<YOUR_PERSISTENT_BEARER_TOKEN>

# 混动路由云端兜底引擎（当本机 PC 关机或离线时无缝切换）
SHINOBU_CLOUD_ENGINE=openai_compat
SHINOBU_BASE_URL=<YOUR_FLAGSHIP_OPENAI_COMPAT_BASE_URL>
SHINOBU_API_KEY=<YOUR_FLAGSHIP_API_KEY>
SHINOBU_MODEL=<YOUR_FLAGSHIP_MODEL_NAME>

# 按需语音合成兜底（Colab GPU 未开机时自动降级至 GCP Chirp 3: HD 或兼容网关）
SHINOBU_GCP_TTS_VOICE=cmn-CN-Chirp3-HD-Aoede
# SHINOBU_TTS_FALLBACK_URL=<OPTIONAL_TTS_GATEWAY_URL>
# SHINOBU_TTS_FALLBACK_API_KEY=<OPTIONAL_TTS_GATEWAY_KEY>

# 四重物理隔离 Telegram Bot 配置
SHINOBU_TELEGRAM_BOT_TOKEN=<YOUR_TELEGRAM_BOT_TOKEN>
SHINOBU_TELEGRAM_CHAT_ID=<YOUR_TELEGRAM_PRIVATE_CHAT_ID>
EOF

sudo chown root:shinobu /etc/shinobu/cloud.env
sudo chmod 640 /etc/shinobu/cloud.env
```

### 4.3 启动主守护与 Telegram 桥

```bash
sudo cp /opt/shinobu/deploy/shinobu-cloud.service /etc/systemd/system/shinobu-cloud.service
sudo systemctl daemon-reload
sudo systemctl enable --now shinobu-cloud.service
sudo systemctl status shinobu-cloud.service
```

若启用 Telegram 伴随服务，可复制 `shinobu-cloud.service` 为 `/etc/systemd/system/shinobu-telegram.service`，将 `ExecStart` 改为：

```ini
ExecStart=/opt/shinobu/.venv/bin/shinobu --home /var/lib/shinobu/home telegram
```

并执行 `sudo systemctl enable --now shinobu-telegram.service`。

---

## 第 5 步：Telegram BotFather 禁入群四锁配置

为彻底杜绝群聊泄漏、转发污染与媒体误入模型上下文，请在 `@BotFather` 完成硬锁定：

1. 发送 `/newbot` 创建专属 Bot，拿到 `<YOUR_TELEGRAM_BOT_TOKEN>`。
2. 发送 `/setjoingroups` -> 选择你的 Bot -> **`Disable`**（物理禁止任何人把 Bot 拉进群聊）。
3. 发送 `/setprivacy` -> 选择你的 Bot -> **`Enable`**。
4. 私聊你的 Bot 发送任意文本，读取个人私聊 `chat.id` 填入 `SHINOBU_TELEGRAM_CHAT_ID`。

代码层 `TelegramBotBridge` 同时强制执行 **四重物理隔离锁**：
1. **仅私聊**：非 `private` 会话一律静默丢弃；
2. **单人白名单**：非 `SHINOBU_TELEGRAM_CHAT_ID` 一律静默丢弃；
3. **禁转发**：包含 `forward_*` 字段的消息一律拦截，绝不入模；
4. **零媒体盲区盾**：包含图片/视频/动图/贴纸/文件/视频笔记的消息一律拦截，**永不调用 `getFile` 下载**。

日常内置指令：
- `/status`：查看云端状态、活跃引擎与待审批数；
- `/nodes`：查看当前在线的本机 PC / Colab GPU 节点；
- `/voice on` / `/voice off`：按需开关语音条回复；
- 云端沙盒触发 L1 命令时，Telegram 自动弹出 `[✅ 批准]` / `[❌ 拒绝]` 按钮，点一下即可完成远程授权。

---

## 第 6 步：配置 GCS SQLite 每日热备

1. 创建同区域 Standard 存储桶（如 `<YOUR_GCS_BUCKET>`），在 Bucket **Lifecycle** 中设置保留策略（例如删除 30 天前旧快照）。
2. 为 GCE 服务账号授予该存储桶的 `Storage Object Creator` 最小权限。
3. 测试并配置定时任务：
   ```bash
   sudo chmod +x /opt/shinobu/deploy/backup_sqlite_gcs.sh
   sudo -u shinobu GCS_BUCKET=<YOUR_GCS_BUCKET> /opt/shinobu/deploy/backup_sqlite_gcs.sh
   ```
   写入 `/etc/cron.d/shinobu-backup`（每日 UTC 04:15 自动在线 `.backup` 快照并上传）：
   ```cron
   15 4 * * * shinobu GCS_BUCKET=<YOUR_GCS_BUCKET> /opt/shinobu/deploy/backup_sqlite_gcs.sh >> /var/lib/shinobu/backups/backup.log 2>&1
   ```

---

## 第 7 步：接入本机 PC 边端触手（`shinobu node`）

本机开机时运行以下命令，即可向云端自动报到（云端 `hybrid` 引擎优先走你本机原生 `agy_cli`，关机或 Ctrl+C 时优雅注销、云端秒级无缝切回云端旗舰引擎）：

```bash
export SHINOBU_CLOUD_TOKEN="<YOUR_PERSISTENT_BEARER_TOKEN>"
shinobu node \
  --cloud-url "https://<YOUR_TAILSCALE_HOST>" \
  --bind-host "<YOUR_PC_TAILSCALE_IP>" \
  --port 18792 \
  --engine agy_cli \
  --allowed-root "<YOUR_WORKSPACE_DIR>"
```

> [!TIP]
> `EdgeNodeServer` 内置安全护栏：**硬拒绝 `0.0.0.0` 与任何公网 IP**，仅允许回环或 Tailscale/私网地址；且所有 `/node/exec` 命令严禁 `shell=True`，强制限制在 `--allowed-root` 白名单目录与 L0/已审批 L1 范围内。

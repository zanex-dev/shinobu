# AGENTS.md — `shinobu-core` 架构边界与智能体导览规范

本文档面向研发智能体与贡献者，定义 `shinobu-core` 微内核的**排他性架构不变量**、**子包与大文件定点切入路标**以及**单测精准映射**，旨在消除全仓盲搜与超长文件整篇通读。

---

## 1. 排他性架构不变量（Orthogonal Invariants）

修改任何模块前必须遵守以下五条物理边界，严禁跨层越权：

1. **引擎参数与会话隔离 100% 收口**：
   - `agy_cli` 与 `agy_sdk` 的命令参数拼装、会话 ID 映射及进程运行隔离 **100% 封装**于 `shinobu_core/engines/agy_engines.py`（其中 `AgySdkEngine` 位于 `L437-758`，`AgyCliEngine` 位于 `L818-1704`）。
   - 上层调度与服务模块（`orchestrator`、`daemon`、`edge_node`）仅允许调用统一的 `EngineAdapter` 协议接口，**严禁在引擎层之外直接拼装 CLI 参数或操纵底层引擎会话**。
2. **L0 前缀独占冻结与记忆尾部追加**：
   - `shinobu_core/soul/`（`loader.py` + `prefix_freezer.py`）独占 L0 系统前缀的加载与不可变哈希冻结，确保多轮对话 KV Cache 稳定命中。
   - `shinobu_core/memory/tools.py` 仅允许在消息序列尾部追加（append-only）动态记忆检索与工具结果，**严禁回写或污染已冻结的 L0 前缀**。
3. **云边终端审批与沙箱职责正交**：
   - `shinobu_core/cloud/approval.py` 与 `shinobu_core/cloud/sandbox.py` 仅负责 Edge / Cloud 远端节点的终端命令审批与受限沙箱执行，**绝不介入底层推理引擎内部的工具调用流**。
4. **单向插件挂载与零依赖裸核**：
   - 外部扩展统一通过标准 `entry_points("shinobu.plugins")` 单向注入微内核，公共底座绝不反向引用任何外部插件实现。
   - 设置环境变量 `SHINOBU_DISABLE_PLUGINS=1` 时，系统退化为**纯 Python 标准库零依赖裸核**并保证全部核心链路独立可用。
5. **白帽洁净室门禁红线**：
   - 仓库内全部源码、测试与 `.md` 文档严禁包含任何非公开扩展包名、内部项目代号、本机用户目录绝对路径或敏感凭据；提交前必须通过 `test_cli_and_gate.py` 白帽门禁校验。

---

## 2. 子包拓扑与职责导览（`shinobu_core/`）

| 子包路径 | 核心职责边界 | 关联禁区与协作契约 |
|---|---|---|
| `shinobu_core/core/` | 微内核生命周期、插件宿主注册表（`entry_points("shinobu.plugins")`）、事件总线与钩子分发 | 保持零业务耦合，不感知具体引擎或外设通道 |
| `shinobu_core/protocol/` | 跨模块标准数据契约、请求/响应消息模型、流式事件帧与错误码定义 | 纯数据结构与序列化逻辑，不含 I/O 副作用 |
| `shinobu_core/engines/` | `EngineAdapter` 抽象协议与后端推理引擎实现（含 `agy_engines.py`） | 唯一允许触碰底层 SDK / CLI 参数与会话状态的模块 |
| `shinobu_core/runtime/` | `orchestrator` 会话编排器、多轮上下文组装、工具调度闭环与并发控制 | 仅经 `EngineAdapter` 驱动推理，不直接操作子进程 |
| `shinobu_core/soul/` | 人格配置加载（`loader.py`）与 L0 系统提示词不可变冻结（`prefix_freezer.py`） | 独占 L0 前缀区，禁止混入动态时间戳或易变状态 |
| `shinobu_core/memory/` | 结构化记忆存储、检索接口与尾部注入工具（`tools.py`） | 严格尾部追加，严禁修改 `soul/` 产出的冻结前缀 |
| `shinobu_core/mcp/` | Model Context Protocol 桥接器、外部工具发现与标准协议转发 | 仅负责协议适配与工具路由，不越权管理宿主沙箱 |
| `shinobu_core/server/` | 本地 CLI 入口（`cli.py`）与常驻 HTTP/SSE 守护服务（`daemon.py`） | 薄接口转发层，核心状态统一委托 `runtime/` 与 `cloud/` |
| `shinobu_core/cloud/` | 云边协同核心、`edge_node` 边缘节点、Telegram/TTS 路由及终端 `approval.py` / `sandbox.py` | 终端沙箱仅管控边缘/云端命令执行，不侵入引擎内生工具 |
| `shinobu_core/ui/` | 桌面交互外壳、形象资源安装器与内嵌 Web 控制台（`web_console.py`） | 纯展示与交互控制层，通过标准 HTTP/CLI 接口通信 |

---

## 3. 超大文件免通读切入路标（Large-File Landmarks）

以下文件超过千行，**严禁整文件通读**；定位代码时必须先按锚点检索再按行号切片读取：

- **`shinobu_core/engines/agy_engines.py`**：
  - `AgySdkEngine` 实现区间：`L437-758`（SDK 会话生命周期、流式响应解析与异常映射）。
  - `AgyCliEngine` 实现区间：`L818-1704`（CLI 参数构造、工作区/运行环境隔离与标准流交互）。
- **`shinobu_core/server/cli.py`（3114L）**：
  - 采用无状态 `_cmd_*` 函数表分发架构，各子命令彼此独立。
  - **切入方式**：直接检索 `def _cmd_<subcmd>` 定位具体子命令实现，或检索 `build_parser` 定位 CLI 参数定义。
- **`shinobu_core/server/daemon.py`（2419L）**：
  - 本地与云边守护进程的 HTTP / SSE 路由分发中心。
  - **切入方式**：直接检索目标 HTTP 路由路径字符串（如 `"/v1/..."` 或 `"/api/..."`）定位对应处理分支。
- **`shinobu_core/ui/web_console.py`（3239L）**：
  - 内嵌完整单页应用（SPA）HTML / CSS / JS 模板字符串与服务端轻量宿主封装。
  - **切入方式**：先检索页面区块 ID、前端函数名或路由入口，再做小范围行段切片。

---

## 4. 局部修改与单测精准映射（Targeted Verification）

改完对应模块后优先运行匹配的最小单测集，避免盲跑全量慢速套件：

| 变更模块 / 子域 | 对应单测文件（`tests/`） | 关键测试类 / 验证焦点 |
|---|---|---|
| `core/` 微内核与插件挂载 | `test_kernel.py` | 插件注册、`SHINOBU_DISABLE_PLUGINS=1` 裸核隔离与钩子顺序 |
| `engines/` 引擎适配层 | `test_engines.py` | `TestAgySdkEngine`、`TestAgyCliEngine` 参数与会话映射 |
| `runtime/` 编排调度层 | `test_orchestrator.py` | 多轮编排闭环、上下文拼接与异常恢复 |
| `memory/` 记忆存储与工具 | `test_memory.py` | `memory/tools.py` 尾部追加契约与持久化检索 |
| `soul/` 人格与前缀冻结 | `test_soul.py` | `loader.py` 解析与 `prefix_freezer.py` L0 前缀不可变性 |
| `mcp/` 协议桥接 | `test_mcp_bridge.py` | MCP 工具发现、请求转发与错误隔离 |
| `server/daemon.py` 基础守护 | `test_daemon.py` | 本地 HTTP/SSE 接口、健康检查与会话路由 |
| `server/daemon.py` 云端与 MCP | `test_daemon_cloud_mcp.py` | Daemon 云边桥接与远端 MCP 挂载协同 |
| `server/cli.py` 与洁净室门禁 | `test_cli_and_gate.py` | `_cmd_*` 参数分发及全仓 `.md`/源码白帽洁净室扫描（`-k "gate"`） |
| `cloud/` 核心协议与状态 | `test_cloud_core.py` | 云边基础会话、鉴权与同步模型 |
| `cloud/` 边缘节点执行 | `test_cloud_edge_node.py` | `edge_node` 节点连接、心跳与任务派发 |
| `cloud/` 审批与终端沙箱 | `test_cloud_engine_sandbox.py` | `approval.py` 审批流与 `sandbox.py` 终端隔离边界 |
| `cloud/` Telegram 通道 | `test_cloud_telegram.py` | Telegram 消息收发适配与指令路由 |
| `cloud/` TTS 路由与网关 | `test_cloud_tts_router.py`、`test_tts_gateway.py` | 云端语音合成策略路由与本地/远端 TTS 网关容错 |
| `ui/` 外壳、形象与控制台 | `test_ui_shell.py`、`test_avatar_installer.py`、`test_web_console_compat.py` | UI 外壳交互、形象包安装校验与 `web_console.py` 模板兼容性 |

---

## 5. 标准施工自检命令

```bash
# 1. 按模块运行定向单测（示例：验证引擎适配层）
python -m unittest discover -s tests -p "test_engines.py" -v

# 2. 修改文档、CLI 或发版前必跑白帽洁净室门禁
python -m unittest discover -s tests -p "test_cli_and_gate.py" -k "gate" -v

# 3. 验证零插件纯裸核模式兼容性
SHINOBU_DISABLE_PLUGINS=1 python -m unittest discover -s tests -p "test_kernel.py" -v
```

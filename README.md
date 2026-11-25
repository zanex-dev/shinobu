<p align="center">
  <img src="assets/banner.svg" alt="SHINOBU (忍野忍) — Zero-Dependency Companion Microkernel" width="100%" />
</p>

<h1 align="center">Shinobu (<code>shinobu-core</code>)</h1>

<p align="center">
  <strong>Zero-Dependency Microkernel AI Companion Runtime — One Unified Codebase for Local Desktop/Web Play and 7×24 Privacy-Isolated Cloud Always-On Hosting.</strong>
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-v1.0.0-be123c" alt="Version v1.0.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-e11d48.svg" alt="License: PolyForm Noncommercial 1.0.0" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B%20(0%20deps)-3776AB.svg" alt="Python 3.10+ (0 deps)" /></a>
  <img src="https://img.shields.io/badge/network-127.0.0.1%20%2F%20Tailscale-059669" alt="Loopback and Tailscale" />
  <img src="https://img.shields.io/badge/telegram-4--Lock%20Isolated-0284c7" alt="Telegram 4-Lock Isolated" />
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> · <a href="README_ZH.md"><strong>简体中文</strong></a> · <a href="deploy/gcp_setup.md">GCP 7×24 Cloud Guide (ZH)</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

---

## 🔒 Zero-Trust Security Self-Verification (Audit in 3 Seconds)

`shinobu-core` is built from day one with strict physical and protocol boundaries so you can inspect the entire runtime in seconds:

1. **Zero Third-Party Core Dependencies (`dependencies = []`)**: The core runtime, daemon, Web Memory Console, memory/reminder SQLite store, hybrid router, edge node, and Telegram bridge run 100% on the Python `>= 3.10` standard library — zero external `pip` supply-chain risk. Only the optional desktop floating pet requires `pip install -e ".[desktop]"` (`PySide6>=6.6`).
2. **Loopback-First Daemon & Hardened Edge Node**:
   - The HTTP/MCP daemon binds to `127.0.0.1` by default (never exposing open ports to the public internet).
   - `EdgeNodeServer` (`shinobu node`) **hard-rejects `0.0.0.0` and any public IP**, restricts execution to `--allowed-root`, and strictly forbids `shell=True`.
3. **KV-Cache-Friendly Frozen L0 Prefix**: Built-in memory & reminder tools (`build_memory_tools`) append tool schemas at the tail without mutating the frozen L0 system prompt prefix, preserving prompt KV-cache hit rates.
4. **Telegram 4-Lock Physical Isolation**:
   - **No groups**: rejects all group/supergroup/channel messages;
   - **Single-user allowlist**: only the configured owner chat ID can interact;
   - **No forwards**: ignores forwarded messages to prevent indirect prompt injection;
   - **Zero-media / never `getFile`**: drops media attachments and never downloads user photos/files from Telegram servers.
5. **Plain-Text Auditable Source Only**: Distributed strictly as readable Python source code (no closed-source binary installers).
6. **3-Second Source Audit Command**:

```powershell
# PowerShell — inspect all network, subprocess, bind-address, and Telegram media boundaries:
Get-ChildItem .\shinobu_core -Recurse -Filter *.py | Select-String -Pattern "HTTPConnection|urlopen|subprocess|getFile|0\.0\.0\.0|shell=True"
```

```bash
# Linux / macOS
grep -rnE "HTTPConnection|urlopen|subprocess|getFile|0\.0\.0\.0|shell=True" ./shinobu_core
```

---

## ✨ Design Philosophy & Key Strengths: Why It's Not Just a "Desktop Pet"

Most desktop pets and chat wrappers stop at being **chat-only static character cards** — unable to do real engineering work and immediately losing context the moment you open a new window or switch apps. `shinobu-core` is built around the exact opposite philosophy: embedding **Native Work Capability** and **Cross-Session Long-Term Working Memory** into a zero-dependency microkernel, where the floating pet, Web Memory Console, and Telegram are simply different windows into one living working companion:

- **Native Work Capability (Far Beyond Small Talk)**: Powered by native Antigravity drivers, a bidirectional MCP tool hub, and a hardened local edge node (`--allowed-root` + L0/L1/L2 tiered approvals), Shinobu doesn't just sit on your screen — it actively invokes tools, assists across your workspace, and lets you remotely approve local tasks from your phone.
- **Cross-Session Long-Term Working Memory (Zero Context Reset Across Windows)**: Project context, preferences, distilled facts, and schedules persist in a local SQLite vault via sleep memory consolidation — shared seamlessly across Desktop Pet, Web Console, Telegram, and external `/v1` clients, and exposed back into any fresh AI IDE or agent session via `shinobu mcp-serve` / `POST /mcp`.

Core engineering highlights:

1. **Native Non-Proxy Google Antigravity Drivers (`agy_cli` & `agy_sdk`) + Multi-Engine Flexibility**: Strictly zero reverse-proxying, zero traffic spoofing, and zero credential scraping — connects directly through the official `agy` CLI (persistent `stream-json` worker, default engine for `shinobu node`) or the official `google-antigravity` Python SDK (lazy-loaded optional extra `.[agy-sdk]` supporting `gemini` / `openai_local` / `litert` modes). All runtime state and session maps stay isolated under `$SHINOBU_HOME/runtime/` without polluting your main Antigravity workspace, while remaining equally compatible with standard `openai_compat` endpoints (cloud APIs / Ollama / vLLM) and `--engine hybrid`.
2. **One Living Soul Across Every Surface (Not a Static Character Card)**: A single persistent brain and local SQLite memory vault simultaneously powers the built-in Web Memory Console (`shinobu ui`), transparent desktop pet (`shinobu pet`), mobile Telegram (`shinobu telegram`), and any external OpenAI `/v1` frontend — switching surfaces never resets memory, schedule, or voice state.
3. **KV-Cache-Friendly Frozen L0 Prefix + Sleep Memory Consolidation**: The core soul prefix is SHA-256 frozen and built-in tools are tail-appended to maximize long-session Prompt KV-cache hit rates (lower latency and token spend); `shinobu sleep` and memory consolidation distill verbose episodes into compact structured facts & episodes in SQLite instead of bloating context windows.
4. **One Zero-Dependency Codebase: 3-Minute Local Play ↔ 7×24 Hybrid Cloud**: Pure Python standard library (`dependencies = []`) runs standalone on a laptop in minutes, and scales seamlessly into `--engine hybrid` cloud hosting — routing tasks to your local PC's native `agy_cli` edge node while awake and falling back to the cloud engine in seconds when your PC sleeps.
5. **Native Bidirectional MCP Hub (Client + Server)**: Discover and non-destructively install curated zero-key community MCP presets via `shinobu mcp` (including safe `agy` bridging), or expose Shinobu's own memory and reminder tools back to external AI IDEs and agents via `shinobu mcp-serve` (stdio) or remote `POST /mcp`.
6. **Proactive Heartbeat, Natural-Language Reminders & Tri-Surface On-Demand TTS**: Schedule reminders conversationally (`schedule_reminder` / `list_reminders`) or via Telegram `/remind`, and trigger on-demand emotional voice playback (`POST /api/tts`) across Web, Desktop Pet, and Telegram with graceful degradation when no TTS backend is configured.
7. **Fault-Isolated Microkernel & Built-In Self-Diagnostics**: External private packs attach via Python `entry_points` with strict per-plugin fault isolation (a broken extension never crashes the kernel), complemented by `shinobu doctor` for one-command native `agy_sdk`/`agy_cli` driver, isolation directory, and redacted-secret health checks.

---

## 🧭 Architecture: One Core, Local & Cloud Unified

The exact same `shinobu-core` package powers both a 3-minute pure-local companion and a 7×24 cloud-hosted brain with local PC edge tentacles:

```text
                       ┌─────────────────────────────────────────────┐
                       │         shinobu-core (0 third-party deps)   │
                       │  Microkernel · Memory/Timemap SQLite · TTS  │
                       │  Orchestrator (Frozen L0 KV Prefix) · MCP   │
                       └──────────────────────┬──────────────────────┘
                                              │
            ┌─────────────────────────────────┴─────────────────────────────────┐
            ▼                                                                   ▼
┌───────────────────────────────┐                               ┌───────────────────────────────┐
│   Level 1: Pure Local Mode    │                               │   Level 2: 7×24 Cloud Brain   │
│  (No cloud / no VPS needed)   │                               │  (Tailscale Serve, 0 ports)   │
├───────────────────────────────┤                               ├───────────────────────────────┤
│ • shinobu serve (127.0.0.1)   │                               │ • shinobu serve --engine      │
│ • shinobu ui (Web Memory +    │   Optional Edge Tentacle      │   hybrid                      │
│   click-to-speak TTS)         │  ◄──── shinobu node ────────► │ • shinobu telegram (4-Lock,   │
│ • shinobu pet (Desktop Pet +  │  (Private Tailscale Mesh;     │   /remind, /voice, L1 Approve)│
│   drawer TTS, PySide6 opt.)   │   seamless fallback on sleep) │ • POST /mcp & GCS daily backup│
└───────────────────────────────┘                               └───────────────────────────────┘
```

---

## ⚡ Quick Start — Level 1: Pure Local Experience (3 Minutes)

No cloud server, no public domain, and no Telegram bot required.

### 1. Install `shinobu-core` (Zero Dependencies)

```bash
git clone https://github.com/zanex-dev/shinobu.git
cd shinobu
pip install -e .
```

### 2. Start the Local Loopback Daemon & Web Memory Console

```bash
# Optional: diagnose native Antigravity drivers (agy_cli / agy_sdk) and isolation dirs
shinobu doctor

# Terminal 1 — start the local daemon on 127.0.0.1:18790
# Runs offline with default mock; switch to native non-proxy Antigravity or standard OpenAI APIs anytime:
shinobu serve --engine agy_cli        # Official Antigravity CLI (non-proxy, isolated workspace)
# or: pip install -e ".[agy-sdk]" && shinobu serve --engine agy_sdk
# or: shinobu serve --engine openai_compat

# Terminal 2 — open the built-in Web Memory Console
shinobu ui
```

In the Web Memory Console, you can chat, inspect memories & reminders, and click **`🔊 朗读` (Speak)** on any assistant bubble to play on-demand speech via `POST /api/tts` (gracefully shows a toast if no TTS backend is configured).

### 3. Optional: Launch the Desktop Floating Pet

Install the optional PySide6 UI extra only if you want the desktop pet window:

```bash
pip install -e ".[desktop]"
shinobu pet
```

The pet chat drawer includes a **`🔊 朗读` (Speak)** button that synthesizes and plays the latest assistant reply (using native `winsound` on Windows with zero extra audio libraries, and graceful fallback hints on other platforms).

---

## ☁️ Level 2: 7×24 Cloud Always-On Brain + Local Edge Node

When you are ready for an always-on companion reachable from your phone and capable of securely reaching your local PC while it is awake:

1. **Zero Public Inbound Ports**: Deploy `shinobu serve --engine hybrid` on a small VM (e.g., GCP `e2-small` on **Standard Tier** for 200 GiB/month free egress) and expose HTTPS strictly inside your private tailnet via **Tailscale Serve**.
2. **4-Lock Telegram Mobile Bridge**: Run `shinobu telegram` to chat, toggle voice notes (`/voice on` / `/voice off`), schedule reminders (`/remind 20m stretch`), inspect nodes (`/nodes`), and tap inline `[✅ Approve]` / `[❌ Reject]` buttons for L1 command approvals.
3. **Local PC Edge Tentacle**: Run `shinobu node` on your workstation. While your PC is online, the cloud `hybrid` router seamlessly delegates supported tasks to your local engine; when your PC sleeps or exits, the cloud brain automatically falls back to its cloud engine without interruption.
4. **Automated SQLite Hot Backups**: Daily online `.backup` snapshots synced to object storage (GCS).

👉 See the step-by-step deployment & pitfall guide: **[`deploy/gcp_setup.md`](deploy/gcp_setup.md)**.

---

## 📖 CLI, Telegram & API Quick Reference

### CLI Commands (`shinobu`)

| Command | Mode | Description |
|---|---|---|
| `shinobu init` | Setup | Initialize soul templates and the local SQLite memory store. |
| `shinobu chat` | Local CLI | Chat directly with Shinobu in single-turn or interactive terminal mode. |
| `shinobu sleep` | Consolidation | Distill, consolidate, and cleanly close the active conversation episode. |
| `shinobu serve` | Local / Cloud | Start the `127.0.0.1` loopback HTTP/SSE/MCP daemon (supports `--engine hybrid`, etc.). |
| `shinobu ui` | Local | Open the zero-dependency Web Memory Console hosted by the running daemon. |
| `shinobu pet` | Local (Optional) | Launch the transparent floating desktop pet & chat drawer (requires `.[desktop]`). |
| `shinobu compat` | External UIs | Print local OpenAI `/v1` compatible endpoint and event stream details (model ID: `shinobu`). |
| `shinobu mcp` | MCP Client | Inspect, probe, and non-destructively install curated zero-key community MCP presets or bridge MCP servers. |
| `shinobu mcp-serve` | MCP Server | Expose Shinobu as an outbound stdio MCP server for external AI clients and IDEs. |
| `shinobu node` | Edge Tentacle | Register this PC over Tailscale/private network (`--cloud-url`, `--bind-host`, `--allowed-root`). |
| `shinobu telegram` | Cloud / Local | Start the 4-lock isolated Telegram long-polling bridge. |
| `shinobu status` / `doctor` | Diagnostics | Inspect home/plugins/daemon state or run driver, isolation, and redacted credential health checks. |

### Telegram Built-in Commands

| Command | Description |
|---|---|
| `/status` | Inspect runtime health, active engine, and pending approval count. |
| `/nodes` | List currently online local PC / Colab GPU edge nodes. |
| `/voice on` / `/voice off` | Toggle on-demand voice note replies. |
| `/remind` / `/reminders` | List pending reminders from the SQLite `timemap` schedule table. |
| `/remind <10m\|2h\|HH:MM\|ISO> <text>` | Schedule a new reminder (relative duration, local `HH:MM`, or ISO-8601). |

### Core HTTP / MCP & Agent Tool Contracts

| Interface | Purpose |
|---|---|
| `GET /v1/models` & `POST /v1/chat/completions` | OpenAI `/v1` compatible chat endpoint with strict loopback-origin CORS policy (default model `shinobu`). |
| `POST /api/tts` | Synthesize speech (`{"text", "mood", "fmt": "ogg_opus"\|"mp3"\|"wav"}`); returns raw audio bytes or `503 {"error": "tts_unavailable"}`. |
| `GET` / `POST /api/timemap` | Query and create scheduled reminders stored in SQLite `timemap`. |
| `POST /mcp` | Remote Model Context Protocol (MCP) JSON-RPC endpoint (protected by bearer token over Tailscale). |
| `build_memory_tools` (5 tools) | Memory read/write plus tail-appended `schedule_reminder` and `list_reminders` (zero L0 prefix mutation). |

### 🔌 Connecting External Frontends (SillyTavern / Open WebUI / VTuber Shells)

`shinobu-core` runs completely standalone with zero external frontend dependencies. If you still want to reuse the exact same soul and memory vault inside **SillyTavern**, Cherry Studio, Open WebUI, or local VTuber/pet shells (e.g. Project AIRI, Open-LLM-VTuber, VPetLLM), run `shinobu compat` and plug the loopback `/v1` base URL and bearer token into your client.

> **Integration Tip**: Treat external clients as a **thin UI shell** — disable client-side **vector memory / auto-summary / dynamic lorebook injectors, status-bar macros, and client TTS plugins** so Shinobu remains the single source of truth for memory consolidation, TTS synthesis, and the frozen L0 KV-cache prefix (preventing split-brain memory conflicts or prompt cache busting).

---

## 🧩 Plugin Extension Boundary

`shinobu-public` (`shinobu-core`) is strictly a clean, generic, white-hat microkernel foundation. Personal personas, private sensors, custom hooks, or private notebooks belong in separate external packages loaded dynamically via Python entry points:

```toml
[project.entry-points."shinobu.plugins"]
my_extension = "my_pkg.plugin:create_plugin"
```

---

## ⚠️ Disclaimer & License

- This project is an open-source technical framework for personal AI runtime orchestration and local/cloud automation. Users are solely responsible for configuring their own API credentials, cloud budgets, firewall rules, and compliance with third-party platform terms of service.
- Never commit real API keys, bearer tokens, private Tailscale hostnames, or personal data into public forks.
- Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE) (`PolyForm-Noncommercial-1.0.0`) — **free for personal, educational, and noncommercial use; commercial use, paid SaaS, bundling, or resale of the code or `assets/` artwork is strictly prohibited** without prior written authorization from zanex-dev.
- `Required Notice: Copyright (c) 2026 zanex (zanex-dev) (https://github.com/zanex-dev/shinobu)`

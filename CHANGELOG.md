# Changelog

All notable changes to `shinobu-core` (`shinobu-public`) are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.0] - 2026-11-25

Preserves the strict zero-dependency core (`dependencies = []`) while adding an opt-in local TTS gateway, Colab GPU voice notebooks, Project AIRI integration kit, custom avatar pack installer, and cross-platform zero-dependency CI.

### Added
- **Optional Local OpenAI-Compatible TTS Gateway (`.[tts-edge]` / `shinobu tts-gateway`)**:
  - Added `shinobu_core/cloud/tts_gateway.py` and optional extra `tts-edge = ["edge-tts>=6.1"]` (lazily imported only during local fallback synthesis so core imports and remote GPU forwarding work with zero extras installed).
  - Added `shinobu tts-gateway` CLI command (`--host`/`--port`/`--voice`/`--upstream-config`/`--set-upstream`/`--upstream-key`/`--clear-upstream`/`--show-upstream`/`--json`) exposing `/v1/audio/speech`, `/v1/models`, and `/health`, with GPU-upstream-first routing, automatic Edge-TTS fallback, and `validate_bind_host` loopback enforcement.
- **Colab GPU Voice Notebooks & Project AIRI Integration Kit**:
  - Added sanitized notebooks `notebooks/shinobu_colab_tts_worker_v2.ipynb` (cloud worker mode), `notebooks/shinobu_colab_tts_local_l4.ipynb` (local L4/T4 direct mode), and `notebooks/README.md` with cleared cell outputs and zero personal paths.
  - Added `deploy/airi_tts_guide.md` and environment-parameterized launcher `deploy/launch_local_airi_tts.ps1` to connect `shinobu tts-gateway` with Project AIRI and other OpenAI-TTS-compatible frontends.
- **Generic Avatar Pack Installer (`shinobu avatar`) & Cross-Platform Zero-Dep CI**:
  - Added `shinobu_core/ui/avatar_installer.py` and `shinobu avatar [--source DIR] [--name custom] [--force] [--json]` to validate and install user-supplied multi-expression PNG/WebP avatar packs without bundling binary artwork in the repo.
  - Added `.github/workflows/ci.yml` running the full unit test and gate suite across Linux / Windows / macOS × Python 3.10 / 3.11 / 3.12 with zero third-party dependencies (`SHINOBU_DISABLE_PLUGINS=1`).
- **Bilingual Optional Ecosystem Menu**: Updated `README.md` and `README_ZH.md` with the 4-dimension "Default (Zero-Dep) vs. Recommended Opt-In" matrix, new CLI quick-reference entries, and Colab / AIRI documentation links.

---

## [1.0.0] - 2026-11-25

First stable open-source release of `shinobu-core`: one zero-dependency Python microkernel powering both pure-local play and 7×24 privacy-isolated cloud always-on hosting.

### Added
- **Unified Zero-Dependency Microkernel (`dependencies = []`)**: Core daemon (`shinobu serve`), plugin entry-point loader (`shinobu.plugins`), SQLite memory/timemap store, hybrid engine router, edge node (`shinobu node`), remote `POST /mcp`, and optional desktop pet (`.[desktop]` -> `PySide6>=6.6`).
- **Reminder Tools & Telegram `/remind` Command (T01)**:
  - Tail-appended `schedule_reminder` and `list_reminders` tools in `build_memory_tools` (5 tools total, preserving the frozen L0 KV-cache prefix).
  - Added `/remind` (and `/reminders`) command and `on_remind` callback in `TelegramBotBridge`, supporting `<N>m`, `<N>h`, local `HH:MM` (auto-rolling to next day if passed), and ISO-8601 timestamps bridged to `GET/POST /api/timemap`.
- **Desktop Web Console & Pet TTS Playback (T02)**:
  - Added `DaemonClient.synthesize_tts(text, *, mood="calm", fmt="wav", timeout=None) -> tuple[bytes, str]` calling `POST /api/tts`.
  - Added per-message `🔊 朗读` speak/stop buttons in the Web Memory Console (`mp3` playback via native browser `Audio`, single-instance management, graceful 503 toast).
  - Added `🔊 朗读` button to the desktop pet chat drawer with pure-Python `extract_last_assistant_text` and zero-extra-dep Windows `winsound.SND_MEMORY` WAV playback (with graceful fallback hints on non-Windows or unconfigured TTS).
- **Cloud Always-On Deployment Kit**: Added `deploy/gcp_setup.md`, systemd service templates, and GCS SQLite hot-backup scripts for zero-public-port Tailscale Serve hosting.
- **Bilingual Open-Source Release Suite (T03)**: Added `README.md`, `README_ZH.md`, `CHANGELOG.md`, `CHANGELOG_ZH.md`, MIT `LICENSE`, and hardened `.gitignore`.

### Security & Privacy
- Enforced `127.0.0.1` loopback-first daemon binding and strict `EdgeNodeServer` guardrails (hard-rejecting `0.0.0.0`/public IPs, forbidding `shell=True`, enforcing `--allowed-root`).
- Enforced Telegram 4-lock physical isolation (no groups, single owner allowlist, no forwards, zero media / never calling `getFile`).
- Completed full public-repository sanitization audit with zero personal paths, real hostnames, tokens, or private persona leaks.

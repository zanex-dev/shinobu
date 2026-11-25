# Changelog

All notable changes to `shinobu-core` (`shinobu-public`) are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

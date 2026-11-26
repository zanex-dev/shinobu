# -*- coding: utf-8 -*-
"""
Zero-dependency loopback single-page Web Console for the companion daemon.

Security & white-hat guarantees:
  - `render_console_html()` returns a pure static HTML/CSS/ES6 shell containing
    ZERO private data, ZERO embedded tokens, and ZERO external CDN references.
  - On load, client-side JS extracts `token=` from `window.location.hash`,
    stores it in `sessionStorage["shinobu_token"]`, and immediately clears the
    URL fragment via `history.replaceState(null, "", location.pathname)`.
  - All API calls authenticate with `Authorization: Bearer <token>`; realtime
    events subscribe to `GET /events?token=...`.
"""

from __future__ import annotations

_CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Companion Studio · 本地伴侣控制台</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1020;
      --bg-elev: #12192d;
      --bg-panel: #162038;
      --bg-soft: #1d2945;
      --border: rgba(148, 163, 184, 0.18);
      --border-strong: rgba(148, 163, 184, 0.34);
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --accent: #6366f1;
      --accent-hover: #818cf8;
      --accent-soft: rgba(99, 102, 241, 0.16);
      --cyan: #38bdf8;
      --ok: #34d399;
      --warn: #fbbf24;
      --danger: #f87171;
      --radius: 14px;
      --radius-sm: 9px;
      --shadow: 0 18px 42px rgba(2, 6, 23, 0.45);
      --font-sans: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      --font-mono: ui-monospace, "Cascadia Code", "JetBrains Mono", Consolas, monospace;
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      background:
        radial-gradient(circle at 12% 8%, rgba(99, 102, 241, 0.16), transparent 36%),
        radial-gradient(circle at 88% 12%, rgba(56, 189, 248, 0.12), transparent 32%),
        var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 14px;
      line-height: 1.55;
    }

    button, input, textarea, select {
      font: inherit;
      color: inherit;
    }

    .app-shell {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 100vh;
      max-width: 1420px;
      margin: 0 auto;
      padding: 16px;
      gap: 14px;
    }

    /* Header */
    .topbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 18px;
      background: rgba(18, 25, 45, 0.88);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand-mark {
      width: 36px;
      height: 36px;
      border-radius: 11px;
      background: linear-gradient(135deg, var(--accent), var(--cyan));
      display: grid;
      place-items: center;
      font-weight: 700;
      color: #fff;
      letter-spacing: 0.04em;
      box-shadow: 0 6px 18px rgba(99, 102, 241, 0.35);
    }

    .brand-title {
      font-weight: 700;
      font-size: 16px;
      letter-spacing: 0.01em;
    }

    .brand-sub {
      font-size: 12px;
      color: var(--text-muted);
    }

    .status-strip {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      background: var(--bg-soft);
      border: 1px solid var(--border);
      color: var(--text);
      white-space: nowrap;
    }

    .badge-accent {
      background: var(--accent-soft);
      border-color: rgba(99, 102, 241, 0.42);
      color: #c7d2fe;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--text-muted);
    }
    .dot.ok { background: var(--ok); box-shadow: 0 0 8px rgba(52, 211, 153, 0.7); }
    .dot.warn { background: var(--warn); }
    .dot.err { background: var(--danger); }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 13px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: var(--bg-soft);
      color: var(--text);
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
    }
    .btn:hover:not(:disabled) {
      background: #253457;
      border-color: var(--border-strong);
    }
    .btn:active:not(:disabled) { transform: translateY(1px); }
    .btn:disabled { opacity: 0.52; cursor: not-allowed; }

    .btn-primary {
      background: var(--accent);
      border-color: rgba(129, 140, 248, 0.65);
      color: #fff;
      font-weight: 600;
    }
    .btn-primary:hover:not(:disabled) {
      background: var(--accent-hover);
      border-color: #a5b4fc;
    }

    .btn-danger {
      background: rgba(248, 113, 113, 0.14);
      border-color: rgba(248, 113, 113, 0.35);
      color: #fecaca;
    }
    .btn-danger:hover:not(:disabled) {
      background: rgba(248, 113, 113, 0.24);
      border-color: rgba(248, 113, 113, 0.6);
    }

    .btn-sm { padding: 5px 10px; font-size: 12px; }

    /* Main 2-column layout */
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 310px;
      gap: 14px;
      min-height: 0;
    }

    .main-card, .side-card {
      background: rgba(18, 25, 45, 0.86);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      min-height: 0;
    }

    /* Tabs */
    .tab-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      background: rgba(22, 32, 56, 0.65);
      border-radius: var(--radius) var(--radius) 0 0;
    }

    .tab-btn {
      padding: 8px 14px;
      border-radius: 10px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      font-weight: 500;
      transition: all 0.15s ease;
    }
    .tab-btn:hover { color: var(--text); background: rgba(148, 163, 184, 0.08); }
    .tab-btn.active {
      color: #fff;
      background: var(--accent-soft);
      border-color: rgba(99, 102, 241, 0.45);
    }

    .tab-panel {
      display: none;
      flex: 1;
      min-height: 540px;
      padding: 16px;
      overflow: auto;
    }
    .tab-panel.active {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    /* Chat View */
    .chat-log {
      flex: 1;
      min-height: 360px;
      max-height: 60vh;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 12px;
      background: rgba(11, 16, 32, 0.56);
      border: 1px solid var(--border);
      border-radius: 12px;
    }

    .msg {
      max-width: 82%;
      padding: 10px 14px;
      border-radius: 12px;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid var(--border);
    }
    .msg.user {
      align-self: flex-end;
      background: rgba(99, 102, 241, 0.22);
      border-color: rgba(129, 140, 248, 0.4);
    }
    .msg.assistant {
      align-self: flex-start;
      background: var(--bg-panel);
    }
    .msg.system {
      align-self: center;
      max-width: 92%;
      font-size: 12px;
      color: var(--text-muted);
      background: rgba(148, 163, 184, 0.08);
    }
    .msg-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 11px;
      color: var(--text-muted);
      margin-bottom: 4px;
    }
    .tts-btn {
      appearance: none;
      border: 1px solid rgba(148, 163, 184, 0.28);
      background: rgba(15, 23, 42, 0.45);
      color: var(--text-muted);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      line-height: 1.4;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
    }
    .tts-btn:hover:not(:disabled) {
      color: var(--text-main, #e2e8f0);
      border-color: rgba(148, 163, 184, 0.55);
      background: rgba(30, 41, 59, 0.72);
    }
    .tts-btn:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .tts-btn.playing {
      color: #fde68a;
      border-color: rgba(251, 191, 36, 0.55);
      background: rgba(251, 191, 36, 0.14);
    }

    .chat-composer {
      display: flex;
      gap: 10px;
      align-items: flex-end;
    }

    .input, .textarea {
      width: 100%;
      background: rgba(11, 16, 32, 0.72);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 9px 12px;
      color: var(--text);
      outline: none;
    }
    .input:focus, .textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.18);
    }
    .textarea { resize: vertical; min-height: 54px; }

    /* Memory / Soul Editor */
    .soul-toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .pill-group {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .pill {
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--bg-panel);
      color: var(--text-muted);
      cursor: pointer;
      font-size: 12px;
    }
    .pill.active {
      background: var(--accent);
      border-color: var(--accent-hover);
      color: #fff;
      font-weight: 600;
    }

    .notice-bar {
      padding: 9px 12px;
      border-radius: var(--radius-sm);
      background: rgba(56, 189, 248, 0.1);
      border: 1px solid rgba(56, 189, 248, 0.3);
      color: #bae6fd;
      font-size: 12px;
    }

    .code-editor {
      width: 100%;
      min-height: 290px;
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 1.6;
      padding: 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: rgba(11, 16, 32, 0.8);
      color: #e2e8f0;
      resize: vertical;
    }

    .subcard {
      background: rgba(11, 16, 32, 0.48);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
    }
    .subcard h3 {
      margin: 0 0 10px 0;
      font-size: 13px;
      color: #cbd5e1;
      letter-spacing: 0.02em;
    }

    /* Split Grids */
    .split-2 {
      display: grid;
      grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
      gap: 12px;
      flex: 1;
      min-height: 0;
    }

    .list-box {
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 460px;
      overflow-y: auto;
    }

    .list-item {
      padding: 9px 11px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: var(--bg-panel);
      cursor: pointer;
      text-align: left;
    }
    .list-item:hover { border-color: var(--border-strong); }
    .list-item.active {
      border-color: var(--accent);
      background: var(--accent-soft);
    }

    .journal-view {
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--font-mono);
      font-size: 13px;
      max-height: 360px;
      overflow-y: auto;
      padding: 12px;
      border-radius: 10px;
      background: rgba(11, 16, 32, 0.72);
      border: 1px solid var(--border);
    }

    /* Right Live Preview Sidebar */
    .side-card {
      padding: 14px;
      gap: 12px;
    }

    .avatar-stage {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      padding: 14px;
      border-radius: 12px;
      background:
        radial-gradient(circle at 50% 30%, rgba(99, 102, 241, 0.2), rgba(11, 16, 32, 0.8));
      border: 1px solid var(--border);
    }

    .avatar-img {
      width: 176px;
      height: 176px;
      object-fit: contain;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border);
      padding: 6px;
    }

    .bubble-box {
      width: 100%;
      min-height: 68px;
      padding: 10px 12px;
      border-radius: 11px;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      font-size: 13px;
      word-break: break-word;
    }

    .event-feed {
      flex: 1;
      min-height: 180px;
      max-height: 300px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 12px;
    }

    .event-row {
      padding: 7px 9px;
      border-radius: 8px;
      background: rgba(11, 16, 32, 0.62);
      border: 1px solid var(--border);
    }

    /* Timemap & State controls */
    .form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      align-items: end;
    }

    .field label {
      display: block;
      font-size: 12px;
      color: var(--text-muted);
      margin-bottom: 4px;
    }

    .schedule-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 9px 11px;
      border-radius: var(--radius-sm);
      background: var(--bg-panel);
      border: 1px solid var(--border);
    }

    .muted { color: var(--text-muted); font-size: 12px; }
    .mono { font-family: var(--font-mono); }

    /* Unlock Modal */
    .overlay {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 16px;
      background: rgba(2, 6, 23, 0.8);
      backdrop-filter: blur(8px);
      z-index: 100;
    }
    .overlay.open { display: flex; }

    .unlock-card {
      width: 100%;
      max-width: 420px;
      padding: 22px;
      border-radius: 16px;
      background: var(--bg-elev);
      border: 1px solid var(--border-strong);
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 10px;
      background: var(--bg-elev);
      border: 1px solid var(--border-strong);
      box-shadow: var(--shadow);
      font-size: 13px;
      opacity: 0;
      pointer-events: none;
      transform: translateY(8px);
      transition: all 0.2s ease;
      z-index: 120;
    }
    .toast.show { opacity: 1; transform: translateY(0); }

    /* Episode Bar, Readonly Banner & Lightweight Markdown */
    .episode-bar {
      padding: 9px 11px;
      border-radius: 10px;
      background: rgba(11, 16, 32, 0.56);
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .episode-bar-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .episode-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-height: 92px;
      overflow-y: auto;
    }
    .episode-list.collapsed { display: none; }

    .readonly-banner {
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 10px;
      background: rgba(245, 158, 11, 0.14);
      border: 1px solid rgba(245, 158, 11, 0.4);
      font-size: 12px;
    }
    .readonly-banner.open { display: flex; }

    .md-code {
      font-family: var(--font-mono);
      font-size: 0.9em;
      padding: 1px 5px;
      border-radius: 5px;
      background: rgba(15, 23, 42, 0.72);
      border: 1px solid var(--border);
    }
    .md-pre {
      margin: 6px 0;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(11, 16, 32, 0.86);
      border: 1px solid var(--border);
      overflow-x: auto;
      font-family: var(--font-mono);
      font-size: 12px;
      white-space: pre;
    }
    .md-list {
      margin: 4px 0 4px 18px;
      padding: 0;
    }
    .md-list li { margin: 2px 0; }

    .fact-row, .approval-row, .worker-row {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(11, 16, 32, 0.62);
      border: 1px solid var(--border);
    }
    .subcard-divider {
      border-top: 1px solid var(--border);
      margin: 12px 0 10px;
      padding-top: 10px;
    }
    .pill:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    @media (max-width: 980px) {
      .workspace { grid-template-columns: 1fr; }
      .split-2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">CS</div>
        <div>
          <div class="brand-title">Companion Studio</div>
          <div class="brand-sub">本地回环伴侣控制台 · 零外部依赖离线版</div>
        </div>
      </div>

      <div class="status-strip">
        <span class="badge" id="badge-sse"><span class="dot" id="dot-sse"></span><span id="text-sse">未连接</span></span>
        <span class="badge badge-accent">状态：<strong id="badge-state">idle</strong></span>
        <span class="badge badge-accent">心情：<strong id="badge-mood">calm</strong></span>
        <span class="badge mono" id="badge-episode">ep: -</span>
        <button class="btn btn-sm" id="btn-top-new-episode" title="秒开新对话但不整理记忆 (/new)">🆕 秒开新对话</button>
        <button class="btn btn-sm" id="btn-top-sleep" title="整理记忆并归档当前会话 (/sleep)">🌙 整理记忆并新开</button>
        <button class="btn btn-sm btn-danger" id="btn-lock" title="清除当前会话令牌">锁定</button>
      </div>
    </header>

    <div class="workspace">
      <main class="main-card">
        <nav class="tab-bar" role="tablist">
          <button class="tab-btn active" data-tab="chat" role="tab">对聊与气泡</button>
          <button class="tab-btn" data-tab="memory" role="tab">记忆手账</button>
          <button class="tab-btn" data-tab="journal" role="tab">日记与检索</button>
          <button class="tab-btn" data-tab="timemap" role="tab">定时与状态</button>
          <button class="tab-btn" data-tab="community" role="tab">社区与插件</button>
        </nav>

        <!-- Tab 1: Chat + Live SSE -->
        <section class="tab-panel active" id="panel-chat">
          <div class="episode-bar" id="episode-bar">
            <div class="episode-bar-head">
              <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <strong style="font-size:12px;">会话历史</strong>
                <span class="badge badge-accent mono" id="episode-view-badge">当前活跃会话</span>
              </div>
              <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
                <button class="btn btn-sm" id="btn-episode-back-active" type="button" style="display:none;">回到当前对话</button>
                <button class="btn btn-sm" id="btn-episode-new" type="button">🆕 秒开新对话</button>
                <button class="btn btn-sm" id="btn-episode-reload" type="button">刷新</button>
                <button class="btn btn-sm" id="btn-episode-toggle" type="button">收起列表</button>
              </div>
            </div>
            <div class="episode-list" id="episode-list"></div>
          </div>
          <div class="readonly-banner" id="chat-readonly-banner">
            <span>当前正在只读回看历史归档会话，输入框已暂停。</span>
            <button class="btn btn-sm btn-primary" id="btn-readonly-return" type="button">返回当前活跃会话</button>
          </div>
          <div class="chat-log" id="chat-log"></div>
          <div class="chat-composer">
            <textarea
              class="textarea"
              id="chat-input"
              rows="2"
              placeholder="输入消息与伴侣对话（Enter 发送，Shift+Enter 换行）..."
            ></textarea>
            <button class="btn btn-primary" id="btn-chat-send">发送</button>
          </div>
        </section>

        <!-- Tab 2: Memory & Soul Editor -->
        <section class="tab-panel" id="panel-memory">
          <div class="soul-toolbar">
            <div class="pill-group" id="soul-tabs">
              <button class="pill active" data-soul="SOUL.md">SOUL.md</button>
              <button class="pill" data-soul="STYLE.md">STYLE.md</button>
              <button class="pill" data-soul="USER.md">USER.md</button>
              <button class="pill" data-soul="MEMORY.md">MEMORY.md</button>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <span class="muted" id="soul-meta">0 bytes</span>
              <button class="btn btn-sm" id="btn-soul-reload">重新读取</button>
              <button class="btn btn-sm btn-primary" id="btn-soul-save">保存当前文件</button>
            </div>
          </div>

          <div class="notice-bar" id="soul-notice">
            非破坏性留底提示：保存前守护进程会自动把旧版本留底至 <code>&lt;home&gt;/soul/_versions/&lt;stem&gt;_vN.md</code>，再原子写入新内容。
          </div>

          <textarea class="code-editor" id="soul-editor" spellcheck="false" placeholder="加载中..."></textarea>

          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; flex-wrap:wrap;">
              <div style="display:flex; align-items:center; gap:8px;">
                <h3 style="margin:0;">长期记忆体检与精炼</h3>
                <span class="muted">去重 · 过期清理 · 冲突合并（先预览后应用）</span>
              </div>
              <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <button class="btn btn-sm" id="btn-memory-compact-preview" type="button">记忆体检（预览）</button>
                <button class="btn btn-sm btn-primary" id="btn-memory-compact-apply" type="button" disabled>确认应用精炼</button>
              </div>
            </div>
            <div class="list-box" id="memory-compact-box" style="max-height:280px; padding:10px 12px;">
              <div class="muted">点击「记忆体检（预览）」检查 MEMORY.md 与 Facts 中的重复、过期或冲突条目；确认应用前会自动留底至 <code>&lt;home&gt;/_versions/MEMORY_vN.md</code>，永不改动原始 <code>journals/</code>。</div>
            </div>
          </div>

          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; flex-wrap:wrap;">
              <div style="display:flex; align-items:center; gap:8px;">
                <h3 style="margin:0;">结构化事实记忆（Facts）</h3>
                <span class="badge mono" id="facts-count">0 条</span>
              </div>
              <button class="btn btn-sm" id="btn-facts-reload" type="button">刷新 Facts</button>
            </div>
            <div class="form-grid" style="margin-bottom:8px;">
              <div class="field">
                <label for="fact-key-input">记忆键（key）</label>
                <input class="input mono" id="fact-key-input" placeholder="例如 user.favorite_drink" />
              </div>
              <div class="field">
                <label for="fact-value-input">记忆内容（value）</label>
                <input class="input" id="fact-value-input" placeholder="例如 冰美式少糖" />
              </div>
              <div class="field">
                <label for="fact-confidence-input">置信度（0.0 ~ 1.0）</label>
                <input class="input mono" id="fact-confidence-input" type="number" min="0" max="1" step="0.05" value="0.9" />
              </div>
              <div style="display:flex; align-items:flex-end;">
                <button class="btn btn-primary" id="btn-fact-add" type="button">新增/更新记忆</button>
              </div>
            </div>
            <div style="margin-bottom:8px;">
              <input class="input" id="facts-filter-input" placeholder="本地关键词过滤（匹配 key / value / 分类）..." />
            </div>
            <div class="list-box" id="facts-list" style="max-height:240px;"></div>
          </div>
        </section>

        <!-- Tab 3: Journals + FTS5 Search -->
        <section class="tab-panel" id="panel-journal">
          <div class="subcard">
            <h3>FTS5 记忆全文检索</h3>
            <div style="display:flex; gap:8px;">
              <input class="input" id="search-input" placeholder="输入关键词检索历史记忆与对话..." />
              <button class="btn btn-primary" id="btn-search">检索</button>
            </div>
            <div class="list-box" id="search-results" style="margin-top:10px; max-height:190px;"></div>
          </div>

          <div class="split-2">
            <div class="subcard" style="display:flex; flex-direction:column; gap:8px;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0;">日记手账列表</h3>
                <button class="btn btn-sm" id="btn-journals-reload">刷新</button>
              </div>
              <div class="list-box" id="journal-list"></div>
            </div>

            <div class="subcard" style="display:flex; flex-direction:column; gap:8px;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0;" id="journal-title">选择一篇日记查看</h3>
                <span class="muted mono" id="journal-mtime"></span>
              </div>
              <div class="journal-view" id="journal-content">暂无选中日记。</div>
            </div>
          </div>
        </section>

        <!-- Tab 4: Timemap + State/Mood + /sleep -->
        <section class="tab-panel" id="panel-timemap">
          <div class="subcard">
            <h3>状态切换（5 态）</h3>
            <div class="pill-group" id="state-controls"></div>
          </div>

          <div class="subcard">
            <h3>心情切换（5 心情）</h3>
            <div class="pill-group" id="mood-controls"></div>
          </div>

          <div class="subcard">
            <h3>睡眠归档（POST /sleep）</h3>
            <div class="form-grid">
              <div class="field">
                <label for="sleep-date">归档日期（可选，YYYY-MM-DD，留空为当天）</label>
                <input class="input mono" id="sleep-date" placeholder="例如 2026-11-21" />
              </div>
              <div>
                <button class="btn btn-primary" id="btn-sleep-run">执行睡眠归档 /sleep</button>
              </div>
            </div>
            <div class="muted" id="sleep-output" style="margin-top:8px;"></div>
          </div>

          <div class="subcard">
            <h3>新增定时提醒（Timemap）</h3>
            <div class="form-grid">
              <div class="field">
                <label for="tm-trigger">触发时间（ISO-8601 UTC）</label>
                <div style="display:flex; gap:6px;">
                  <input class="input mono" id="tm-trigger" placeholder="2026-11-21T22:00:00Z" />
                  <button class="btn btn-sm" id="btn-tm-plus5" type="button">+5m</button>
                </div>
              </div>
              <div class="field">
                <label for="tm-label">提醒标签（label）</label>
                <input class="input" id="tm-label" placeholder="例如 stretch_break" />
              </div>
              <div class="field">
                <label for="tm-payload">附加 JSON（可选）</label>
                <input class="input mono" id="tm-payload" placeholder='{"note": "休息一下"}' />
              </div>
              <div>
                <button class="btn btn-primary" id="btn-tm-add">添加日程</button>
              </div>
            </div>
          </div>

          <div class="subcard" style="flex:1;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <h3 style="margin:0;">当前日程队列（schedules）</h3>
              <div style="display:flex; gap:8px; align-items:center;">
                <label class="muted" style="display:flex; align-items:center; gap:5px; cursor:pointer;">
                  <input type="checkbox" id="tm-include-done" /> 显示已完成
                </label>
                <button class="btn btn-sm" id="btn-tm-reload">刷新</button>
              </div>
            </div>
            <div class="list-box" id="timemap-list"></div>
          </div>
        </section>

        <!-- Tab 5: Community Frontends + Model Hot-Switch + MCP + Bubble Test -->
        <section class="tab-panel" id="panel-community">
          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <h3 style="margin:0;">社区壳接入（Project AIRI / Open-LLM-VTuber）</h3>
              <button class="btn btn-sm" id="btn-compat-reload" type="button">刷新</button>
            </div>
            <div class="muted" id="compat-status" style="margin-bottom:8px;">正在加载 OpenAI 兼容接入配置...</div>
            <div class="form-grid">
              <div class="field">
                <label for="compat-base-url">OpenAI Base URL</label>
                <div style="display:flex; gap:6px;">
                  <input class="input mono" id="compat-base-url" readonly placeholder="http://127.0.0.1:8765/v1" />
                  <button class="btn btn-sm" id="btn-copy-base-url" type="button">复制</button>
                </div>
              </div>
              <div class="field">
                <label for="compat-model">Model</label>
                <div style="display:flex; gap:6px;">
                  <input class="input mono" id="compat-model" readonly placeholder="shinobu-companion" />
                  <button class="btn btn-sm" id="btn-copy-model" type="button">复制</button>
                </div>
              </div>
              <div class="field">
                <label for="compat-api-key">API Key（当前会话令牌）</label>
                <div style="display:flex; gap:6px;">
                  <input class="input mono" id="compat-api-key" type="password" readonly placeholder="••••••••" />
                  <button class="btn btn-sm" id="btn-toggle-api-key" type="button">显示</button>
                  <button class="btn btn-sm" id="btn-copy-api-key" type="button">复制</button>
                </div>
              </div>
            </div>
            <div class="muted mono" id="compat-extra-urls" style="margin-top:8px; font-size:12px;"></div>
            <div class="pill-group" id="compat-frontends" style="margin-top:10px;"></div>
          </div>

          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <h3 style="margin:0;">驱动引擎模型热切</h3>
              <div style="display:flex; gap:6px; align-items:center;">
                <span class="badge badge-accent mono" id="engine-badge">加载中...</span>
                <button class="btn btn-sm" id="btn-engine-reload" type="button">刷新</button>
              </div>
            </div>
            <div class="muted" id="engine-status" style="margin-bottom:8px;">选择预设或输入自定义模型名后点击应用。</div>
            <div class="pill-group" id="engine-presets" style="margin-bottom:10px;"></div>
            <div class="form-grid">
              <div class="field">
                <label for="engine-model-input">自定义模型名（留空恢复默认）</label>
                <input class="input mono" id="engine-model-input" placeholder="(default)" />
              </div>
              <div style="display:flex; align-items:flex-end;">
                <button class="btn btn-primary" id="btn-engine-apply" type="button">应用模型</button>
              </div>
            </div>
            <div class="subcard-divider">
              <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:6px; flex-wrap:wrap;">
                <strong style="font-size:12px;">混动路由模式（Hybrid Route）</strong>
                <span class="muted mono" id="engine-route-status">加载中...</span>
              </div>
              <div class="pill-group" id="engine-route-controls"></div>
            </div>
            <div class="subcard-divider">
              <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:6px; flex-wrap:wrap;">
                <strong style="font-size:12px;">思考深度档位（Reasoning Effort）</strong>
                <span class="muted mono" id="engine-reasoning-status">加载中...</span>
              </div>
              <div class="pill-group" id="engine-reasoning-controls"></div>
            </div>
          </div>

          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <h3 style="margin:0;">在线节点（Workers）</h3>
              <button class="btn btn-sm" id="btn-workers-reload" type="button">刷新</button>
            </div>
            <div class="muted mono" id="workers-summary" style="margin-bottom:8px; font-size:12px;">正在读取在线节点...</div>
            <div class="list-box" id="workers-list" style="max-height:200px;"></div>
          </div>

          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <div style="display:flex; align-items:center; gap:8px;">
                <h3 style="margin:0;">待审批队列（L1 Approvals）</h3>
                <span class="badge mono" id="approvals-badge">0 待审</span>
              </div>
              <button class="btn btn-sm" id="btn-approvals-reload" type="button">刷新</button>
            </div>
            <div class="muted" id="approvals-summary" style="margin-bottom:8px;">正在读取待审批队列...</div>
            <div class="list-box" id="approvals-list" style="max-height:240px;"></div>
          </div>

          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <h3 style="margin:0;">MCP 插件清单与实测探测</h3>
              <div style="display:flex; gap:6px;">
                <button class="btn btn-sm" id="btn-mcp-reload" type="button">刷新</button>
                <button class="btn btn-sm btn-primary" id="btn-mcp-probe" type="button">实测握手 Probe</button>
              </div>
            </div>
            <div class="muted mono" id="mcp-summary" style="margin-bottom:8px; font-size:12px;">正在读取 MCP 配置...</div>
            <div class="list-box" id="mcp-server-list"></div>
          </div>

          <div class="subcard">
            <h3>外部气泡测试条（零 Token）</h3>
            <div class="form-grid">
              <div class="field">
                <label for="bubble-test-text">气泡文本（POST /api/bubble）</label>
                <input class="input" id="bubble-test-text" placeholder="例如：测试一条零 Token 伴侣气泡" />
              </div>
              <div class="field">
                <label for="bubble-test-mood">附带心情（可选）</label>
                <select class="input" id="bubble-test-mood">
                  <option value="">保持当前心情</option>
                  <option value="calm">calm · 平静</option>
                  <option value="happy">happy · 开心</option>
                  <option value="smug">smug · 得意</option>
                  <option value="sleepy">sleepy · 犯困</option>
                  <option value="annoyed">annoyed · 恼火</option>
                </select>
              </div>
              <div class="field">
                <label for="bubble-test-duration">停留时长（秒）</label>
                <input class="input mono" id="bubble-test-duration" type="number" min="1" max="60" step="0.5" value="6" />
              </div>
              <div style="display:flex; align-items:flex-end;">
                <button class="btn btn-primary" id="btn-bubble-send" type="button">发送气泡</button>
              </div>
            </div>
            <div class="muted" id="bubble-test-status" style="margin-top:8px;"></div>
          </div>
        </section>
      </main>

      <!-- Right Sidebar: Avatar Preview + Bubble + Live Events -->
      <aside class="side-card">
        <div class="avatar-stage">
          <img class="avatar-img" id="avatar-img" alt="伴侣立绘预览" />
          <div style="display:flex; gap:6px; flex-wrap:wrap; justify-content:center;">
            <span class="badge" id="side-pack">pack: builtin</span>
            <span class="badge badge-accent" id="side-state-mood">idle / calm</span>
          </div>
          <div class="bubble-box" id="live-bubble">
            <div class="muted">最新气泡</div>
            <div id="live-bubble-text">等待实时事件...</div>
          </div>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center;">
          <strong style="font-size:13px;">实时 SSE 事件流</strong>
          <button class="btn btn-sm" id="btn-clear-events">清空</button>
        </div>
        <div class="event-feed" id="event-feed"></div>
      </aside>
    </div>
  </div>

  <!-- Token Unlock Overlay -->
  <div class="overlay" id="unlock-overlay" role="dialog" aria-modal="true">
    <div class="unlock-card">
      <div class="brand">
        <div class="brand-mark">CS</div>
        <div>
          <div class="brand-title">解锁本地控制台</div>
          <div class="brand-sub">令牌仅保存在当前标签页 sessionStorage</div>
        </div>
      </div>
      <div class="field">
        <label for="unlock-token-input">Bearer 访问令牌</label>
        <input class="input mono" id="unlock-token-input" type="password" placeholder="粘贴守护进程启动令牌..." autocomplete="off" />
      </div>
      <div class="muted" id="unlock-error" style="color: var(--danger); min-height: 18px;"></div>
      <button class="btn btn-primary" id="btn-unlock">验证并进入控制台</button>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    (() => {
      "use strict";

      const TOKEN_KEY = "shinobu_token";
      const STATES = [
        { id: "idle", label: "idle · 待机" },
        { id: "donut", label: "donut · 悠闲" },
        { id: "sleepy", label: "sleepy · 困倦" },
        { id: "thinking", label: "thinking · 思考" },
        { id: "speaking", label: "speaking · 对话" }
      ];
      const MOODS = [
        { id: "calm", label: "calm · 平静" },
        { id: "happy", label: "happy · 开心" },
        { id: "smug", label: "smug · 得意" },
        { id: "sleepy", label: "sleepy · 犯困" },
        { id: "annoyed", label: "annoyed · 恼火" }
      ];

      const ENGINE_MODEL_PRESETS = [
        { id: "", label: "(default)" },
        { id: "gemini-3.8-flash-medium", label: "gemini-3.8-flash-medium" },
        { id: "claude-sonnet-5-5-high", label: "claude-sonnet-5-5-high" },
        { id: "gemini-4-neon-high", label: "gemini-4-neon-high" }
      ];
      const ROUTE_MODES = [
        { id: "auto", label: "auto · 自动混动" },
        { id: "cloud", label: "cloud · 云端主脑" },
        { id: "local", label: "local · 本地算力" }
      ];
      const REASONING_EFFORTS = [
        { id: "default", label: "default · 自动" },
        { id: "off", label: "off · 关闭" },
        { id: "minimal", label: "minimal · 极简" },
        { id: "low", label: "low · 轻度" },
        { id: "medium", label: "medium · 均衡" },
        { id: "high", label: "high · 深度" }
      ];
      const DEFAULT_COMMUNITY_FRONTENDS = [
        { id: "airi", name: "Project AIRI", provider: "OpenAI Compatible", url: "https://github.com/moeru-ai/airi" },
        { id: "open-llm-vtuber", name: "Open-LLM-VTuber", provider: "OpenAI Compatible", url: "https://github.com/Open-LLM-VTuber/Open-LLM-VTuber" }
      ];

      const app = {
        token: "",
        state: "idle",
        mood: "calm",
        episodeId: "",
        viewingEpisodeId: "",
        episodes: { active: null, recentClosed: [] },
        episodesCollapsed: false,
        eventSource: null,
        avatarBlobUrl: null,
        streaming: false,
        soulFiles: {},
        activeSoul: "SOUL.md",
        facts: [],
        journals: [],
        compat: null,
        compatKeyVisible: false,
        engine: null,
        workers: [],
        approvals: [],
        mcp: null,
        mcpProbe: null,
        sseSyncTimer: null
      };

      const $ = (id) => document.getElementById(id);

      function escapeHtml(raw) {
        return String(raw == null ? "" : raw)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      }

      function renderInlineMarkdown(escapedLine) {
        const inlineCodes = [];
        let out = escapedLine.replace(/`([^`\n]+)`/g, (_m, code) => {
          const idx = inlineCodes.length;
          inlineCodes.push('<code class="md-code">' + code + "</code>");
          return "\x00IC" + idx + "\x00";
        });
        out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        out = out.replace(/\x00IC(\d+)\x00/g, (_m, i) => inlineCodes[Number(i)] || "");
        return out;
      }

      function renderSafeMarkdown(raw) {
        const text = String(raw == null ? "" : raw).replace(/\r\n?/g, "\n");
        const codeBlocks = [];
        const withoutBlocks = text.replace(/```[^\n`]*\n?([\s\S]*?)```/g, (_m, code) => {
          const idx = codeBlocks.length;
          const cleanCode = escapeHtml(code.replace(/\n$/, ""));
          codeBlocks.push('<pre class="md-pre"><code>' + cleanCode + "</code></pre>");
          return "\n\x00CB" + idx + "\x00\n";
        });

        const lines = escapeHtml(withoutBlocks).split("\n");
        const htmlParts = [];
        let listTag = "";
        const closeList = () => {
          if (listTag) {
            htmlParts.push("</" + listTag + ">");
            listTag = "";
          }
        };

        for (let i = 0; i < lines.length; i += 1) {
          const line = lines[i];
          const blockMatch = line.match(/^\x00CB(\d+)\x00$/);
          if (blockMatch) {
            closeList();
            htmlParts.push(codeBlocks[Number(blockMatch[1])] || "");
            continue;
          }
          const ulMatch = line.match(/^\s*[-*•]\s+(.+)$/);
          if (ulMatch) {
            if (listTag && listTag !== "ul") closeList();
            if (!listTag) {
              listTag = "ul";
              htmlParts.push('<ul class="md-list">');
            }
            htmlParts.push("<li>" + renderInlineMarkdown(ulMatch[1]) + "</li>");
            continue;
          }
          const olMatch = line.match(/^\s*\d+\.\s+(.+)$/);
          if (olMatch) {
            if (listTag && listTag !== "ol") closeList();
            if (!listTag) {
              listTag = "ol";
              htmlParts.push('<ol class="md-list">');
            }
            htmlParts.push("<li>" + renderInlineMarkdown(olMatch[1]) + "</li>");
            continue;
          }
          closeList();
          const rendered = renderInlineMarkdown(line);
          if (htmlParts.length > 0 && !htmlParts[htmlParts.length - 1].endsWith("</pre>") && !htmlParts[htmlParts.length - 1].endsWith("</ul>") && !htmlParts[htmlParts.length - 1].endsWith("</ol>")) {
            htmlParts.push("<br>" + rendered);
          } else {
            htmlParts.push(rendered);
          }
        }
        closeList();
        return htmlParts.join("");
      }

      function setMessageContent(el, text, useMarkdown = true) {
        if (!useMarkdown) {
          el.textContent = String(text == null ? "" : text);
          return;
        }
        el.innerHTML = renderSafeMarkdown(text);
      }

      function showToast(msg) {
        const el = $("toast");
        el.textContent = msg;
        el.classList.add("show");
        clearTimeout(showToast._t);
        showToast._t = setTimeout(() => el.classList.remove("show"), 2600);
      }

      function bootstrapTokenFromHash() {
        const rawHash = window.location.hash ? window.location.hash.slice(1) : "";
        if (rawHash) {
          const params = new URLSearchParams(rawHash);
          const hashToken = (params.get("token") || "").trim();
          if (hashToken) {
            sessionStorage.setItem(TOKEN_KEY, hashToken);
          }
          if (window.history && typeof window.history.replaceState === "function") {
            window.history.replaceState(null, "", window.location.pathname);
          }
        }
        app.token = (sessionStorage.getItem(TOKEN_KEY) || "").trim();
      }

      function openUnlock(errMsg = "") {
        disconnectSSE();
        $("unlock-error").textContent = errMsg;
        $("unlock-overlay").classList.add("open");
        $("unlock-token-input").focus();
      }

      function closeUnlock() {
        $("unlock-overlay").classList.remove("open");
        $("unlock-error").textContent = "";
      }

      async function apiFetch(path, options = {}) {
        const headers = new Headers(options.headers || {});
        if (app.token) {
          headers.set("Authorization", "Bearer " + app.token);
        }
        if (options.body && !headers.has("Content-Type")) {
          headers.set("Content-Type", "application/json; charset=utf-8");
        }
        const resp = await fetch(path, { ...options, headers });
        if (resp.status === 401) {
          sessionStorage.removeItem(TOKEN_KEY);
          app.token = "";
          openUnlock("访问令牌无效或已过期，请重新输入。");
          throw new Error("unauthorized");
        }
        return resp;
      }

      async function apiJson(path, options = {}) {
        const resp = await apiFetch(path, options);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(data.error || data.detail || ("HTTP " + resp.status));
        }
        return data;
      }

      function setSseStatus(kind, text) {
        const dot = $("dot-sse");
        dot.className = "dot " + kind;
        $("text-sse").textContent = text;
      }

      function updateStateDisplay(nextState, nextMood, episodeId) {
        if (nextState) app.state = nextState;
        if (nextMood) app.mood = nextMood;
        if (episodeId !== undefined && episodeId !== null) app.episodeId = episodeId;

        $("badge-state").textContent = app.state;
        $("badge-mood").textContent = app.mood;
        $("badge-episode").textContent = "ep: " + (app.episodeId || "-");
        $("side-state-mood").textContent = app.state + " / " + app.mood;

        document.querySelectorAll("#state-controls .pill").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.state === app.state);
        });
        document.querySelectorAll("#mood-controls .pill").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.mood === app.mood);
        });

        syncReadonlyChatMode();
        refreshAvatar(app.state, app.mood);
      }

      async function refreshAvatar(state, mood) {
        if (!app.token) return;
        try {
          const url = "/api/avatar?state=" + encodeURIComponent(state) + "&mood=" + encodeURIComponent(mood);
          const resp = await apiFetch(url, { method: "GET" });
          if (!resp.ok) return;
          const pack = resp.headers.get("X-Shinobu-Avatar-Pack") || "builtin";
          const blob = await resp.blob();
          const nextUrl = URL.createObjectURL(blob);
          const img = $("avatar-img");
          img.src = nextUrl;
          if (app.avatarBlobUrl) {
            URL.revokeObjectURL(app.avatarBlobUrl);
          }
          app.avatarBlobUrl = nextUrl;
          $("side-pack").textContent = "pack: " + pack;
        } catch (_err) {
          /* silent fallback */
        }
      }

      function appendEventRow(kind, summary) {
        const feed = $("event-feed");
        const row = document.createElement("div");
        row.className = "event-row";
        const ts = new Date().toLocaleTimeString();
        const header = document.createElement("div");
        header.className = "muted mono";
        header.textContent = "[" + ts + "] " + kind;
        const body = document.createElement("div");
        body.textContent = summary;
        row.appendChild(header);
        row.appendChild(body);
        feed.prepend(row);
        while (feed.children.length > 60) {
          feed.removeChild(feed.lastChild);
        }
      }

      function updateBubble(text, source) {
        const label = source ? "[" + source + "] " : "";
        $("live-bubble-text").textContent = label + text;
      }

      function disconnectSSE() {
        if (app.eventSource) {
          app.eventSource.close();
          app.eventSource = null;
        }
        setSseStatus("", "未连接");
      }

      function scheduleSseSessionSync(options = {}) {
        const { refreshTurns = false, refreshEpisodes = true, refreshState = false } = options;
        clearTimeout(app.sseSyncTimer);
        app.sseSyncTimer = setTimeout(async () => {
          if (!app.token) return;
          const jobs = [];
          if (refreshState) {
            jobs.push(
              apiJson("/state")
                .then((snap) => updateStateDisplay(snap.state || app.state, snap.mood || app.mood, snap.episode_id || app.episodeId))
                .catch(() => {})
            );
          }
          if (refreshEpisodes) {
            jobs.push(loadEpisodes().catch(() => {}));
          }
          if (refreshTurns && !app.streaming && !app.viewingEpisodeId) {
            jobs.push(loadTurns().catch(() => {}));
          }
          await Promise.allSettled(jobs);
        }, 260);
      }

      function connectSSE() {
        disconnectSSE();
        if (!app.token) return;
        const es = new EventSource("/events?token=" + encodeURIComponent(app.token));
        app.eventSource = es;
        setSseStatus("warn", "连接中...");

        es.onopen = () => {
          setSseStatus("ok", "SSE 实时在线");
        };

        es.onerror = () => {
          if (es.readyState === 2) {
            setSseStatus("err", "SSE 已断开");
          } else {
            setSseStatus("warn", "SSE 重连中");
          }
        };

        const handleFrame = (fallbackKind) => (evt) => {
          setSseStatus("ok", "SSE 实时在线");
          let frame = {};
          try { frame = JSON.parse(evt.data); } catch (_e) { frame = { payload: { text: evt.data } }; }
          const kind = frame.kind || fallbackKind || "event";
          const payload = frame.payload || {};

          if (kind === "state_changed") {
            const st = payload.state || payload.to_state || app.state;
            const md = payload.mood || payload.to_mood || app.mood;
            const nextEp = frame.episode_id || payload.episode_id || app.episodeId;
            const epChanged = Boolean(nextEp && nextEp !== app.episodeId);
            updateStateDisplay(st, md, nextEp);
            appendEventRow("state_changed", st + " / " + md + (payload.reason ? " (" + payload.reason + ")" : ""));
            scheduleSseSessionSync({ refreshTurns: epChanged, refreshEpisodes: true, refreshState: false });
          } else if (kind === "bubble") {
            const text = payload.text || payload.message || JSON.stringify(payload);
            const src = payload.source || "daemon";
            updateBubble(text, src);
            appendEventRow("bubble", "[" + src + "] " + text);
          } else if (kind === "done") {
            const text = payload.text || "";
            const nextEp = frame.episode_id || payload.episode_id || app.episodeId;
            if (nextEp && nextEp !== app.episodeId) {
              updateStateDisplay(null, null, nextEp);
            }
            if (text) updateBubble(text.slice(0, 90), "chat");
            appendEventRow("done", text ? text.slice(0, 72) : "对话完成");
            scheduleSseSessionSync({ refreshTurns: true, refreshEpisodes: true, refreshState: true });
          } else if (kind === "heartbeat") {
            appendEventRow("heartbeat", payload.source || "tick");
          } else if (kind === "error") {
            appendEventRow("error", payload.error || JSON.stringify(payload));
          } else if (kind === "approval_requested" || kind === "approval_decided") {
            appendEventRow(kind, payload.action || payload.id || "审批状态更新");
            loadApprovals().catch(() => {});
          }
        };

        ["state_changed", "bubble", "done", "heartbeat", "error", "approval_requested", "approval_decided"].forEach((evName) => {
          es.addEventListener(evName, handleFrame(evName));
        });
        es.onmessage = handleFrame("message");
      }

      /* ---------------- Chat & Episodes ---------------- */
      function normalizeEpisode(raw) {
        if (!raw) return null;
        if (typeof raw === "string") {
          return { id: raw, title: "", started_at: "", turn_count: null };
        }
        const id = String(raw.id || raw.episode_id || "").trim();
        if (!id) return null;
        return {
          id,
          title: raw.title || raw.summary || raw.label || "",
          started_at: raw.started_at || raw.created_at || "",
          closed_at: raw.closed_at || raw.ended_at || "",
          turn_count: raw.turn_count !== undefined ? raw.turn_count : raw.turns_count
        };
      }

      function syncReadonlyChatMode() {
        const isReadonly = Boolean(app.viewingEpisodeId && app.viewingEpisodeId !== app.episodeId);
        const banner = $("chat-readonly-banner");
        const input = $("chat-input");
        const sendBtn = $("btn-chat-send");
        const backBtn = $("btn-episode-back-active");
        const viewBadge = $("episode-view-badge");

        if (banner) banner.classList.toggle("open", isReadonly);
        if (backBtn) backBtn.style.display = isReadonly ? "" : "none";
        if (viewBadge) {
          viewBadge.textContent = isReadonly
            ? ("只读回看 · " + app.viewingEpisodeId)
            : ("当前活跃会话 · " + (app.episodeId || "-"));
        }
        if (input) {
          input.disabled = isReadonly;
          input.placeholder = isReadonly
            ? "只读回看模式：请先点击「返回当前活跃会话」后再发送消息..."
            : "输入消息与伴侣对话（Enter 发送，Shift+Enter 换行）...";
        }
        if (sendBtn && !app.streaming) {
          sendBtn.disabled = isReadonly;
        }
      }

      function renderEpisodesBar() {
        const host = $("episode-list");
        if (!host) return;
        host.classList.toggle("collapsed", app.episodesCollapsed);
        $("btn-episode-toggle").textContent = app.episodesCollapsed ? "展开列表" : "收起列表";
        host.innerHTML = "";

        const active = app.episodes.active;
        const recent = Array.isArray(app.episodes.recentClosed) ? app.episodes.recentClosed : [];
        const activeId = (active && active.id) || app.episodeId || "";
        const viewingId = app.viewingEpisodeId || activeId;

        if (activeId) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "pill mono" + (!app.viewingEpisodeId || viewingId === activeId ? " active" : "");
          const countHint = active && active.turn_count !== null && active.turn_count !== undefined
            ? (" (" + active.turn_count + ")")
            : "";
          btn.textContent = "● 当前 " + activeId.slice(0, 12) + countHint;
          btn.title = "当前活跃会话：" + activeId;
          btn.onclick = () => selectEpisode("");
          host.appendChild(btn);
        }

        for (const ep of recent) {
          if (!ep || !ep.id || ep.id === activeId) continue;
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "pill mono" + (app.viewingEpisodeId === ep.id ? " active" : "");
          const shortTitle = ep.title ? (" · " + String(ep.title).slice(0, 14)) : "";
          const countHint = ep.turn_count !== null && ep.turn_count !== undefined ? (" (" + ep.turn_count + ")") : "";
          btn.textContent = ep.id.slice(0, 12) + countHint + shortTitle;
          btn.title = [ep.id, ep.title, ep.started_at].filter(Boolean).join(" | ");
          btn.onclick = () => selectEpisode(ep.id);
          host.appendChild(btn);
        }

        if (host.children.length === 0) {
          const empty = document.createElement("span");
          empty.className = "muted";
          empty.textContent = "暂无可切换会话。";
          host.appendChild(empty);
        }
        syncReadonlyChatMode();
      }

      async function loadEpisodes() {
        try {
          const data = await apiJson("/api/episodes?limit=15");
          const active = normalizeEpisode(data.active_episode);
          const recentRaw = Array.isArray(data.recent_closed) ? data.recent_closed : [];
          const recentClosed = recentRaw.map(normalizeEpisode).filter(Boolean);
          app.episodes = { active, recentClosed };
          if (active && active.id && active.id !== app.episodeId) {
            updateStateDisplay(null, null, active.id);
          }
          renderEpisodesBar();
        } catch (_err) {
          renderEpisodesBar();
        }
      }

      async function selectEpisode(episodeId) {
        const target = String(episodeId || "").trim();
        const activeId = (app.episodes.active && app.episodes.active.id) || app.episodeId || "";
        if (!target || (activeId && target === activeId)) {
          app.viewingEpisodeId = "";
          renderEpisodesBar();
          await loadTurns();
          return;
        }
        app.viewingEpisodeId = target;
        renderEpisodesBar();
        await loadTurns(target);
      }

      async function createNewEpisode() {
        if (app.streaming) {
          showToast("当前正在流式生成，请稍候再新开对话");
          return;
        }
        try {
          const data = await apiJson("/api/episodes/new", {
            method: "POST",
            body: JSON.stringify({})
          });
          const nextEp = data.episode_id
            || (data.active_episode && (data.active_episode.id || data.active_episode.episode_id))
            || (data.episode && (data.episode.id || data.episode.episode_id))
            || "";
          app.viewingEpisodeId = "";
          if (nextEp) {
            updateStateDisplay(null, null, nextEp);
          }
          await Promise.allSettled([loadEpisodes(), loadTurns()]);
          showToast("已秒开新对话" + (nextEp ? "（" + nextEp.slice(0, 12) + "）" : ""));
        } catch (err) {
          showToast("秒开新对话失败：" + (err.message || "未知错误"));
        }
      }

      let activeTtsAudio = null;
      let activeTtsUrl = null;
      let activeTtsBtn = null;

      function stopActiveTts() {
        if (activeTtsAudio) {
          try { activeTtsAudio.pause(); } catch (_e) {}
          activeTtsAudio = null;
        }
        if (activeTtsUrl) {
          try { URL.revokeObjectURL(activeTtsUrl); } catch (_e) {}
          activeTtsUrl = null;
        }
        if (activeTtsBtn) {
          activeTtsBtn.classList.remove("playing");
          activeTtsBtn.disabled = false;
          activeTtsBtn.textContent = "🔊 朗读";
          activeTtsBtn = null;
        }
      }

      async function speakText(text, btn) {
        const clean = String(text || "").trim();
        if (!clean) {
          showToast("暂无可朗读的回复内容");
          return;
        }
        if (activeTtsBtn === btn && activeTtsAudio) {
          stopActiveTts();
          return;
        }
        stopActiveTts();
        if (btn) {
          btn.disabled = true;
          btn.textContent = "⏳ 合成中…";
        }
        try {
          const resp = await apiFetch("/api/tts", {
            method: "POST",
            body: JSON.stringify({
              text: clean,
              mood: app.mood || "calm",
              fmt: "mp3"
            })
          });
          if (!resp.ok) {
            let errDetail = "";
            try {
              const errData = await resp.json();
              errDetail = errData.error || errData.detail || "";
            } catch (_e) {}
            if (resp.status === 503 || errDetail === "tts_unavailable") {
              showToast("语音合成暂不可用（请先在服务端配置 TTS）");
            } else {
              showToast("语音合成失败（HTTP " + resp.status + "）");
            }
            if (btn) {
              btn.disabled = false;
              btn.textContent = "🔊 朗读";
            }
            return;
          }
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const audio = new Audio(url);
          activeTtsAudio = audio;
          activeTtsUrl = url;
          activeTtsBtn = btn || null;
          if (btn) {
            btn.disabled = false;
            btn.classList.add("playing");
            btn.textContent = "⏹ 停止";
          }
          const cleanup = () => {
            if (activeTtsAudio === audio) {
              stopActiveTts();
            } else {
              try { URL.revokeObjectURL(url); } catch (_e) {}
            }
          };
          audio.addEventListener("ended", cleanup);
          audio.addEventListener("error", () => {
            cleanup();
            showToast("浏览器无法播放该语音音频");
          });
          await audio.play();
        } catch (err) {
          stopActiveTts();
          if (err && err.message !== "unauthorized") {
            showToast("语音播报失败：" + (err.message || "未知错误"));
          }
        }
      }

      function appendChatMessage(role, text, metaText = "", useMarkdown = true) {
        const log = $("chat-log");
        const wrap = document.createElement("div");
        wrap.className = "msg " + (role === "user" ? "user" : role === "assistant" ? "assistant" : "system");
        const content = document.createElement("div");
        content.className = "msg-body";
        content.dataset.rawText = String(text || "");
        setMessageContent(content, text, useMarkdown && role !== "system");
        if (metaText || role === "assistant") {
          const meta = document.createElement("div");
          meta.className = "msg-meta";
          const label = document.createElement("span");
          label.textContent = metaText || (role === "assistant" ? "伴侣" : "");
          meta.appendChild(label);
          if (role === "assistant") {
            const ttsBtn = document.createElement("button");
            ttsBtn.type = "button";
            ttsBtn.className = "tts-btn";
            ttsBtn.textContent = "🔊 朗读";
            ttsBtn.title = "朗读此条回复";
            ttsBtn.addEventListener("click", () => {
              const targetText = content.dataset.rawText || content.textContent || "";
              speakText(targetText, ttsBtn);
            });
            meta.appendChild(ttsBtn);
          }
          wrap.appendChild(meta);
        }
        wrap.appendChild(content);
        log.appendChild(wrap);
        log.scrollTop = log.scrollHeight;
        return content;
      }

      async function loadTurns(episodeId = "") {
        const target = String(episodeId || app.viewingEpisodeId || "").trim();
        const isHistory = Boolean(target && target !== app.episodeId);
        const url = isHistory
          ? ("/api/turns?episode_id=" + encodeURIComponent(target) + "&limit=100")
          : "/api/turns?limit=50";
        const data = await apiJson(url);
        if (!isHistory && data.episode_id) {
          updateStateDisplay(null, null, data.episode_id);
        }
        const log = $("chat-log");
        log.innerHTML = "";
        const turns = Array.isArray(data.turns) ? data.turns : [];
        if (turns.length === 0) {
          appendChatMessage(
            "system",
            isHistory ? ("历史会话 " + target + " 暂无记录。") : "当前会话尚无历史消息，发一句话打个招呼吧。",
            "",
            false
          );
          syncReadonlyChatMode();
          return;
        }
        for (const t of turns) {
          const role = t.role || "assistant";
          const body = t.content || t.text || "";
          const ts = t.created_at || t.timestamp || "";
          appendChatMessage(role, body, role + (ts ? " · " + ts : ""), true);
        }
        syncReadonlyChatMode();
      }

      async function sendChat() {
        if (app.streaming) return;
        if (app.viewingEpisodeId && app.viewingEpisodeId !== app.episodeId) {
          showToast("当前为历史只读回看模式，请先返回当前活跃会话");
          return;
        }
        const input = $("chat-input");
        const text = input.value.trim();
        if (!text) return;

        input.value = "";
        app.streaming = true;
        $("btn-chat-send").disabled = true;

        appendChatMessage("user", text, "你 · 刚刚", true);
        const assistantBody = appendChatMessage("assistant", "", "伴侣 · 实时流式生成中", false);

        try {
          const resp = await apiFetch("/chat", {
            method: "POST",
            body: JSON.stringify({ text, stream: true })
          });
          if (!resp.ok || !resp.body) {
            throw new Error("HTTP " + resp.status);
          }

          const reader = resp.body.getReader();
          const decoder = new TextDecoder("utf-8");
          let buffer = "";
          let assembled = "";
          let hadError = false;

          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const blocks = buffer.split(/\r?\n\r?\n/);
            buffer = blocks.pop() || "";

            for (const block of blocks) {
              let evName = "";
              const dataLines = [];
              for (const line of block.split(/\r?\n/)) {
                if (line.startsWith("event:")) {
                  evName = line.slice(6).trim();
                } else if (line.startsWith("data:")) {
                  dataLines.push(line.slice(5).trim());
                }
              }
              if (dataLines.length === 0) continue;
              let parsed = {};
              try { parsed = JSON.parse(dataLines.join("\n")); } catch (_e) { continue; }
              const kind = parsed.kind || evName;
              const payload = parsed.payload || {};

              if (kind === "token_delta") {
                const delta = payload.delta || "";
                assembled += delta;
                assistantBody.dataset.rawText = assembled;
                assistantBody.textContent = assembled;
                $("chat-log").scrollTop = $("chat-log").scrollHeight;
              } else if (kind === "done") {
                if (!assembled && payload.text) {
                  assembled = String(payload.text);
                }
                if (assembled) {
                  assistantBody.dataset.rawText = assembled;
                  setMessageContent(assistantBody, assembled, true);
                }
                if (parsed.episode_id) {
                  updateStateDisplay(null, null, parsed.episode_id);
                }
              } else if (kind === "error") {
                hadError = true;
                assistantBody.dataset.rawText = "";
                assistantBody.textContent = "[错误] " + (payload.error || "未知错误");
              }
            }
          }
          if (!hadError) {
            if (assembled) {
              assistantBody.dataset.rawText = assembled;
              setMessageContent(assistantBody, assembled, true);
            } else if (!assistantBody.textContent) {
              assistantBody.dataset.rawText = "";
              assistantBody.textContent = "（空回复）";
            }
          }
          loadEpisodes().catch(() => {});
        } catch (err) {
          if (err.message !== "unauthorized") {
            assistantBody.dataset.rawText = "";
            assistantBody.textContent = "[发送失败] " + err.message;
          }
        } finally {
          app.streaming = false;
          syncReadonlyChatMode();
          if (!input.disabled) input.focus();
        }
      }

      /* ---------------- Soul & Facts ---------------- */
      function renderSoulEditor() {
        const text = app.soulFiles[app.activeSoul] || "";
        $("soul-editor").value = text;
        updateSoulByteCount();
        document.querySelectorAll("#soul-tabs .pill").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.soul === app.activeSoul);
        });
      }

      function updateSoulByteCount() {
        const bytes = new TextEncoder().encode($("soul-editor").value).length;
        $("soul-meta").textContent = app.activeSoul + " · " + bytes + " bytes";
      }

      async function loadSoulFiles() {
        const data = await apiJson("/api/soul");
        app.soulFiles = data.files || {};
        renderSoulEditor();
      }

      async function saveActiveSoul() {
        const content = $("soul-editor").value;
        const name = app.activeSoul;
        const data = await apiJson("/api/soul/" + encodeURIComponent(name), {
          method: "PUT",
          body: JSON.stringify({ content })
        });
        app.soulFiles[name] = content;
        const backupMsg = data.backup ? "，旧版已留底为 " + data.backup : "（首次创建）";
        $("soul-notice").textContent = "已保存 " + name + "（" + data.bytes + " bytes）" + backupMsg + "。";
        showToast("已保存 " + name);
      }

      function renderFactsList() {
        const list = $("facts-list");
        const countEl = $("facts-count");
        const keyword = ($("facts-filter-input") ? $("facts-filter-input").value : "").trim().toLowerCase();
        list.innerHTML = "";
        const allFacts = Array.isArray(app.facts) ? app.facts : [];
        const filtered = keyword
          ? allFacts.filter((f) => {
              const hay = [f.key, f.value, f.category, f.id].map((v) => String(v == null ? "" : v).toLowerCase()).join(" ");
              return hay.includes(keyword);
            })
          : allFacts;

        if (countEl) {
          countEl.textContent = keyword
            ? (filtered.length + " / " + allFacts.length + " 条")
            : (allFacts.length + " 条");
        }

        if (filtered.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = keyword ? ("未找到匹配「" + keyword + "」的长期记忆。") : "暂无结构化事实记忆。";
          list.appendChild(empty);
          return;
        }
        for (const f of filtered) {
          const item = document.createElement("div");
          item.className = "fact-row";

          const left = document.createElement("div");
          left.style.flex = "1";
          left.style.minWidth = "0";
          const title = document.createElement("div");
          title.style.fontWeight = "600";
          title.style.wordBreak = "break-word";
          title.textContent = (f.key || "fact") + " = " + (f.value !== undefined ? String(f.value) : "");
          const metaParts = [];
          if (f.confidence !== undefined && f.confidence !== null) {
            metaParts.push("conf=" + Number(f.confidence).toFixed(2));
          }
          if (f.category) metaParts.push(f.category);
          if (f.updated_at || f.created_at) metaParts.push(f.updated_at || f.created_at);
          if (f.id !== undefined && f.id !== null) metaParts.push("#" + f.id);
          const sub = document.createElement("div");
          sub.className = "muted mono";
          sub.textContent = metaParts.join(" · ");
          left.appendChild(title);
          if (sub.textContent) left.appendChild(sub);

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger";
          delBtn.textContent = "删除";
          delBtn.onclick = () => deleteFact(f);

          item.appendChild(left);
          item.appendChild(delBtn);
          list.appendChild(item);
        }
      }

      async function loadFacts() {
        const data = await apiJson("/api/memory/facts?limit=100");
        app.facts = Array.isArray(data.facts) ? data.facts : [];
        renderFactsList();
      }

      async function addOrUpdateFact() {
        const key = $("fact-key-input").value.trim();
        const value = $("fact-value-input").value.trim();
        const confRaw = Number($("fact-confidence-input").value);
        if (!key || !value) {
          showToast("请填写记忆键（key）和记忆内容（value）");
          return;
        }
        const confidence = Number.isFinite(confRaw) ? Math.min(1, Math.max(0, confRaw)) : 0.9;
        const btn = $("btn-fact-add");
        btn.disabled = true;
        try {
          await apiJson("/api/memory/facts", {
            method: "POST",
            body: JSON.stringify({ key, value, confidence })
          });
          $("fact-key-input").value = "";
          $("fact-value-input").value = "";
          await loadFacts();
          showToast("已保存长期记忆：" + key);
        } catch (err) {
          showToast("保存记忆失败：" + (err.message || "未知错误"));
        } finally {
          btn.disabled = false;
        }
      }

      async function deleteFact(fact) {
        const label = fact && (fact.key || fact.id) ? String(fact.key || fact.id) : "该条记忆";
        if (!window.confirm("确认删除长期记忆「" + label + "」吗？")) return;
        const factId = fact && fact.id !== undefined && fact.id !== null ? String(fact.id).trim() : "";
        if (!factId) {
          showToast("该条记忆缺少 id，暂无法通过接口删除");
          return;
        }
        try {
          const resp = await apiFetch("/api/memory/facts/" + encodeURIComponent(factId), {
            method: "DELETE"
          });
          if (resp.status === 404 || resp.status === 405) {
            showToast("当前服务端未开启或未找到该记忆删除接口（HTTP " + resp.status + "）");
            return;
          }
          if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            throw new Error(errData.error || errData.detail || ("HTTP " + resp.status));
          }
          await loadFacts();
          showToast("已删除长期记忆：" + label);
        } catch (err) {
          if (err.message !== "unauthorized") {
            showToast("删除记忆失败：" + (err.message || "未知错误"));
          }
        }
      }

      function renderMemoryCompactResult(res) {
        const box = $("memory-compact-box");
        if (!box) return;
        box.textContent = "";
        if (!res || typeof res !== "object") {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "暂无体检结果。";
          box.appendChild(empty);
          return;
        }

        const header = document.createElement("div");
        header.style.display = "flex";
        header.style.flexWrap = "wrap";
        header.style.gap = "8px";
        header.style.alignItems = "center";
        header.style.marginBottom = "8px";

        const badge = document.createElement("span");
        badge.className = "badge mono";
        badge.textContent = res.dry_run
          ? "预览（未写入）"
          : (res.applied ? "已应用精炼" : "无变更");
        header.appendChild(badge);

        const beforeChars = Number.isFinite(Number(res.before_chars)) ? Number(res.before_chars) : 0;
        const afterChars = Number.isFinite(Number(res.after_chars)) ? Number(res.after_chars) : 0;
        const updatedFacts = Array.isArray(res.updated_facts) ? res.updated_facts : [];
        const removedIds = Array.isArray(res.removed_fact_ids) ? res.removed_fact_ids : [];

        const stats = document.createElement("span");
        stats.className = "muted mono";
        stats.textContent =
          "MEMORY.md: " + beforeChars + " -> " + afterChars + " chars | " +
          "更新 Facts: " + updatedFacts.length + " | 删除 Facts: " + removedIds.length;
        header.appendChild(stats);
        box.appendChild(header);

        const summaryEl = document.createElement("div");
        summaryEl.style.marginBottom = "8px";
        summaryEl.textContent = "体检摘要：" + (res.summary || "无摘要");
        box.appendChild(summaryEl);

        if (res.backup_path) {
          const backupEl = document.createElement("div");
          backupEl.className = "muted mono";
          backupEl.style.marginBottom = "8px";
          backupEl.textContent = "已留底旧版：" + String(res.backup_path);
          box.appendChild(backupEl);
        }

        if (updatedFacts.length > 0 || removedIds.length > 0) {
          const detailEl = document.createElement("div");
          detailEl.className = "muted mono";
          detailEl.style.marginBottom = "8px";
          const parts = [];
          if (updatedFacts.length > 0) {
            parts.push("更新键: " + updatedFacts.map((f) => String(f && f.key ? f.key : "")).filter(Boolean).join(", "));
          }
          if (removedIds.length > 0) {
            parts.push("删除 ID: " + removedIds.join(", "));
          }
          detailEl.textContent = parts.join(" | ");
          box.appendChild(detailEl);
        }

        const previewLabel = document.createElement("div");
        previewLabel.className = "muted";
        previewLabel.style.marginBottom = "4px";
        previewLabel.textContent = "精炼后 MEMORY.md 预览：";
        box.appendChild(previewLabel);

        const pre = document.createElement("pre");
        pre.className = "mono";
        pre.style.margin = "0";
        pre.style.padding = "8px";
        pre.style.whiteSpace = "pre-wrap";
        pre.style.wordBreak = "break-word";
        pre.style.borderRadius = "8px";
        pre.style.background = "rgba(15, 23, 42, 0.05)";
        pre.textContent = typeof res.preview_memory_md === "string" && res.preview_memory_md.trim()
          ? res.preview_memory_md
          : "(空)";
        box.appendChild(pre);
      }

      async function runMemoryCompaction(dryRun) {
        const isDryRun = Boolean(dryRun);
        if (!isDryRun) {
          const ok = window.confirm(
            "确认应用长期记忆精炼吗？应用前将自动留底 MEMORY_vN.md，不会修改原始日记 journals/。"
          );
          if (!ok) return;
        }
        const previewBtn = $("btn-memory-compact-preview");
        const applyBtn = $("btn-memory-compact-apply");
        const box = $("memory-compact-box");
        const prevApplyDisabled = applyBtn ? applyBtn.disabled : true;
        if (previewBtn) previewBtn.disabled = true;
        if (applyBtn) applyBtn.disabled = true;
        if (box) {
          box.textContent = isDryRun ? "正在执行记忆体检预览，请稍候..." : "正在留底并应用记忆精炼，请稍候...";
        }
        try {
          const data = await apiJson("/api/memory/compact", {
            method: "POST",
            body: JSON.stringify({ dry_run: isDryRun })
          });
          const res = (data && data.result && typeof data.result === "object") ? data.result : data;
          renderMemoryCompactResult(res);
          if (isDryRun) {
            if (applyBtn) applyBtn.disabled = false;
            showToast("记忆体检预览完成，请核对后点击「确认应用精炼」");
          } else {
            if (applyBtn) applyBtn.disabled = true;
            if (res && res.applied) {
              await Promise.all([loadSoulFiles(), loadFacts()]);
              showToast("已应用长期记忆精炼并完成留底");
            } else {
              showToast("未产生有效变更，原记忆文件保持不变");
            }
          }
        } catch (err) {
          if (applyBtn) applyBtn.disabled = prevApplyDisabled;
          if (box) {
            box.textContent = "记忆体检失败：" + (err.message || "未知错误");
          }
          if (err.message !== "unauthorized") {
            showToast("记忆体检失败：" + (err.message || "未知错误"));
          }
        } finally {
          if (previewBtn) previewBtn.disabled = false;
        }
      }

      /* ---------------- Journals & FTS5 Search ---------------- */
      async function loadJournals() {
        const data = await apiJson("/api/journals");
        app.journals = Array.isArray(data.journals) ? data.journals : [];
        const box = $("journal-list");
        box.innerHTML = "";
        if (app.journals.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "尚无日记文件（执行 /sleep 后将自动生成）。";
          box.appendChild(empty);
          $("journal-title").textContent = "暂无日记";
          $("journal-mtime").textContent = "";
          $("journal-content").textContent = "";
          return;
        }
        app.journals.forEach((j, idx) => {
          const btn = document.createElement("button");
          btn.className = "list-item" + (idx === 0 ? " active" : "");
          const nameEl = document.createElement("div");
          nameEl.style.fontWeight = "600";
          nameEl.textContent = j.name;
          const timeEl = document.createElement("div");
          timeEl.className = "muted mono";
          timeEl.textContent = j.mtime || "";
          btn.appendChild(nameEl);
          btn.appendChild(timeEl);
          btn.onclick = () => {
            document.querySelectorAll("#journal-list .list-item").forEach((el) => el.classList.remove("active"));
            btn.classList.add("active");
            $("journal-title").textContent = j.name;
            $("journal-mtime").textContent = j.mtime || "";
            $("journal-content").textContent = j.content || "";
          };
          box.appendChild(btn);
        });
        const first = app.journals[0];
        $("journal-title").textContent = first.name;
        $("journal-mtime").textContent = first.mtime || "";
        $("journal-content").textContent = first.content || "";
      }

      async function runSearch() {
        const q = $("search-input").value.trim();
        const box = $("search-results");
        box.innerHTML = "";
        if (!q) {
          const hint = document.createElement("div");
          hint.className = "muted";
          hint.textContent = "请输入检索关键词。";
          box.appendChild(hint);
          return;
        }
        const data = await apiJson("/api/memory/search?q=" + encodeURIComponent(q) + "&limit=20");
        const results = Array.isArray(data.results) ? data.results : [];
        if (results.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "未检索到匹配记忆：" + q;
          box.appendChild(empty);
          return;
        }
        for (const r of results) {
          const row = document.createElement("div");
          row.className = "event-row";
          const head = document.createElement("div");
          head.className = "muted mono";
          head.textContent = [r.source || r.kind || "memory", r.created_at || r.updated_at || ""].filter(Boolean).join(" · ");
          const body = document.createElement("div");
          body.textContent = r.snippet || r.content || r.text || r.value || JSON.stringify(r);
          row.appendChild(head);
          row.appendChild(body);
          box.appendChild(row);
        }
      }

      /* ---------------- Timemap, State & Sleep ---------------- */
      function buildStateMoodButtons() {
        const sWrap = $("state-controls");
        sWrap.innerHTML = "";
        for (const item of STATES) {
          const btn = document.createElement("button");
          btn.className = "pill";
          btn.dataset.state = item.id;
          btn.textContent = item.label;
          btn.onclick = () => updateRemoteState({ state: item.id, reason: "web_console" });
          sWrap.appendChild(btn);
        }
        const mWrap = $("mood-controls");
        mWrap.innerHTML = "";
        for (const item of MOODS) {
          const btn = document.createElement("button");
          btn.className = "pill";
          btn.dataset.mood = item.id;
          btn.textContent = item.label;
          btn.onclick = () => updateRemoteState({ mood: item.id, reason: "web_console" });
          mWrap.appendChild(btn);
        }
      }

      async function updateRemoteState(patch) {
        const data = await apiJson("/api/state", {
          method: "POST",
          body: JSON.stringify(patch)
        });
        const snap = data.state && typeof data.state === "object" ? data.state : data;
        updateStateDisplay(snap.state || app.state, snap.mood || app.mood);
        showToast("已切换状态：" + (snap.state || app.state) + " / " + (snap.mood || app.mood));
      }

      async function triggerSleep(dateStr = "") {
        const body = dateStr.trim() ? { date_str: dateStr.trim() } : {};
        $("sleep-output").textContent = "正在执行睡眠归档...";
        try {
          const data = await apiJson("/sleep", {
            method: "POST",
            body: JSON.stringify(body)
          });
          if (data.state) {
            updateStateDisplay(data.state.state, data.state.mood, data.episode_id || data.new_episode_id || app.episodeId);
          } else if (data.episode_id || data.new_episode_id) {
            updateStateDisplay(null, null, data.episode_id || data.new_episode_id);
          }
          app.viewingEpisodeId = "";
          $("sleep-output").textContent = "归档完成：" + (data.status || "consolidated");
          showToast("睡眠归档完成 (/sleep)");
          loadJournals().catch(() => {});
          loadEpisodes().catch(() => {});
          loadTurns().catch(() => {});
        } catch (err) {
          $("sleep-output").textContent = "归档失败：" + err.message;
          showToast("归档失败：" + err.message);
        }
      }

      async function loadTimemap() {
        const includeDone = $("tm-include-done").checked ? "1" : "0";
        const data = await apiJson("/api/timemap?include_done=" + includeDone);
        const list = $("timemap-list");
        list.innerHTML = "";
        const schedules = Array.isArray(data.schedules) ? data.schedules : [];
        if (schedules.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "当前没有待执行的 Timemap 定时提醒。";
          list.appendChild(empty);
          return;
        }
        for (const item of schedules) {
          const row = document.createElement("div");
          row.className = "schedule-row";

          const info = document.createElement("div");
          const title = document.createElement("div");
          title.style.fontWeight = "600";
          title.textContent = "#" + item.id + " · " + item.label + (item.done ? " [已完成]" : "");
          const sub = document.createElement("div");
          sub.className = "muted mono";
          const payloadStr = item.payload && Object.keys(item.payload).length ? " · " + JSON.stringify(item.payload) : "";
          sub.textContent = (item.trigger_at || "") + payloadStr;
          info.appendChild(title);
          info.appendChild(sub);

          const delBtn = document.createElement("button");
          delBtn.className = "btn btn-sm btn-danger";
          delBtn.textContent = "删除";
          delBtn.onclick = async () => {
            try {
              await apiJson("/api/timemap/" + encodeURIComponent(String(item.id)), { method: "DELETE" });
              showToast("已删除日程 #" + item.id);
              await loadTimemap();
            } catch (err) {
              showToast("删除失败：" + err.message);
            }
          };

          row.appendChild(info);
          row.appendChild(delBtn);
          list.appendChild(row);
        }
      }

      async function addTimemap() {
        const trigger_at = $("tm-trigger").value.trim();
        const label = $("tm-label").value.trim();
        const rawPayload = $("tm-payload").value.trim();
        if (!trigger_at || !label) {
          showToast("请填写触发时间与提醒标签");
          return;
        }
        let payload = {};
        if (rawPayload) {
          try {
            payload = JSON.parse(rawPayload);
          } catch (_e) {
            showToast("附加 JSON 格式不正确");
            return;
          }
        }
        await apiJson("/api/timemap", {
          method: "POST",
          body: JSON.stringify({ trigger_at, label, payload })
        });
        $("tm-label").value = "";
        $("tm-payload").value = "";
        showToast("已添加日程：" + label);
        await loadTimemap();
      }

      /* ---------------- Tab 5: Community & Plugins ---------------- */
      async function copyText(text, label) {
        const value = String(text || "");
        if (!value) {
          showToast(label + " 为空，无法复制");
          return;
        }
        try {
          if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
            await navigator.clipboard.writeText(value);
          } else {
            const ta = document.createElement("textarea");
            ta.value = value;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
          }
          showToast("已复制 " + label);
        } catch (_err) {
          showToast("复制失败，请手动选中复制");
        }
      }

      function syncApiKeyVisibility() {
        const input = $("compat-api-key");
        const btn = $("btn-toggle-api-key");
        if (!input || !btn) return;
        input.type = app.compatKeyVisible ? "text" : "password";
        btn.textContent = app.compatKeyVisible ? "隐藏" : "显示";
      }

      function renderCompatPanel(data, errMsg = "") {
        const statusEl = $("compat-status");
        const extraEl = $("compat-extra-urls");
        const frontendsEl = $("compat-frontends");
        if (errMsg) {
          statusEl.textContent = "社区接入接口暂不可用（" + errMsg + "），不影响其余面板。";
        } else if (data) {
          statusEl.textContent = "已就绪：复制以下三件套至 Project AIRI 或 Open-LLM-VTuber 即可直连。";
          $("compat-base-url").value = data.openai_base_url || "";
          $("compat-model").value = data.model || "";
          $("compat-api-key").value = data.api_key || "";
          const extras = [];
          if (data.events_url) extras.push("SSE: " + data.events_url);
          if (data.bubble_url) extras.push("Bubble: " + data.bubble_url);
          if (typeof data.mcp_config_exists === "boolean") {
            extras.push("MCP 配置: " + (data.mcp_config_exists ? "已存在" : "未创建"));
          }
          extraEl.textContent = extras.join(" | ");
        }
        syncApiKeyVisibility();

        const list = (data && Array.isArray(data.frontends) && data.frontends.length > 0)
          ? data.frontends
          : DEFAULT_COMMUNITY_FRONTENDS;
        frontendsEl.innerHTML = "";
        list.forEach((item) => {
          const url = String(item.url || "");
          const safeUrl = (url.startsWith("https://") || url.startsWith("http://")) ? url : "";
          const el = document.createElement(safeUrl ? "a" : "span");
          el.className = "pill";
          if (safeUrl) {
            el.href = safeUrl;
            el.target = "_blank";
            el.rel = "noopener noreferrer";
            el.style.textDecoration = "none";
          }
          const providerText = item.provider ? " · " + item.provider : "";
          el.textContent = (item.name || item.id || "Frontend") + providerText;
          frontendsEl.appendChild(el);
        });
      }

      async function loadCompat() {
        try {
          const data = await apiJson("/api/compat");
          app.compat = data;
          renderCompatPanel(data, "");
        } catch (err) {
          renderCompatPanel(app.compat, err.message || "请求失败");
        }
      }

      function highlightEnginePreset(currentModel) {
        const target = String(currentModel || "").trim();
        document.querySelectorAll("#engine-presets .pill").forEach((btn) => {
          btn.classList.toggle("active", (btn.dataset.model || "") === target);
        });
      }

      function buildEnginePresetButtons() {
        const host = $("engine-presets");
        if (host) {
          host.innerHTML = "";
          ENGINE_MODEL_PRESETS.forEach((preset) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "pill mono";
            btn.dataset.model = preset.id;
            btn.textContent = preset.label;
            btn.onclick = () => {
              $("engine-model-input").value = preset.id;
              highlightEnginePreset(preset.id);
            };
            host.appendChild(btn);
          });
        }

        const routeHost = $("engine-route-controls");
        if (routeHost) {
          routeHost.innerHTML = "";
          ROUTE_MODES.forEach((item) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "pill mono";
            btn.dataset.route = item.id;
            btn.textContent = item.label;
            btn.onclick = () => applyEngineRoute(item.id);
            routeHost.appendChild(btn);
          });
        }

        const reasoningHost = $("engine-reasoning-controls");
        if (reasoningHost) {
          reasoningHost.innerHTML = "";
          REASONING_EFFORTS.forEach((item) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "pill mono";
            btn.dataset.reasoning = item.id;
            btn.textContent = item.label;
            btn.onclick = () => applyEngineReasoning(item.id);
            reasoningHost.appendChild(btn);
          });
        }
      }

      function renderEnginePanel(data, errMsg = "") {
        const badge = $("engine-badge");
        const statusEl = $("engine-status");
        const applyBtn = $("btn-engine-apply");
        const routeStatusEl = $("engine-route-status");
        const reasoningStatusEl = $("engine-reasoning-status");
        if (errMsg) {
          badge.textContent = "接口未就绪";
          statusEl.textContent = "驱动引擎状态接口暂不可用（" + errMsg + "）。";
          if (routeStatusEl) routeStatusEl.textContent = "暂不可用";
          if (reasoningStatusEl) reasoningStatusEl.textContent = "暂不可用";
          document.querySelectorAll("#engine-route-controls .pill, #engine-reasoning-controls .pill").forEach((b) => {
            b.disabled = true;
          });
          return;
        }
        if (!data) return;
        const engineName = data.engine || "unknown";
        const modelName = data.model || "";
        badge.textContent = engineName + " · " + (modelName || "(default)");
        $("engine-model-input").value = modelName;
        highlightEnginePreset(modelName);
        if (data.supports_model_switch === false) {
          applyBtn.disabled = true;
          statusEl.textContent = "当前引擎（" + engineName + "）不支持运行时模型热切。";
        } else {
          applyBtn.disabled = false;
          statusEl.textContent = "支持运行时热切：选择预设或输入自定义模型名后点击应用。";
        }

        const routeMode = String(data.route_mode || "auto").toLowerCase();
        const canRoute = data.supports_route_switch !== false;
        const brainWorkers = data.online_brain_workers;
        const routeDetails = ["当前：" + routeMode];
        if (data.last_route) routeDetails.push("上次命中：" + data.last_route);
        if (brainWorkers !== undefined && brainWorkers !== null) {
          const workerCount = Array.isArray(brainWorkers) ? brainWorkers.length : Number(brainWorkers);
          if (Number.isFinite(workerCount)) routeDetails.push("在线算力节点：" + workerCount);
        }
        if (!canRoute) {
          routeDetails.push("（当前引擎不支持运行时切换路由）");
        }
        if (routeStatusEl) routeStatusEl.textContent = routeDetails.join(" · ");
        document.querySelectorAll("#engine-route-controls .pill").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.route === routeMode);
          btn.disabled = !canRoute;
        });

        const rawEffort = String(data.reasoning_effort || "").trim().toLowerCase();
        const reasoningEffort = rawEffort || "default";
        const reasoningMode = String(data.reasoning_mode || "").trim().toLowerCase();
        const effectiveEffort = String(
          data.effective_reasoning_effort || data.episode_reasoning_effort || ""
        ).trim().toLowerCase();
        const canReasoning = data.supports_reasoning_switch !== false;
        if (reasoningStatusEl) {
          let statusText = "当前档位：" + reasoningEffort;
          if (reasoningMode === "auto" && effectiveEffort) {
            statusText = "当前档位：default（自动棘轮：" + effectiveEffort + "）";
          } else if (reasoningMode === "manual" && rawEffort) {
            statusText = "当前档位：" + rawEffort + "（手动锁定，切窗恢复自动）";
          }
          reasoningStatusEl.textContent = canReasoning
            ? statusText
            : (statusText + "（当前引擎不支持运行时切换思考深度）");
        }
        document.querySelectorAll("#engine-reasoning-controls .pill").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.reasoning === reasoningEffort);
          btn.disabled = !canReasoning;
        });
      }

      async function loadEngine() {
        try {
          const data = await apiJson("/api/engine");
          app.engine = data;
          renderEnginePanel(data, "");
        } catch (err) {
          renderEnginePanel(null, err.message || "请求失败");
        }
      }

      async function applyEngineModel() {
        const model = $("engine-model-input").value.trim();
        const btn = $("btn-engine-apply");
        btn.disabled = true;
        try {
          const res = await apiJson("/api/engine/model", {
            method: "POST",
            body: JSON.stringify({ model })
          });
          const nextModel = res.model || "";
          const prevModel = res.previous_model || "(default)";
          app.engine = {
            ...(app.engine || {}),
            engine: res.engine || (app.engine && app.engine.engine) || "unknown",
            model: nextModel,
            supports_model_switch: true
          };
          renderEnginePanel(app.engine, "");
          $("engine-status").textContent = "已切换模型：" + prevModel + " -> " + (nextModel || "(default)");
          showToast("模型已切换为 " + (nextModel || "(default)"));
          loadCompat();
        } catch (err) {
          $("engine-status").textContent = "模型热切失败：" + (err.message || "未知错误");
          showToast("模型热切失败：" + (err.message || "未知错误"));
        } finally {
          if (!app.engine || app.engine.supports_model_switch !== false) {
            btn.disabled = false;
          }
        }
      }

      async function applyEngineRoute(mode) {
        if (app.engine && app.engine.supports_route_switch === false) return;
        const buttons = document.querySelectorAll("#engine-route-controls .pill");
        buttons.forEach((b) => { b.disabled = true; });
        try {
          const res = await apiJson("/api/engine/route", {
            method: "POST",
            body: JSON.stringify({ route_mode: mode, mode })
          });
          const nextRoute = res.route_mode || res.mode || mode;
          app.engine = {
            ...(app.engine || {}),
            route_mode: nextRoute,
            last_route: res.last_route !== undefined ? res.last_route : (app.engine && app.engine.last_route),
            supports_route_switch: res.supports_route_switch !== undefined ? res.supports_route_switch : true
          };
          renderEnginePanel(app.engine, "");
          showToast("混动路由已切换为 " + nextRoute);
        } catch (err) {
          showToast("路由切换失败：" + (err.message || "未知错误"));
          renderEnginePanel(app.engine, "");
        }
      }

      async function applyEngineReasoning(effort) {
        if (app.engine && app.engine.supports_reasoning_switch === false) return;
        const buttons = document.querySelectorAll("#engine-reasoning-controls .pill");
        buttons.forEach((b) => { b.disabled = true; });
        try {
          const res = await apiJson("/api/engine/reasoning", {
            method: "POST",
            body: JSON.stringify({ reasoning_effort: effort, effort })
          });
          const nextEffort = res.reasoning_effort || res.effort || effort;
          app.engine = {
            ...(app.engine || {}),
            reasoning_effort: nextEffort,
            supports_reasoning_switch: res.supports_reasoning_switch !== undefined ? res.supports_reasoning_switch : true
          };
          renderEnginePanel(app.engine, "");
          showToast("思考深度已切换为 " + nextEffort);
        } catch (err) {
          showToast("思考深度切换失败：" + (err.message || "未知错误"));
          renderEnginePanel(app.engine, "");
        }
      }

      function renderWorkersPanel(data, errMsg = "") {
        const summaryEl = $("workers-summary");
        const listEl = $("workers-list");
        if (!summaryEl || !listEl) return;
        listEl.innerHTML = "";
        if (errMsg) {
          summaryEl.textContent = "在线节点接口暂不可用（" + errMsg + "）。";
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "等待后端启用 GET /api/workers。";
          listEl.appendChild(empty);
          return;
        }
        const workers = data && Array.isArray(data.workers) ? data.workers : [];
        const onlineCount = workers.filter((w) => w && w.online !== false && w.status !== "offline").length;
        summaryEl.textContent = "总节点: " + workers.length + " | 在线: " + onlineCount;
        if (workers.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "当前没有已注册的远程或本地算力节点。";
          listEl.appendChild(empty);
          return;
        }
        for (const w of workers) {
          const row = document.createElement("div");
          row.className = "worker-row";

          const left = document.createElement("div");
          left.style.flex = "1";
          left.style.minWidth = "0";
          const name = document.createElement("div");
          name.className = "mono";
          name.style.fontWeight = "600";
          name.textContent = String(w.name || w.worker_id || w.id || "worker");
          const detailParts = [];
          if (w.role || w.kind) detailParts.push(String(w.role || w.kind));
          if (Array.isArray(w.capabilities) && w.capabilities.length > 0) {
            detailParts.push("caps: " + w.capabilities.join(", "));
          }
          if (w.host || w.addr || w.endpoint) detailParts.push(String(w.host || w.addr || w.endpoint));
          if (w.last_seen || w.last_heartbeat || w.updated_at) {
            detailParts.push("心跳: " + String(w.last_seen || w.last_heartbeat || w.updated_at));
          }
          const sub = document.createElement("div");
          sub.className = "muted mono";
          sub.textContent = detailParts.join(" · ") || "已连接";
          left.appendChild(name);
          left.appendChild(sub);

          const statusBadge = document.createElement("span");
          statusBadge.className = "badge mono";
          const stText = w.status || (w.online === false ? "offline" : "online");
          statusBadge.textContent = String(stText);

          row.appendChild(left);
          row.appendChild(statusBadge);
          listEl.appendChild(row);
        }
      }

      async function loadWorkers() {
        try {
          const data = await apiJson("/api/workers");
          app.workers = Array.isArray(data.workers) ? data.workers : [];
          renderWorkersPanel(data, "");
        } catch (err) {
          renderWorkersPanel(null, err.message || "请求失败");
        }
      }

      function renderApprovalsPanel(data, errMsg = "") {
        const badgeEl = $("approvals-badge");
        const summaryEl = $("approvals-summary");
        const listEl = $("approvals-list");
        if (!summaryEl || !listEl) return;
        listEl.innerHTML = "";
        if (errMsg) {
          if (badgeEl) badgeEl.textContent = "-";
          summaryEl.textContent = "审批队列接口暂不可用（" + errMsg + "）。";
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "等待后端启用 GET /api/approvals。";
          listEl.appendChild(empty);
          return;
        }
        const pending = data && Array.isArray(data.pending)
          ? data.pending
          : (data && Array.isArray(data.approvals) ? data.approvals : []);
        if (badgeEl) badgeEl.textContent = pending.length + " 待审";
        summaryEl.textContent = pending.length > 0
          ? ("共有 " + pending.length + " 项待确认操作，请核对后批准或拒绝。")
          : "当前没有等待审批的敏感操作。";

        if (pending.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "待审批队列为空。";
          listEl.appendChild(empty);
          return;
        }

        for (const item of pending) {
          const approvalId = String(item.id || item.approval_id || "").trim();
          const row = document.createElement("div");
          row.className = "approval-row";

          const left = document.createElement("div");
          left.style.flex = "1";
          left.style.minWidth = "0";
          const title = document.createElement("div");
          title.style.fontWeight = "600";
          title.textContent = String(item.action || item.tool || item.title || item.kind || "待审批操作");
          const meta = document.createElement("div");
          meta.className = "muted mono";
          meta.textContent = [
            approvalId ? ("#" + approvalId) : "",
            item.source || item.worker_id || "",
            item.created_at || item.requested_at || ""
          ].filter(Boolean).join(" · ");
          const desc = document.createElement("div");
          desc.className = "muted";
          desc.style.marginTop = "4px";
          desc.style.wordBreak = "break-word";
          const detailText = item.summary || item.reason || item.description
            || (item.args ? JSON.stringify(item.args) : "")
            || (item.payload ? JSON.stringify(item.payload) : "");
          desc.textContent = detailText || "无附加详情";

          left.appendChild(title);
          if (meta.textContent) left.appendChild(meta);
          left.appendChild(desc);

          const actions = document.createElement("div");
          actions.style.display = "flex";
          actions.style.gap = "6px";
          actions.style.flexShrink = "0";

          const approveBtn = document.createElement("button");
          approveBtn.type = "button";
          approveBtn.className = "btn btn-sm btn-primary";
          approveBtn.textContent = "批准";
          approveBtn.disabled = !approvalId;
          approveBtn.onclick = () => decideApproval(approvalId, "approve", [approveBtn, rejectBtn]);

          const rejectBtn = document.createElement("button");
          rejectBtn.type = "button";
          rejectBtn.className = "btn btn-sm btn-danger";
          rejectBtn.textContent = "拒绝";
          rejectBtn.disabled = !approvalId;
          rejectBtn.onclick = () => decideApproval(approvalId, "reject", [approveBtn, rejectBtn]);

          actions.appendChild(approveBtn);
          actions.appendChild(rejectBtn);
          row.appendChild(left);
          row.appendChild(actions);
          listEl.appendChild(row);
        }
      }

      async function loadApprovals() {
        try {
          const data = await apiJson("/api/approvals");
          app.approvals = Array.isArray(data.pending) ? data.pending : [];
          renderApprovalsPanel(data, "");
        } catch (err) {
          renderApprovalsPanel(null, err.message || "请求失败");
        }
      }

      async function decideApproval(approvalId, decision, buttons = []) {
        if (!approvalId) return;
        buttons.forEach((b) => { if (b) b.disabled = true; });
        try {
          await apiJson("/api/approvals/" + encodeURIComponent(approvalId) + "/decide", {
            method: "POST",
            body: JSON.stringify({
              decision,
              approved: decision === "approve"
            })
          });
          showToast(decision === "approve" ? ("已批准审批 #" + approvalId) : ("已拒绝审批 #" + approvalId));
          await loadApprovals();
        } catch (err) {
          showToast("审批提交失败：" + (err.message || "未知错误"));
          buttons.forEach((b) => { if (b) b.disabled = false; });
        }
      }

      function renderMcpPanel(configData, probeData, errMsg = "") {
        const summaryEl = $("mcp-summary");
        const listEl = $("mcp-server-list");
        if (errMsg) {
          summaryEl.textContent = "MCP 接口暂不可用（" + errMsg + "）。";
          listEl.innerHTML = "";
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "等待后端启用 /api/mcp 与 /api/mcp/probe。";
          listEl.appendChild(empty);
          return;
        }

        const parts = [];
        if (configData) {
          parts.push("配置文件: " + (configData.config_path || "-") + (configData.config_exists ? " (存在)" : " (未找到)"));
          const mounted = Array.isArray(configData.mounted_tools) ? configData.mounted_tools : [];
          parts.push("已挂载工具: " + mounted.length);
          if (configData.config_error) parts.push("配置错误: " + configData.config_error);
        }
        if (probeData) {
          parts.push("实测握手: " + (probeData.ok ? "通过" : "存在异常") + " (" + (probeData.elapsed_ms || 0) + "ms)");
          if (probeData.config_error) parts.push("探测错误: " + probeData.config_error);
        }
        summaryEl.textContent = parts.join(" | ") || "暂无 MCP 状态数据。";

        const probeMap = new Map();
        if (probeData && Array.isArray(probeData.servers)) {
          probeData.servers.forEach((s) => probeMap.set(s.name, s));
        }

        const baseServers = (configData && Array.isArray(configData.servers)) ? configData.servers : [];
        const merged = [];
        const seen = new Set();
        baseServers.forEach((srv) => {
          seen.add(srv.name);
          merged.push({ cfg: srv, probe: probeMap.get(srv.name) || null });
        });
        probeMap.forEach((ps, name) => {
          if (!seen.has(name)) {
            merged.push({ cfg: { name, command: "", args: [], disabled: !!ps.disabled }, probe: ps });
          }
        });

        listEl.innerHTML = "";
        if (merged.length === 0) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "当前未配置任何 MCP 服务器。";
          listEl.appendChild(empty);
          return;
        }

        merged.forEach(({ cfg, probe }) => {
          const row = document.createElement("div");
          row.className = "list-item";
          row.style.display = "flex";
          row.style.flexDirection = "column";
          row.style.gap = "4px";

          const head = document.createElement("div");
          head.style.display = "flex";
          head.style.justifyContent = "space-between";
          head.style.alignItems = "center";

          const title = document.createElement("strong");
          title.className = "mono";
          title.textContent = cfg.name || "unnamed";

          const badge = document.createElement("span");
          badge.className = "badge";
          if (cfg.disabled || (probe && probe.disabled)) {
            badge.textContent = "已禁用";
          } else if (probe) {
            badge.textContent = probe.connected
              ? ("已连通 · " + (probe.tool_count || 0) + " 工具 · " + (probe.elapsed_ms || 0) + "ms")
              : ("握手失败 · " + (probe.elapsed_ms || 0) + "ms");
          } else {
            badge.textContent = "已配置（未探测）";
          }

          head.appendChild(title);
          head.appendChild(badge);
          row.appendChild(head);

          if (cfg.command) {
            const cmd = document.createElement("div");
            cmd.className = "muted mono";
            cmd.style.fontSize = "12px";
            const argsText = Array.isArray(cfg.args) ? cfg.args.join(" ") : "";
            cmd.textContent = (cfg.command + (argsText ? " " + argsText : "")).trim();
            row.appendChild(cmd);
          }

          if (probe && Array.isArray(probe.tools) && probe.tools.length > 0) {
            const toolsEl = document.createElement("div");
            toolsEl.className = "muted mono";
            toolsEl.style.fontSize = "12px";
            toolsEl.textContent = "Tools: " + probe.tools.join(", ");
            row.appendChild(toolsEl);
          }

          if (probe && probe.error) {
            const errEl = document.createElement("div");
            errEl.className = "muted";
            errEl.style.color = "var(--danger)";
            errEl.style.fontSize = "12px";
            errEl.textContent = "错误: " + probe.error;
            row.appendChild(errEl);
          }

          listEl.appendChild(row);
        });
      }

      async function loadMcp() {
        try {
          const data = await apiJson("/api/mcp");
          app.mcp = data;
          renderMcpPanel(data, app.mcpProbe, "");
        } catch (err) {
          renderMcpPanel(null, null, err.message || "请求失败");
        }
      }

      async function probeMcp() {
        const btn = $("btn-mcp-probe");
        const prevText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "探测中...";
        try {
          const data = await apiJson("/api/mcp/probe", { method: "POST", body: JSON.stringify({}) });
          app.mcpProbe = data;
          renderMcpPanel(app.mcp, data, "");
          showToast(data.ok ? "MCP 实测握手完成" : "MCP 握手检测到异常");
        } catch (err) {
          $("mcp-summary").textContent = "MCP 实测握手失败：" + (err.message || "未知错误");
          showToast("MCP 握手失败：" + (err.message || "未知错误"));
        } finally {
          btn.disabled = false;
          btn.textContent = prevText;
        }
      }

      async function sendTestBubble() {
        const text = $("bubble-test-text").value.trim();
        if (!text) {
          showToast("请输入要推送的气泡文本");
          return;
        }
        const mood = $("bubble-test-mood").value;
        const durVal = Number($("bubble-test-duration").value);
        const payload = { text };
        if (mood) payload.mood = mood;
        if (Number.isFinite(durVal) && durVal > 0) payload.duration_sec = durVal;

        const btn = $("btn-bubble-send");
        btn.disabled = true;
        try {
          await apiJson("/api/bubble", {
            method: "POST",
            body: JSON.stringify(payload)
          });
          $("bubble-test-status").textContent = "已推送外部气泡：「" + text + "」" + (mood ? "（mood=" + mood + "）" : "");
          $("bubble-test-text").value = "";
          showToast("已推送外部气泡");
        } catch (err) {
          $("bubble-test-status").textContent = "推送失败：" + (err.message || "未知错误");
          showToast("气泡推送失败：" + (err.message || "未知错误"));
        } finally {
          btn.disabled = false;
        }
      }

      async function loadCommunityPanel() {
        await Promise.allSettled([
          loadCompat(),
          loadEngine(),
          loadWorkers(),
          loadApprovals(),
          loadMcp()
        ]);
      }

      /* ---------------- Bootstrap ---------------- */
      async function initializeSession() {
        const stateSnap = await apiJson("/state");
        updateStateDisplay(stateSnap.state || "idle", stateSnap.mood || "calm", stateSnap.episode_id || "");
        connectSSE();
        await Promise.allSettled([
          loadEpisodes(),
          loadTurns(),
          loadSoulFiles(),
          loadFacts(),
          loadJournals(),
          loadTimemap(),
          loadCommunityPanel()
        ]);
      }

      function bindUI() {
        buildStateMoodButtons();
        buildEnginePresetButtons();

        document.querySelectorAll(".tab-btn").forEach((btn) => {
          btn.onclick = () => {
            const tab = btn.dataset.tab;
            document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
            document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + tab));
          };
        });

        $("btn-chat-send").onclick = () => sendChat();
        $("chat-input").addEventListener("keydown", (e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendChat();
          }
        });

        $("btn-top-new-episode").onclick = () => createNewEpisode();
        $("btn-episode-new").onclick = () => createNewEpisode();
        $("btn-episode-reload").onclick = () => loadEpisodes().then(() => showToast("已刷新会话列表"));
        $("btn-episode-toggle").onclick = () => {
          app.episodesCollapsed = !app.episodesCollapsed;
          renderEpisodesBar();
        };
        $("btn-episode-back-active").onclick = () => selectEpisode("");
        $("btn-readonly-return").onclick = () => selectEpisode("");

        document.querySelectorAll("#soul-tabs .pill").forEach((btn) => {
          btn.onclick = () => {
            app.soulFiles[app.activeSoul] = $("soul-editor").value;
            app.activeSoul = btn.dataset.soul;
            renderSoulEditor();
          };
        });
        $("soul-editor").addEventListener("input", updateSoulByteCount);
        $("btn-soul-reload").onclick = () => loadSoulFiles().then(() => showToast("已重新读取灵魂与记忆文件"));
        $("btn-soul-save").onclick = () => saveActiveSoul().catch((e) => showToast("保存失败：" + e.message));
        $("btn-facts-reload").onclick = () => loadFacts().then(() => showToast("已刷新 Facts"));
        $("btn-fact-add").onclick = () => addOrUpdateFact();
        $("fact-value-input").addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            addOrUpdateFact();
          }
        });
        $("facts-filter-input").addEventListener("input", () => renderFactsList());
        $("btn-memory-compact-preview").onclick = () => runMemoryCompaction(true);
        $("btn-memory-compact-apply").onclick = () => runMemoryCompaction(false);

        $("btn-journals-reload").onclick = () => loadJournals().then(() => showToast("已刷新日记列表"));
        $("btn-search").onclick = () => runSearch().catch((e) => showToast("检索失败：" + e.message));
        $("search-input").addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            runSearch().catch((err) => showToast("检索失败：" + err.message));
          }
        });

        $("btn-top-sleep").onclick = () => triggerSleep("");
        $("btn-sleep-run").onclick = () => triggerSleep($("sleep-date").value);

        $("btn-tm-plus5").onclick = () => {
          const dt = new Date(Date.now() + 5 * 60 * 1000);
          $("tm-trigger").value = dt.toISOString().replace(/\.\d{3}Z$/, "Z");
        };
        $("btn-tm-add").onclick = () => addTimemap().catch((e) => showToast("添加失败：" + e.message));
        $("btn-tm-reload").onclick = () => loadTimemap().catch((e) => showToast("刷新失败：" + e.message));
        $("tm-include-done").onchange = () => loadTimemap().catch(() => {});

        $("btn-compat-reload").onclick = () => loadCompat().then(() => showToast("已刷新社区壳配置"));
        $("btn-copy-base-url").onclick = () => copyText($("compat-base-url").value, "Base URL");
        $("btn-copy-model").onclick = () => copyText($("compat-model").value, "Model");
        $("btn-copy-api-key").onclick = () => copyText($("compat-api-key").value, "API Key");
        $("btn-toggle-api-key").onclick = () => {
          app.compatKeyVisible = !app.compatKeyVisible;
          syncApiKeyVisibility();
        };

        $("btn-engine-reload").onclick = () => loadEngine().then(() => showToast("已刷新引擎状态"));
        $("btn-engine-apply").onclick = () => applyEngineModel();
        $("engine-model-input").addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            applyEngineModel();
          }
        });

        $("btn-workers-reload").onclick = () => loadWorkers().then(() => showToast("已刷新在线节点"));
        $("btn-approvals-reload").onclick = () => loadApprovals().then(() => showToast("已刷新待审批队列"));

        $("btn-mcp-reload").onclick = () => loadMcp().then(() => showToast("已刷新 MCP 清单"));
        $("btn-mcp-probe").onclick = () => probeMcp();

        $("btn-bubble-send").onclick = () => sendTestBubble();
        $("bubble-test-text").addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            sendTestBubble();
          }
        });

        $("btn-clear-events").onclick = () => { $("event-feed").innerHTML = ""; };

        $("btn-lock").onclick = () => {
          sessionStorage.removeItem(TOKEN_KEY);
          app.token = "";
          app.compatKeyVisible = false;
          syncApiKeyVisibility();
          openUnlock("已手动锁定控制台。");
        };

        const submitUnlock = async () => {
          const candidate = $("unlock-token-input").value.trim();
          if (!candidate) {
            $("unlock-error").textContent = "请输入访问令牌。";
            return;
          }
          app.token = candidate;
          try {
            await apiJson("/health");
            sessionStorage.setItem(TOKEN_KEY, candidate);
            $("unlock-token-input").value = "";
            closeUnlock();
            await initializeSession();
            showToast("控制台已解锁");
          } catch (err) {
            app.token = "";
            $("unlock-error").textContent = "验证失败：" + err.message;
          }
        };

        $("btn-unlock").onclick = submitUnlock;
        $("unlock-token-input").addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submitUnlock();
          }
        });
      }

      window.addEventListener("DOMContentLoaded", async () => {
        bindUI();
        bootstrapTokenFromHash();
        if (!app.token) {
          openUnlock("");
          return;
        }
        try {
          await initializeSession();
        } catch (_err) {
          /* 401 handler opens unlock modal */
        }
      });
    })();
  </script>
</body>
</html>
"""


def render_console_html() -> str:
    """Return the static zero-dependency single-page Web Console HTML shell."""
    return _CONSOLE_HTML


__all__ = ["render_console_html"]

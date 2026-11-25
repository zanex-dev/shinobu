from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from shinobu_core.ui.web_console import render_console_html  # noqa: E402


class TestWebConsoleCompat(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = render_console_html()

    def test_community_tab_and_panel_anchors(self) -> None:
        html = self.html
        required_anchors = [
            '<button class="tab-btn" data-tab="community" role="tab">社区与插件</button>',
            '<section class="tab-panel" id="panel-community">',
            'id="compat-base-url"',
            'id="compat-model"',
            'id="compat-api-key"',
            'id="btn-toggle-api-key"',
            'id="btn-copy-base-url"',
            'id="btn-copy-model"',
            'id="btn-copy-api-key"',
            'id="compat-frontends"',
            'id="engine-badge"',
            'id="engine-presets"',
            'id="engine-model-input"',
            'id="btn-engine-apply"',
            'id="mcp-summary"',
            'id="mcp-server-list"',
            'id="btn-mcp-reload"',
            'id="btn-mcp-probe"',
            'id="bubble-test-text"',
            'id="bubble-test-mood"',
            'id="bubble-test-duration"',
            'id="btn-bubble-send"',
        ]
        for anchor in required_anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, html)

    def test_backend_api_contract_and_presets(self) -> None:
        html = self.html
        for endpoint in (
            "/api/compat",
            "/api/mcp",
            "/api/mcp/probe",
            "/api/engine",
            "/api/engine/model",
            "/api/bubble",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, html)

        for preset in (
            "(default)",
            "gemini-3.8-flash-medium",
            "claude-sonnet-5-5-high",
            "gemini-4-neon-high",
        ):
            with self.subTest(preset=preset):
                self.assertIn(preset, html)

        for frontend in ("Project AIRI", "Open-LLM-VTuber"):
            with self.subTest(frontend=frontend):
                self.assertIn(frontend, html)

        init_match = re.search(
            r"async function initializeSession\(\)\s*\{(?P<body>[\s\S]*?)\n\s*\}",
            html,
        )
        self.assertIsNotNone(init_match, "initializeSession() not found")
        assert init_match is not None
        self.assertIn("loadCommunityPanel()", init_match.group("body"))

    def test_sse_status_self_healing_and_closed_branch(self) -> None:
        html = self.html
        onerror_match = re.search(
            r"es\.onerror\s*=\s*\(\)\s*=>\s*\{(?P<body>[\s\S]*?)\};",
            html,
        )
        self.assertIsNotNone(onerror_match, "es.onerror handler not found")
        assert onerror_match is not None
        onerror_body = onerror_match.group("body")
        self.assertIn("es.readyState === 2", onerror_body)
        self.assertIn("SSE 已断开", onerror_body)
        self.assertIn("SSE 重连中", onerror_body)

        handle_match = re.search(
            r"const handleFrame\s*=\s*\(fallbackKind\)\s*=>\s*\(evt\)\s*=>\s*\{(?P<head>[\s\S]{0,200})",
            html,
        )
        self.assertIsNotNone(handle_match, "handleFrame handler not found")
        assert handle_match is not None
        self.assertIn('setSseStatus("ok", "SSE 实时在线")', handle_match.group("head"))

    def test_zero_external_cdn_or_fonts(self) -> None:
        html = self.html
        forbidden_patterns = [
            r"<script\b[^>]*\bsrc\s*=",
            r"<link\b[^>]*\bhref\s*=\s*[\"']https?://",
            r"@import\s+url\(",
            r"fonts\.googleapis\.com",
            r"fonts\.gstatic\.com",
            r"cdn\.jsdelivr\.net",
            r"unpkg\.com",
            r"cdnjs\.cloudflare\.com",
        ]
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, html, flags=re.IGNORECASE))

    def test_unique_dom_ids_and_inline_js_syntax(self) -> None:
        html = self.html
        scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
        self.assertEqual(len(scripts), 1, "Expected exactly one inline <script> block")
        script_body = scripts[0]

        html_without_script = html.split("<script>", 1)[0]
        ids = re.findall(r'\bid="([^"]+)"', html_without_script)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(duplicates, [], f"Duplicate DOM ids found: {duplicates}")

        node_bin = shutil.which("node")
        if node_bin:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".js",
                delete=False,
            ) as tmp:
                tmp.write(script_body)
                tmp_path = Path(tmp.name)
            try:
                proc = subprocess.run(
                    [node_bin, "--check", str(tmp_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"Inline JS syntax error:\n{proc.stderr}\n{proc.stdout}",
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            self.assertIn("function loadCommunityPanel()", script_body)
            self.assertIn("function applyEngineModel()", script_body)
            self.assertIn("function probeMcp()", script_body)
            self.assertIn("function sendTestBubble()", script_body)

    def test_episodes_and_safe_markdown_anchors(self) -> None:
        html = self.html
        for anchor in (
            'id="btn-top-new-episode"',
            'id="btn-top-sleep"',
            'id="episode-bar"',
            'id="episode-view-badge"',
            'id="btn-episode-back-active"',
            'id="btn-episode-new"',
            'id="btn-episode-reload"',
            'id="btn-episode-toggle"',
            'id="episode-list"',
            'id="chat-readonly-banner"',
            'id="btn-readonly-return"',
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, html)

        for token in (
            "/api/episodes?limit=15",
            "/api/episodes/new",
            "/api/turns?episode_id=",
            "function escapeHtml(",
            "function renderSafeMarkdown(",
            "function scheduleSseSessionSync(",
            "loadEpisodes()",
        ):
            with self.subTest(token=token):
                self.assertIn(token, html)

        node_bin = shutil.which("node")
        if node_bin:
            scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
            self.assertEqual(len(scripts), 1)
            fn_match = re.search(
                r"(function escapeHtml\(raw\)[\s\S]*?return htmlParts\.join\(\"\"\);\s*\})",
                scripts[0],
            )
            self.assertIsNotNone(fn_match, "Markdown helper functions not found")
            assert fn_match is not None
            probe_js = (
                fn_match.group(1)
                + "\n"
                + r"""
const xss = '<script>alert(1)<\/script> **bold** `<em>x</em>`\n- item1\n- item2\n```js\nconst a = "<img onerror=1>";\n```';
const out = renderSafeMarkdown(xss);
if (out.includes('<script>') || out.includes('<em>') || out.includes('<img')) {
  console.error('XSS_LEAK:' + out);
  process.exit(2);
}
if (!out.includes('<strong>bold</strong>') || !out.includes('<code class="md-code">&lt;em&gt;x&lt;/em&gt;</code>') || !out.includes('<ul class="md-list">') || !out.includes('<pre class="md-pre">')) {
  console.error('MD_MISSING:' + out);
  process.exit(3);
}
console.log('OK');
"""
            )
            proc = subprocess.run(
                [node_bin, "-e", probe_js],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Safe markdown check failed:\n{proc.stderr}\n{proc.stdout}",
            )

    def test_facts_crud_and_filter_anchors(self) -> None:
        html = self.html
        for anchor in (
            'id="facts-count"',
            'id="fact-key-input"',
            'id="fact-value-input"',
            'id="fact-confidence-input"',
            'id="btn-fact-add"',
            'id="facts-filter-input"',
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, html)

        for token in (
            "/api/memory/facts",
            'method: "DELETE"',
            "resp.status === 404 || resp.status === 405",
            "window.confirm(",
            "function addOrUpdateFact()",
            "function deleteFact(",
        ):
            with self.subTest(token=token):
                self.assertIn(token, html)

    def test_hybrid_route_reasoning_workers_and_approvals_anchors(self) -> None:
        html = self.html
        for anchor in (
            'id="engine-route-status"',
            'id="engine-route-controls"',
            'id="engine-reasoning-status"',
            'id="engine-reasoning-controls"',
            'id="btn-workers-reload"',
            'id="workers-summary"',
            'id="workers-list"',
            'id="approvals-badge"',
            'id="btn-approvals-reload"',
            'id="approvals-summary"',
            'id="approvals-list"',
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, html)

        for token in (
            "/api/engine/route",
            "/api/engine/reasoning",
            "supports_route_switch",
            "supports_reasoning_switch",
            "/api/workers",
            "/api/approvals",
            "/decide",
            "loadWorkers()",
            "loadApprovals()",
            '"auto"',
            '"cloud"',
            '"local"',
            '"off"',
            '"minimal"',
            '"low"',
            '"medium"',
            '"high"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, html)

    def test_memory_compaction_anchors(self) -> None:
        html = self.html
        for anchor in (
            'id="btn-memory-compact-preview"',
            'id="btn-memory-compact-apply"',
            'id="memory-compact-box"',
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, html)

        for token in (
            "/api/memory/compact",
            "function renderMemoryCompactResult(",
            "function runMemoryCompaction(",
            "dry_run",
        ):
            with self.subTest(token=token):
                self.assertIn(token, html)

    def test_tts_playback_anchors(self) -> None:
        html = self.html
        for token in (
            ".tts-btn",
            ".tts-btn.playing",
            "function stopActiveTts()",
            "async function speakText(text, btn)",
            'apiFetch("/api/tts"',
            'fmt: "mp3"',
            "URL.createObjectURL",
            "URL.revokeObjectURL",
            "new Audio(",
            "🔊 朗读",
            "⏹ 停止",
            "tts_unavailable",
        ):
            with self.subTest(token=token):
                self.assertIn(token, html)


if __name__ == "__main__":
    unittest.main()

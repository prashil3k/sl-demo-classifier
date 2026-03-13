#!/usr/bin/env python3
"""
Storylane Demo Classifier — Web Interface
==========================================
A simple browser-based UI. Open http://localhost:8000 and click Start.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8000
PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
RUBRICS_DIR = PROJECT_DIR / "rubrics"

# Store process state
state = {
    "running": False,
    "process": None,
    "log_lines": [],
    "finished": False,
    "error": None,
    "active_rubric": None,  # Path to custom rubric being used
    "api_key": "",  # Anthropic API key (set via UI)
}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(get_html().encode())

        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps({
                "running": state["running"],
                "finished": state["finished"],
                "error": state["error"],
                "log": state["log_lines"][-200:],  # Last 200 lines
                "log_count": len(state["log_lines"]),
            }).encode())

        elif self.path == "/results":
            json_path = OUTPUT_DIR / "demo_report.json"
            if json_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json_path.read_bytes())
            else:
                self.send_response(404)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "No results yet"}')

        elif self.path == "/rubric-status":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": state.get("rubric_status"),
                "error": state.get("rubric_error"),
                "rubric": state.get("rubric_text"),
                "active_rubric": state.get("active_rubric"),
            }).encode())

        elif self.path == "/default-rubric":
            criteria_path = PROJECT_DIR / "classification_criteria.txt"
            if criteria_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"rubric": criteria_path.read_text()}).encode())
            else:
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"rubric": "(built-in default)"}).encode())

        elif self.path == "/download-csv":
            csv_path = OUTPUT_DIR / "demo_report.csv"
            if csv_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=demo_report.csv")
                self.end_headers()
                self.wfile.write(csv_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/start":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode() if content_length else "{}"
            try:
                params = json.loads(body) if body else {}
            except json.JSONDecodeError:
                params = {}

            if state["running"]:
                self.send_response(400)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Already running"}')
                return

            limit = params.get("limit", 0)
            no_classify = params.get("no_classify", False)
            mode = params.get("mode", "fast")
            extra_urls = params.get("extra_urls", "")
            criteria_file = state.get("active_rubric")  # Use custom rubric if one was generated
            api_key = state.get("api_key", "")

            if not api_key and not no_classify:
                self.send_response(400)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "No API key set. Please enter your Anthropic API key first."}')
                return

            # Start the run in a background thread
            state["running"] = True
            state["finished"] = False
            state["error"] = None
            state["log_lines"] = []

            thread = threading.Thread(target=run_classifier, args=(limit, no_classify, mode, criteria_file, extra_urls, api_key), daemon=True)
            thread.start()

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "started"}')

        elif self.path == "/stop":
            if state["process"]:
                state["process"].terminate()
                state["log_lines"].append("⛔ Stopped by user — partial results have been saved")
            state["running"] = False
            state["finished"] = True
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "stopped"}')

        elif self.path == "/upload-framework":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode() if content_length else "{}"
            try:
                params = json.loads(body) if body else {}
            except json.JSONDecodeError:
                params = {}

            doc_text = params.get("doc_text", "").strip()
            if not doc_text:
                self.send_response(400)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "No document text provided"}')
                return

            # Generate rubric in a background thread
            def do_generate():
                try:
                    state["rubric_status"] = "generating"
                    state["rubric_error"] = None

                    # Import the generator from run.py
                    sys.path.insert(0, str(PROJECT_DIR))
                    from run import generate_rubric_from_doc

                    RUBRICS_DIR.mkdir(parents=True, exist_ok=True)
                    timestamp = int(time.time())
                    rubric_path = RUBRICS_DIR / f"custom_rubric_{timestamp}.txt"

                    rubric_text = generate_rubric_from_doc(doc_text, output_path=rubric_path, api_key=state.get("api_key", ""))

                    state["active_rubric"] = str(rubric_path)
                    state["rubric_text"] = rubric_text
                    state["rubric_status"] = "ready"
                except Exception as e:
                    state["rubric_error"] = str(e)
                    state["rubric_status"] = "error"

            state["rubric_status"] = "generating"
            thread = threading.Thread(target=do_generate, daemon=True)
            thread.start()

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "generating"}')

        elif self.path == "/reset-rubric":
            state["active_rubric"] = None
            state["rubric_text"] = None
            state["rubric_status"] = None
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "reset"}')

        elif self.path == "/save-api-key":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode() if content_length else "{}"
            try:
                params = json.loads(body) if body else {}
            except json.JSONDecodeError:
                params = {}

            key = params.get("api_key", "").strip()
            if not key:
                self.send_response(400)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "No API key provided"}')
                return

            state["api_key"] = key
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "saved"}')

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging


def run_classifier(limit, no_classify, mode="fast", criteria_file=None, extra_urls="", api_key=""):
    """Run the classifier script as a subprocess."""
    try:
        # Use the venv python explicitly
        venv_python = str(PROJECT_DIR / "venv" / "bin" / "python3")
        if not Path(venv_python).exists():
            venv_python = sys.executable

        cmd = [venv_python, "-u", str(PROJECT_DIR / "run.py")]  # -u for unbuffered output
        if limit:
            cmd += ["--limit", str(limit)]
        if no_classify:
            cmd += ["--no-classify"]
        if mode:
            cmd += ["--mode", mode]
        if criteria_file:
            cmd += ["--criteria-file", criteria_file]
        if extra_urls:
            cmd += ["--extra-urls", extra_urls]
        if api_key:
            cmd += ["--api-key", api_key]

        env = os.environ.copy()
        # Force unbuffered Python output so logs stream in real-time
        env["PYTHONUNBUFFERED"] = "1"

        state["log_lines"].append(f"Starting: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(PROJECT_DIR),
        )
        state["process"] = proc

        for line in proc.stdout:
            line = line.rstrip("\n")
            state["log_lines"].append(line)

        proc.wait()
        state["process"] = None

        if proc.returncode != 0:
            state["error"] = f"Process exited with code {proc.returncode}"
        state["finished"] = True

    except Exception as e:
        state["error"] = str(e)
        state["finished"] = True
    finally:
        state["running"] = False


def get_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storylane Demo Classifier</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0a1a;
    color: #e0dce8;
    min-height: 100vh;
  }
  .container { max-width: 900px; margin: 0 auto; padding: 40px 20px; }

  h1 {
    font-size: 2rem;
    background: linear-gradient(135deg, #f0a 0%, #fa0 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 8px;
  }
  .subtitle { color: #8a8494; margin-bottom: 32px; font-size: 0.95rem; }

  .panel {
    background: #1a1428;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
    border: 1px solid #2a2438;
  }
  .panel h2 { font-size: 1.1rem; margin-bottom: 16px; color: #c8c0d8; }

  .form-row {
    display: flex;
    gap: 16px;
    align-items: end;
    flex-wrap: wrap;
  }
  .form-group { display: flex; flex-direction: column; gap: 6px; }
  .form-group label { font-size: 0.85rem; color: #8a8494; }
  .form-group select, .form-group input {
    background: #0f0a1a;
    border: 1px solid #3a3448;
    color: #e0dce8;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 0.9rem;
  }

  .btn {
    padding: 10px 24px;
    border: none;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
  }
  .btn-start {
    background: linear-gradient(135deg, #f0a 0%, #fa0 100%);
    color: #0f0a1a;
  }
  .btn-start:hover { opacity: 0.9; transform: translateY(-1px); }
  .btn-start:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
  .btn-stop { background: #3a2030; color: #f88; }
  .btn-stop:hover { background: #4a2840; }
  .btn-download { background: #1a2830; color: #8fd; }
  .btn-download:hover { background: #2a3840; }
  .btn-secondary { background: #2a2438; color: #c8c0d8; }
  .btn-secondary:hover { background: #3a3448; }
  .btn-small { padding: 6px 16px; font-size: 0.85rem; }

  /* Modal overlay */
  .modal-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(4px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .modal-overlay.hidden { display: none; }
  .modal {
    background: #1a1428;
    border: 1px solid #3a2848;
    border-radius: 16px;
    padding: 36px;
    max-width: 540px;
    width: 90%;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  .modal h2 {
    font-size: 1.5rem;
    background: linear-gradient(135deg, #f0a 0%, #fa0 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 12px;
  }
  .modal p { color: #a8a0b8; font-size: 0.9rem; line-height: 1.6; margin-bottom: 16px; }
  .modal .feature-list { list-style: none; margin-bottom: 20px; }
  .modal .feature-list li {
    padding: 6px 0;
    font-size: 0.88rem;
    color: #c8c0d8;
  }
  .modal .feature-list li::before { content: "  "; margin-right: 8px; }
  .modal .divider {
    border: none;
    border-top: 1px solid #2a2438;
    margin: 20px 0;
  }
  .modal label {
    display: block;
    font-size: 0.85rem;
    color: #8a8494;
    margin-bottom: 8px;
  }
  .modal input[type="password"] {
    width: 100%;
    background: #0f0a1a;
    border: 1px solid #3a3448;
    color: #e0dce8;
    padding: 12px 14px;
    border-radius: 8px;
    font-size: 0.95rem;
    font-family: 'SF Mono', 'Fira Code', monospace;
    margin-bottom: 8px;
  }
  .modal input[type="password"]:focus { border-color: #f0a; outline: none; }
  .modal .api-link {
    font-size: 0.82rem;
    color: #8a8494;
    margin-bottom: 20px;
    display: block;
  }
  .modal .api-link a { color: #fa8; text-decoration: none; }
  .modal .api-link a:hover { text-decoration: underline; }
  .modal .btn-row { display: flex; gap: 12px; justify-content: flex-end; }
  .modal .error-msg { color: #f88; font-size: 0.82rem; margin-bottom: 8px; display: none; }

  /* API key status badge */
  .api-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 16px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.2s;
  }
  .api-badge.set { background: #1a3028; color: #8fd; }
  .api-badge.unset { background: #3a2030; color: #f88; }
  .api-badge:hover { opacity: 0.8; }

  /* Framework upload area */
  .upload-area {
    border: 2px dashed #3a3448;
    border-radius: 10px;
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.2s;
    margin-bottom: 16px;
  }
  .upload-area:hover { border-color: #f0a; background: #1f1830; }
  .upload-area.active { border-color: #8fd; background: #1a2820; }
  .upload-area p { font-size: 0.9rem; color: #8a8494; margin-bottom: 8px; }
  .upload-area .hint { font-size: 0.78rem; color: #5a5468; }

  textarea {
    width: 100%;
    min-height: 120px;
    background: #0f0a1a;
    border: 1px solid #3a3448;
    color: #e0dce8;
    padding: 12px;
    border-radius: 8px;
    font-family: inherit;
    font-size: 0.85rem;
    line-height: 1.5;
    resize: vertical;
  }

  .rubric-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 0.82rem;
    margin-bottom: 12px;
  }
  .rubric-badge.default { background: #2a2438; color: #8a8494; }
  .rubric-badge.custom { background: #1a3028; color: #8fd; }

  .rubric-preview {
    background: #0a0714;
    border-radius: 8px;
    padding: 16px;
    max-height: 250px;
    overflow-y: auto;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.6;
    color: #a8a0b8;
    white-space: pre-wrap;
    word-break: break-word;
    margin-bottom: 12px;
    border: 1px solid #2a2438;
  }

  .spinner {
    display: inline-block;
    width: 16px; height: 16px;
    border: 2px solid #3a3448;
    border-top-color: #f0a;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .log-panel {
    background: #0a0714;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 24px;
    border: 1px solid #2a2438;
    min-height: 300px;
    max-height: 500px;
    overflow-y: auto;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.82rem;
    line-height: 1.6;
  }
  .log-panel .line { white-space: pre-wrap; word-break: break-word; }
  .log-panel .line.success { color: #8fd; }
  .log-panel .line.error { color: #f88; }
  .log-panel .line.info { color: #8af; }
  .log-panel .line.warn { color: #fa8; }

  .status-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
  }
  .status-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: #3a3448;
  }
  .status-dot.running {
    background: #f0a;
    animation: pulse 1.5s infinite;
  }
  .status-dot.done { background: #8fd; }
  .status-dot.error { background: #f88; }
  .status-text { font-size: 0.9rem; color: #8a8494; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .results-panel {
    background: #1a1428;
    border-radius: 12px;
    padding: 24px;
    border: 1px solid #2a2438;
    display: none;
  }
  .results-panel.visible { display: block; }
  .results-panel h2 { font-size: 1.1rem; margin-bottom: 16px; color: #c8c0d8; }

  .results-grid { display: grid; gap: 12px; }
  .result-card {
    background: #0f0a1a;
    border-radius: 8px;
    padding: 16px;
    border: 1px solid #2a2438;
  }
  .result-card .name { font-weight: 600; font-size: 1rem; margin-bottom: 4px; }
  .result-card .type {
    font-size: 0.82rem; padding: 3px 8px;
    border-radius: 4px; display: inline-block; margin-bottom: 8px;
  }
  .type-strong { background: #1a3028; color: #8fd; }
  .type-feature-dump { background: #3a2820; color: #fa8; }
  .type-generic { background: #2a2838; color: #aaf; }
  .type-claim-heavy { background: #3a2030; color: #f8a; }
  .type-clickthrough { background: #2a2428; color: #aaa; }
  .type-other { background: #2a2438; color: #888; }

  .score-bar { display: flex; gap: 4px; align-items: center; margin-bottom: 8px; }
  .score-bar .label { font-size: 0.78rem; color: #8a8494; width: 50px; }
  .score-bar .bar {
    height: 6px; border-radius: 3px; background: #2a2438; flex: 1; overflow: hidden;
  }
  .score-bar .fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
  .score-bar .val { font-size: 0.78rem; color: #8a8494; width: 24px; text-align: right; }
  .summary-text { font-size: 0.85rem; color: #a8a0b8; line-height: 1.5; }

  /* Collapsible section */
  .collapsible-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    user-select: none;
  }
  .collapsible-header h2 { margin-bottom: 0; }
  .collapsible-header .toggle {
    font-size: 0.82rem;
    color: #5a5468;
    transition: transform 0.2s;
  }
  .collapsible-body {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.3s ease, padding 0.3s ease;
    padding-top: 0;
  }
  .collapsible-body.open {
    max-height: 800px;
    padding-top: 16px;
  }

  /* Custom URL input */
  .url-input-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 8px;
  }
  .url-input-row input {
    flex: 1;
    background: #0f0a1a;
    border: 1px solid #3a3448;
    color: #e0dce8;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 0.85rem;
  }
  .url-tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #1a2830;
    color: #8fd;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 0.8rem;
    margin: 4px 4px 4px 0;
  }
  .url-tag .remove {
    cursor: pointer;
    color: #f88;
    font-weight: bold;
    font-size: 0.9rem;
  }
  .url-tag .remove:hover { color: #faa; }
</style>
</head>
<body>

<!-- ==================== INTRO / API KEY MODAL ==================== -->
<div class="modal-overlay" id="introModal">
  <div class="modal">
    <h2>Storylane Demo Classifier</h2>
    <p>Automatically walk through customer demos on the Storylane showcase, capture screenshots, and classify them using Claude AI.</p>
    <ul class="feature-list">
      <li>Scrapes all demos from the Storylane customer showcase</li>
      <li>Walks through each demo step-by-step using a headless browser</li>
      <li>Classifies demos using Claude AI (narrative quality, storytelling, scoring)</li>
      <li>Generates CSV and JSON reports with detailed insights</li>
    </ul>
    <hr class="divider">
    <label for="apiKeyInput">Enter your Anthropic API Key to get started</label>
    <input type="password" id="apiKeyInput" placeholder="sk-ant-..." onkeydown="if(event.key==='Enter')saveApiKey()">
    <span class="api-link">
      Don't have one? <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">Get your API key from console.anthropic.com</a>
    </span>
    <div class="error-msg" id="apiKeyError"></div>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="skipApiKey()">Skip (scrape only)</button>
      <button class="btn btn-start" onclick="saveApiKey()">Save &amp; Continue</button>
    </div>
  </div>
</div>

<!-- ==================== MAIN APP ==================== -->
<div class="container">
  <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
    <h1>Storylane Demo Classifier</h1>
    <span class="api-badge unset" id="apiBadge" onclick="showApiKeyModal()">No API Key</span>
  </div>
  <p class="subtitle">Automatically walk through customer demos, capture screenshots, and classify them using AI.</p>

  <!-- SECTION 1: Run Settings (primary) -->
  <div class="panel">
    <h2>Run Settings</h2>
    <div class="form-row">
      <div class="form-group">
        <label>How many showcase demos?</label>
        <select id="limitSelect">
          <option value="5">Test run (5 demos)</option>
          <option value="10">10 demos</option>
          <option value="20">20 demos</option>
          <option value="0" selected>All demos (~48)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Classification Mode</label>
        <select id="modeSelect">
          <option value="fast" selected>Fast -- text only, Haiku (~$0.10)</option>
          <option value="smart">Smart -- Haiku + Sonnet for top demos (~$1-2)</option>
          <option value="full">Full -- screenshots + Sonnet (~$5-8)</option>
          <option value="none">Skip classification (free)</option>
        </select>
      </div>
      <button class="btn btn-start" id="startBtn" onclick="startRun()">Start</button>
      <button class="btn btn-stop" id="stopBtn" onclick="stopRun()" style="display:none">Stop</button>
    </div>

    <!-- Custom URLs -->
    <div style="margin-top:20px; padding-top:16px; border-top:1px solid #2a2438;">
      <label style="font-size:0.85rem; color:#8a8494; display:block; margin-bottom:6px;">Add custom demo URLs <span style="color:#5a5468;">(optional -- these run in addition to the showcase demos)</span></label>
      <div class="url-input-row">
        <input type="text" id="customUrlInput" placeholder="Paste a demo URL and press Enter (e.g. https://app.storylane.io/demo/...)" onkeydown="if(event.key==='Enter')addCustomUrl()">
        <button class="btn btn-secondary btn-small" onclick="addCustomUrl()">Add</button>
      </div>
      <div id="customUrlTags" style="margin-top:8px;"></div>
    </div>
  </div>

  <div class="status-bar">
    <div class="status-dot" id="statusDot"></div>
    <span class="status-text" id="statusText">Ready to start</span>
  </div>

  <div class="log-panel" id="logPanel">
    <div class="line info">Click "Start" above to begin processing demos.</div>
  </div>

  <div style="margin-bottom: 24px; display: flex; gap: 12px;">
    <button class="btn btn-download" id="downloadBtn" onclick="downloadCSV()" style="display:none">
      Download CSV Report
    </button>
  </div>

  <div class="results-panel" id="resultsPanel">
    <h2>Results</h2>
    <div class="results-grid" id="resultsGrid"></div>
  </div>

  <!-- SECTION: Custom Framework (collapsible, at the bottom) -->
  <div class="panel" id="frameworkPanel">
    <div class="collapsible-header" onclick="toggleFramework()">
      <h2 style="display:flex; align-items:center; gap:10px;">
        Custom Classification Framework
        <span class="rubric-badge default" id="rubricBadge" style="margin:0;">Default</span>
      </h2>
      <span class="toggle" id="frameworkToggle">&#9660; Expand</span>
    </div>
    <div class="collapsible-body" id="frameworkBody">
      <p style="font-size:0.85rem; color:#5a5468; margin-bottom:12px;">
        By default, demos are classified using the Logic / Emotion / Credibility storytelling framework. If you want to use a different framework, paste your document below and generate a custom rubric.
      </p>
      <div id="uploadSection">
        <textarea id="frameworkText" placeholder="Paste your classification framework document here...&#10;&#10;Example: Describe how you want demos evaluated -- what makes a good demo? What are the categories? What should be scored?"></textarea>
        <div style="display:flex; gap:12px; align-items:center; margin-top:12px;">
          <button class="btn btn-secondary" onclick="generateRubric()">Generate Custom Rubric</button>
          <span id="rubricSpinner" style="display:none"><span class="spinner"></span> Generating with Sonnet...</span>
          <button class="btn btn-secondary btn-small" onclick="viewCurrentRubric()" style="margin-left:auto">View Current Rubric</button>
        </div>
      </div>

      <div id="rubricPreviewSection" style="display:none; margin-top:16px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <span style="font-size:0.88rem; font-weight:600; color:#c8c0d8;">Generated Rubric Preview</span>
          <div style="display:flex; gap:8px;">
            <button class="btn btn-secondary btn-small" onclick="useRubric()">Use This Rubric</button>
            <button class="btn btn-secondary btn-small" onclick="resetRubric()">Reset to Default</button>
          </div>
        </div>
        <div class="rubric-preview" id="rubricPreview"></div>
      </div>
    </div>
  </div>
</div>

<script>
let pollInterval = null;
let lastLogCount = 0;
let customRubricActive = false;
let customUrls = [];
let apiKeySet = false;

// --- API Key functions ---

function showApiKeyModal() {
  document.getElementById('introModal').classList.remove('hidden');
  document.getElementById('apiKeyInput').focus();
}

function saveApiKey() {
  const key = document.getElementById('apiKeyInput').value.trim();
  if (!key) {
    const errEl = document.getElementById('apiKeyError');
    errEl.textContent = 'Please enter your API key.';
    errEl.style.display = 'block';
    return;
  }
  if (!key.startsWith('sk-ant-')) {
    const errEl = document.getElementById('apiKeyError');
    errEl.textContent = 'API key should start with "sk-ant-". Please check and try again.';
    errEl.style.display = 'block';
    return;
  }

  fetch('/save-api-key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ api_key: key })
  })
  .then(r => r.json())
  .then(data => {
    if (data.status === 'saved') {
      apiKeySet = true;
      document.getElementById('introModal').classList.add('hidden');
      updateApiBadge();
    }
  })
  .catch(() => {
    const errEl = document.getElementById('apiKeyError');
    errEl.textContent = 'Failed to save key. Is the server running?';
    errEl.style.display = 'block';
  });
}

function skipApiKey() {
  apiKeySet = false;
  document.getElementById('introModal').classList.add('hidden');
  document.getElementById('modeSelect').value = 'none';
  updateApiBadge();
}

function updateApiBadge() {
  const badge = document.getElementById('apiBadge');
  if (apiKeySet) {
    badge.className = 'api-badge set';
    badge.textContent = 'API Key Set';
  } else {
    badge.className = 'api-badge unset';
    badge.textContent = 'No API Key';
  }
}

// --- Custom URL functions ---

function addCustomUrl() {
  const input = document.getElementById('customUrlInput');
  const url = input.value.trim();
  if (!url) return;
  if (!url.startsWith('http')) {
    alert('Please enter a valid URL starting with http:// or https://');
    return;
  }
  customUrls.push(url);
  input.value = '';
  renderUrlTags();
}

function removeCustomUrl(index) {
  customUrls.splice(index, 1);
  renderUrlTags();
}

function renderUrlTags() {
  const container = document.getElementById('customUrlTags');
  if (customUrls.length === 0) {
    container.innerHTML = '';
    return;
  }
  container.innerHTML = customUrls.map((url, i) => {
    const short = url.length > 60 ? url.substring(0, 57) + '...' : url;
    return '<span class="url-tag">' + short + ' <span class="remove" onclick="removeCustomUrl(' + i + ')">x</span></span>';
  }).join('');
}

// --- Collapsible framework section ---

function toggleFramework() {
  const body = document.getElementById('frameworkBody');
  const toggle = document.getElementById('frameworkToggle');
  if (body.classList.contains('open')) {
    body.classList.remove('open');
    toggle.innerHTML = '&#9660; Expand';
  } else {
    body.classList.add('open');
    toggle.innerHTML = '&#9650; Collapse';
  }
}

// --- Framework / Rubric functions ---

function generateRubric() {
  const docText = document.getElementById('frameworkText').value.trim();
  if (!docText) {
    alert('Please paste your framework document first.');
    return;
  }

  document.getElementById('rubricSpinner').style.display = 'inline-flex';
  document.getElementById('rubricPreviewSection').style.display = 'none';

  fetch('/upload-framework', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ doc_text: docText })
  });

  // Poll for rubric generation
  const rubricPoll = setInterval(() => {
    fetch('/rubric-status')
      .then(r => r.json())
      .then(data => {
        if (data.status === 'ready') {
          clearInterval(rubricPoll);
          document.getElementById('rubricSpinner').style.display = 'none';
          document.getElementById('rubricPreview').textContent = data.rubric;
          document.getElementById('rubricPreviewSection').style.display = 'block';
        } else if (data.status === 'error') {
          clearInterval(rubricPoll);
          document.getElementById('rubricSpinner').style.display = 'none';
          alert('Error generating rubric: ' + (data.error || 'Unknown error'));
        }
      })
      .catch(() => {});
  }, 2000);
}

function useRubric() {
  customRubricActive = true;
  document.getElementById('rubricBadge').className = 'rubric-badge custom';
  document.getElementById('rubricBadge').textContent = 'Custom';
  document.getElementById('rubricPreviewSection').style.display = 'none';
}

function resetRubric() {
  fetch('/reset-rubric', { method: 'POST' });
  customRubricActive = false;
  document.getElementById('rubricBadge').className = 'rubric-badge default';
  document.getElementById('rubricBadge').textContent = 'Default';
  document.getElementById('rubricPreviewSection').style.display = 'none';
}

function viewCurrentRubric() {
  fetch('/rubric-status')
    .then(r => r.json())
    .then(data => {
      if (data.rubric) {
        document.getElementById('rubricPreview').textContent = data.rubric;
        document.getElementById('rubricPreviewSection').style.display = 'block';
      } else {
        // Show default
        fetch('/default-rubric')
          .then(r => r.json())
          .then(d => {
            document.getElementById('rubricPreview').textContent = d.rubric;
            document.getElementById('rubricPreviewSection').style.display = 'block';
          });
      }
    });
}

// --- Run functions ---

function startRun() {
  const limit = parseInt(document.getElementById('limitSelect').value);
  const mode = document.getElementById('modeSelect').value;
  const noClassify = mode === 'none';

  // Check API key for classification modes
  if (!noClassify && !apiKeySet) {
    showApiKeyModal();
    return;
  }

  document.getElementById('startBtn').style.display = 'none';
  document.getElementById('stopBtn').style.display = 'inline-block';
  document.getElementById('downloadBtn').style.display = 'none';
  document.getElementById('resultsPanel').classList.remove('visible');
  document.getElementById('logPanel').innerHTML = '';
  document.getElementById('statusDot').className = 'status-dot running';
  document.getElementById('statusText').textContent = 'Starting...';
  lastLogCount = 0;

  const extraUrls = customUrls.join(',');

  fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ limit: limit || 0, no_classify: noClassify, mode: noClassify ? 'fast' : mode, extra_urls: extraUrls })
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      document.getElementById('statusDot').className = 'status-dot error';
      document.getElementById('statusText').textContent = data.error;
      document.getElementById('startBtn').style.display = 'inline-block';
      document.getElementById('stopBtn').style.display = 'none';
      if (data.error.includes('API key')) showApiKeyModal();
      return;
    }
    pollInterval = setInterval(pollStatus, 1500);
  });
}

function stopRun() {
  fetch('/stop', { method: 'POST' });
  document.getElementById('statusDot').className = 'status-dot error';
  document.getElementById('statusText').textContent = 'Stopped -- partial results saved';
  document.getElementById('startBtn').style.display = 'inline-block';
  document.getElementById('stopBtn').style.display = 'none';
  if (pollInterval) clearInterval(pollInterval);

  // Show download button and load whatever results exist
  document.getElementById('downloadBtn').style.display = 'inline-block';
  loadResults();
}

function pollStatus() {
  fetch('/status')
    .then(r => r.json())
    .then(data => {
      if (data.log_count > lastLogCount) {
        const panel = document.getElementById('logPanel');
        const newLines = data.log.slice(lastLogCount);
        for (const line of newLines) {
          const div = document.createElement('div');
          div.className = 'line ' + getLineClass(line);
          div.textContent = line;
          panel.appendChild(div);
        }
        panel.scrollTop = panel.scrollHeight;
        lastLogCount = data.log_count;
      }

      if (data.running) {
        document.getElementById('statusDot').className = 'status-dot running';
        document.getElementById('statusText').textContent = 'Running... (' + data.log_count + ' lines)';
      }

      if (data.finished && !data.running) {
        clearInterval(pollInterval);
        document.getElementById('startBtn').style.display = 'inline-block';
        document.getElementById('stopBtn').style.display = 'none';

        if (data.error) {
          document.getElementById('statusDot').className = 'status-dot error';
          document.getElementById('statusText').textContent = 'Error: ' + data.error;
        } else {
          document.getElementById('statusDot').className = 'status-dot done';
          document.getElementById('statusText').textContent = 'Done!';
        }
        // Always show download + results (partial or complete)
        document.getElementById('downloadBtn').style.display = 'inline-block';
        loadResults();
      }
    })
    .catch(() => {});
}

function getLineClass(line) {
  if (line.includes('Done') || line.includes('Saving progress')) return 'success';
  if (line.includes('Error') || line.includes('Stopped')) return 'error';
  if (line.includes('warning') || line.includes('unavailable')) return 'warn';
  return '';
}

function downloadCSV() { window.location.href = '/download-csv'; }

function loadResults() {
  fetch('/results')
    .then(r => r.json())
    .then(data => {
      if (data.error) return;
      const grid = document.getElementById('resultsGrid');
      grid.innerHTML = '';

      // Show ALL demos (classified or not) so partial results are visible
      const allDemos = data.sort((a, b) => {
        const scoreA = (a.classification && a.classification.overall_score) || 0;
        const scoreB = (b.classification && b.classification.overall_score) || 0;
        return scoreB - scoreA;
      });

      for (const demo of allDemos) {
        const cls = demo.classification || {};
        const typeClass = getTypeClass(cls.type);
        const card = document.createElement('div');
        card.className = 'result-card';

        let inner = '<div class="name">' + escapeHtml(demo.name) + '</div>';
        if (cls.type) {
          inner += '<span class="type ' + typeClass + '">' + escapeHtml(cls.type) + '</span>';
        } else if (demo.steps_captured > 0) {
          inner += '<span class="type type-other">Scraped (not classified)</span>';
        } else {
          inner += '<span class="type type-other">Discovered</span>';
        }

        if (demo.steps_captured > 0) {
          inner += '<div style="font-size:0.78rem; color:#5a5468; margin-bottom:6px;">' + demo.steps_captured + ' steps captured</div>';
        }

        inner += makeScoreBar('Logic', cls.logic_score);
        inner += makeScoreBar('Emotion', cls.emotion_score);
        inner += makeScoreBar('Credib.', cls.credibility_score);

        if (cls.summary) {
          inner += '<div class="summary-text">' + escapeHtml(cls.summary) + '</div>';
        }

        card.innerHTML = inner;
        grid.appendChild(card);
      }
      if (allDemos.length > 0) {
        document.getElementById('resultsPanel').classList.add('visible');
      }
    })
    .catch(() => {});
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function getTypeClass(type) {
  if (!type) return 'type-other';
  const t = type.toLowerCase();
  if (t.includes('strong') || t.includes('good')) return 'type-strong';
  if (t.includes('feature dump') || t.includes('dump') || t.includes('walkthrough')) return 'type-feature-dump';
  if (t.includes('generic') || t.includes('needs improvement')) return 'type-generic';
  if (t.includes('claim')) return 'type-claim-heavy';
  if (t.includes('click')) return 'type-clickthrough';
  return 'type-other';
}

function makeScoreBar(label, score) {
  if (!score) return '';
  const pct = score * 10;
  const color = score >= 7 ? '#8fd' : score >= 4 ? '#fa8' : '#f88';
  return '<div class="score-bar">' +
    '<span class="label">' + label + '</span>' +
    '<div class="bar"><div class="fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
    '<span class="val">' + score + '</span></div>';
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    # Accept API key from env var if present (will be overridden by UI entry)
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        state["api_key"] = env_key

    print(f"🎬 Storylane Demo Classifier")
    print(f"   Open http://localhost:{PORT} in your browser")
    print(f"   Press Ctrl+C to stop the server")
    print()

    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}")

    server = HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        server.server_close()

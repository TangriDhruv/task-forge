"""The web dashboard: the single primary interface for this system
(CLAUDE.md "Architecture: single process, one web dashboard app"). Replaces
the standalone CLI (`cua.agent.cli`) and the read-only live viewer
(`cua.agent.viewer`) for interactive use -- this app can do everything those
did, plus let a human actually take over a live, paused session
(assignment.md §3.6), which a read-only poller never could.

Single Flask process (CLAUDE.md: single process, no queue): each discovery
run executes on its own background `threading.Thread`; this app's routes
poll and relay into that thread's `EscalationBridge` concurrently, via the
same in-memory `RUNS` registry -- no second process, no IPC.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request

from cua.agent.loop import DEFAULT_MAX_STEPS, RunResult, run_discovery
from cua.escalation import credentials
from cua.escalation.bridge import EscalationBridge

DEFAULT_TARGET = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5055")
DEFAULT_USERNAME = os.environ.get("TARGET_APP_USERNAME", "demo")
DEFAULT_PASSWORD = os.environ.get("TARGET_APP_PASSWORD", "demo")
EVIDENCE_ROOT = "evidence"


@dataclass
class RunState:
    run_id: str
    goal: str
    target: str
    output_dir: str
    bridge: EscalationBridge | None = None
    result: RunResult | None = None
    error: str | None = None


# Single-process, in-memory run registry -- fine for this system's scope
# (CLAUDE.md: no queues/clusters). Keyed by run_id, read/written from the
# worker thread and from Flask's request threads.
RUNS: dict[str, RunState] = {}


def _trace_dict(run: RunState) -> dict:
    trace_path = os.path.join(run.output_dir, "trace.json")
    if not os.path.exists(trace_path):
        return {"status": "starting", "steps": []}
    with open(trace_path) as f:
        return json.load(f)


def _result_dict(result: RunResult | None) -> dict | None:
    if result is None:
        return None
    return {"status": result.status, "outputs": result.outputs, "summary": result.summary}


def start_run(goal: str, target: str, start_path: str, username: str, password: str) -> str:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    output_dir = os.path.join(EVIDENCE_ROOT, f"discovery-run-{run_id}")
    os.makedirs(output_dir, exist_ok=True)
    run = RunState(run_id=run_id, goal=goal, target=target, output_dir=output_dir)
    RUNS[run_id] = run

    def on_bridge_ready(bridge: EscalationBridge) -> None:
        run.bridge = bridge

    def worker() -> None:
        try:
            run.result = run_discovery(
                goal=goal,
                base_url=target,
                start_path=start_path,
                username=username,
                password=password,
                max_steps=DEFAULT_MAX_STEPS,
                headless=True,
                output_dir=output_dir,
                interactive=True,
                on_bridge_ready=on_bridge_ready,
            )
        except Exception as exc:  # noqa: BLE001 -- surface to the dashboard, don't die silently
            run.error = str(exc)

    threading.Thread(target=worker, daemon=True).start()
    return run_id


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        runs = sorted(RUNS.values(), key=lambda r: r.run_id, reverse=True)[:15]
        rows = "".join(
            f'<tr><td><a href="/runs/{r.run_id}">{r.run_id}</a></td>'
            f'<td>{_escape(r.goal)}</td><td>{_status_of(r)}</td></tr>'
            for r in runs
        )
        return INDEX_PAGE.format(
            target=DEFAULT_TARGET, username=DEFAULT_USERNAME, rows=rows or "<tr><td colspan=3><i>No runs yet.</i></td></tr>"
        )

    @app.route("/runs", methods=["POST"])
    def create_run():
        goal = (request.form.get("goal") or "").strip()
        target = (request.form.get("target") or DEFAULT_TARGET).strip()
        start_path = (request.form.get("start_path") or "/search").strip()
        username = (request.form.get("username") or DEFAULT_USERNAME).strip()
        password = (request.form.get("password") or DEFAULT_PASSWORD).strip()
        if not goal:
            return "Goal is required", 400
        run_id = start_run(goal, target, start_path, username, password)
        return Response(status=302, headers={"Location": f"/runs/{run_id}"})

    @app.route("/runs/<run_id>")
    def run_page(run_id):
        run = RUNS.get(run_id)
        if run is None:
            return "Run not found", 404
        return RUN_PAGE.format(run_id=run_id, goal=_escape(run.goal))

    @app.route("/runs/<run_id>/state")
    def run_state(run_id):
        run = RUNS.get(run_id)
        if run is None:
            return jsonify(error="not found"), 404
        trace = _trace_dict(run)
        bridge = run.bridge
        pending = None
        if bridge is not None and bridge.pending is not None:
            p = bridge.pending
            pending = {
                "kind": p.kind,
                "reason": p.reason,
                "step": p.step,
                "field_ref": p.field_ref,
                "credential_name": p.credential_name,
            }
        # A failure before the first step (e.g. a bad API key) never writes a
        # trace file at all, so `trace["status"]` would otherwise sit at its
        # "starting" default forever with the real reason only visible in
        # `error` -- surface it as the status itself instead.
        status = "error" if run.error else trace.get("status", "running")
        return jsonify(
            goal=run.goal,
            status=status,
            stuck_reason=trace.get("stuck_reason"),
            steps=trace.get("steps", []),
            control=bridge.control if bridge else "agent",
            pending=pending,
            viewport=bridge.viewport_size if bridge else {"width": 1280, "height": 720},
            result=_result_dict(run.result),
            error=run.error,
        )

    @app.route("/runs/<run_id>/screenshot")
    def run_screenshot(run_id):
        run = RUNS.get(run_id)
        if run is None:
            return "", 404
        if run.bridge is not None:
            try:
                return Response(run.bridge.screenshot_bytes(), mimetype="image/png")
            except Exception:  # noqa: BLE001 -- page may already be closed; fall through to a file
                pass
        shots = sorted(glob.glob(os.path.join(run.output_dir, "screenshots", "step_*.png")))
        if shots:
            with open(shots[-1], "rb") as f:
                return Response(f.read(), mimetype="image/png")
        return "", 404

    @app.route("/runs/<run_id>/relay/click", methods=["POST"])
    def relay_click(run_id):
        run = RUNS.get(run_id)
        if run is None or run.bridge is None:
            return jsonify(error="run not ready"), 400
        data = request.get_json(force=True) or {}
        try:
            run.bridge.relay_click(float(data.get("x", 0)), float(data.get("y", 0)))
        except PermissionError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True)

    @app.route("/runs/<run_id>/relay/type", methods=["POST"])
    def relay_type(run_id):
        run = RUNS.get(run_id)
        if run is None or run.bridge is None:
            return jsonify(error="run not ready"), 400
        data = request.get_json(force=True) or {}
        text = data.get("text", "")
        sensitive = bool(data.get("sensitive"))
        save_as = (data.get("save_as") or "").strip()
        try:
            if save_as:
                credentials.save_credential(save_as, text)
                sensitive = True  # anything worth naming and saving is sensitive by definition
            run.bridge.relay_type(text, sensitive=sensitive)
        except PermissionError as exc:
            return jsonify(error=str(exc)), 409
        except credentials.CredentialStoreError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, saved=bool(save_as))

    @app.route("/runs/<run_id>/relay/key", methods=["POST"])
    def relay_key(run_id):
        run = RUNS.get(run_id)
        if run is None or run.bridge is None:
            return jsonify(error="run not ready"), 400
        data = request.get_json(force=True) or {}
        try:
            run.bridge.relay_key(data.get("key", "Enter"))
        except PermissionError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True)

    @app.route("/runs/<run_id>/resume", methods=["POST"])
    def run_resume(run_id):
        run = RUNS.get(run_id)
        if run is None or run.bridge is None:
            return jsonify(error="run not ready"), 400
        run.bridge.resume()
        return jsonify(ok=True)

    return app


def _status_of(run: RunState) -> str:
    if run.error:
        return "error"
    if run.result:
        return run.result.status
    return "running"


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


INDEX_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>CUA Dashboard</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 0; background: #1e1e1e; color: #ddd; }}
  header {{ padding: 14px 20px; background: #252526; border-bottom: 1px solid #3a3a3a; }}
  header h1 {{ font-size: 16px; margin: 0; color: #fff; }}
  main {{ max-width: 760px; margin: 24px auto; padding: 0 16px; }}
  form {{ background: #252526; border: 1px solid #3a3a3a; border-radius: 6px; padding: 16px; margin-bottom: 24px; }}
  label {{ display: block; font-size: 12px; color: #9a9a9a; margin: 10px 0 4px; }}
  input, textarea {{ width: 100%; box-sizing: border-box; background: #1e1e1e; color: #eee; border: 1px solid #444; border-radius: 4px; padding: 7px 9px; font-size: 13px; }}
  textarea {{ min-height: 56px; font-family: inherit; }}
  button {{ margin-top: 14px; background: #3b5f8a; color: #fff; border: none; border-radius: 4px; padding: 9px 16px; font-size: 13px; cursor: pointer; }}
  button:hover {{ background: #4a74a8; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #333; }}
  a {{ color: #7fc8ff; }}
</style>
</head>
<body>
<header><h1>Computer-Use Automation — Dashboard</h1></header>
<main>
  <form method="post" action="/runs">
    <label>Goal</label>
    <textarea name="goal" placeholder="Search for member M-10001, view their accounts, and transfer $50 from checking to savings." required></textarea>
    <label>Target base URL</label>
    <input name="target" value="{target}">
    <label>Start path</label>
    <input name="start_path" value="/search">
    <label>Username</label>
    <input name="username" value="{username}">
    <label>Password</label>
    <input name="password" type="password" placeholder="demo">
    <button type="submit">Run discovery</button>
  </form>
  <h3>Recent runs</h3>
  <table>{rows}</table>
</main>
</body>
</html>
"""

RUN_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Run {run_id}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 0; background: #1e1e1e; color: #ddd; }}
  header {{ padding: 10px 16px; background: #252526; border-bottom: 1px solid #3a3a3a; }}
  header h1 {{ font-size: 15px; margin: 0 0 4px 0; color: #fff; }}
  header .goal {{ font-size: 12px; color: #9a9a9a; }}
  #status {{ display: inline-block; margin-left: 10px; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; }}
  .status-running, .status-starting, .status-paused_for_human {{ background: #3b5f8a; color: #cfe3ff; }}
  .status-success {{ background: #2e5d34; color: #c9f7cf; }}
  .status-stuck, .status-policy_violation, .status-max_steps, .status-error {{ background: #6b3b2e; color: #ffd7c9; }}
  main {{ display: flex; height: calc(100vh - 54px); }}
  #shot-pane {{ flex: 3; display: flex; flex-direction: column; align-items: center; padding: 16px; overflow: auto; }}
  #browser-chrome {{ position: relative; border: 1px solid #444; border-radius: 6px; overflow: hidden; background: #fff; max-width: 100%; box-shadow: 0 4px 20px rgba(0,0,0,.4); }}
  #shot {{ display: block; max-width: 900px; width: 100%; cursor: default; }}
  #shot.human-control {{ cursor: crosshair; outline: 3px solid #d98f3b; }}
  #escalation {{ display: none; background: #3a2e1e; border: 1px solid #d98f3b; border-radius: 6px; padding: 12px 14px; margin-top: 12px; max-width: 900px; width: 100%; font-size: 13px; }}
  #escalation h4 {{ margin: 0 0 6px; color: #ffcf8a; font-size: 13px; }}
  #escalation .row {{ display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }}
  #escalation input[type=text] {{ flex: 1; min-width: 160px; background: #1e1e1e; color: #eee; border: 1px solid #555; border-radius: 4px; padding: 6px 8px; }}
  #escalation button {{ background: #d98f3b; color: #1e1e1e; border: none; border-radius: 4px; padding: 6px 12px; cursor: pointer; font-size: 12px; }}
  #escalation label {{ font-size: 11px; color: #ccc; display: flex; gap: 4px; align-items: center; }}
  #log-pane {{ flex: 2; border-left: 1px solid #3a3a3a; overflow-y: auto; padding: 8px; }}
  .step {{ border-bottom: 1px solid #333; padding: 8px 6px; font-size: 12px; }}
  .step.human {{ background: #2a2518; }}
  .step .n {{ color: #888; font-weight: 600; }}
  .step .tool {{ color: #7fc8ff; font-weight: 600; }}
  .step.human .tool {{ color: #ffcf8a; }}
  .step .reasoning {{ color: #bbb; margin-top: 4px; font-style: italic; }}
  .step .error {{ color: #ff9a8a; margin-top: 4px; }}
  .step .input {{ color: #999; margin-top: 2px; font-family: monospace; font-size: 11px; word-break: break-all; }}
  #result {{ display: none; background: #1f2a1f; border: 1px solid #2e5d34; border-radius: 6px; padding: 10px 14px; margin-top: 12px; max-width: 900px; width: 100%; font-size: 12px; }}
</style>
</head>
<body>
<header>
  <h1>Run {run_id} <span id="status" class="status-starting">starting...</span></h1>
  <div class="goal">GOAL: {goal}</div>
</header>
<main>
  <div id="shot-pane">
    <div id="browser-chrome"><img id="shot" src="" alt="waiting for first screenshot..."></div>
    <div id="escalation">
      <h4 id="esc-reason"></h4>
      <div class="row">
        <input type="text" id="esc-text" placeholder="Type the value to send (e.g. a password)">
        <button onclick="sendText()">Send text</button>
        <button onclick="sendKey('Enter')">Press Enter</button>
      </div>
      <div class="row">
        <label><input type="checkbox" id="esc-sensitive"> sensitive (don't log the value)</label>
        <label>Save as credential: <input type="text" id="esc-save-as" placeholder="e.g. target_app_admin_password" style="flex:0 0 220px"></label>
      </div>
      <div class="row">
        <button onclick="resumeAgent()">Done — resume agent</button>
        <span style="color:#998">Click directly on the screenshot to relay a click into the live page.</span>
      </div>
    </div>
    <div id="result"></div>
  </div>
  <div id="log-pane"><div id="empty">Waiting for steps...</div></div>
</main>
<script>
const runId = "{run_id}";
let lastStepCount = -1;
let viewport = {{width: 1280, height: 720}};

async function poll() {{
  try {{
    const res = await fetch(`/runs/${{runId}}/state`, {{cache: 'no-store'}});
    if (!res.ok) return;
    const data = await res.json();
    viewport = data.viewport || viewport;

    const statusEl = document.getElementById('status');
    const detail = data.error || data.stuck_reason;
    statusEl.textContent = data.status + (detail ? ': ' + detail : '');
    statusEl.className = 'status-' + data.status;

    document.getElementById('shot').src = `/runs/${{runId}}/screenshot?t=${{Date.now()}}`;

    const shotEl = document.getElementById('shot');
    const escEl = document.getElementById('escalation');
    if (data.control === 'human' && data.pending) {{
      shotEl.classList.add('human-control');
      escEl.style.display = 'block';
      document.getElementById('esc-reason').textContent =
        `Agent needs help (${{data.pending.kind}}): ${{data.pending.reason}}`;
    }} else {{
      shotEl.classList.remove('human-control');
      escEl.style.display = 'none';
    }}

    if (data.result) {{
      const r = document.getElementById('result');
      r.style.display = 'block';
      r.innerHTML = `<b>${{data.result.status.toUpperCase()}}</b> — ${{data.result.summary || ''}}` +
        (data.result.outputs ? `<pre>${{JSON.stringify(data.result.outputs, null, 2)}}</pre>` : '');
    }}

    const steps = data.steps || [];
    if (steps.length !== lastStepCount) {{
      const logPane = document.getElementById('log-pane');
      logPane.innerHTML = '';
      for (const s of steps) {{
        const div = document.createElement('div');
        div.className = 'step' + (s.actor === 'human' ? ' human' : '');
        let html = `<span class="n">#${{s.step}}</span> <span class="tool">${{s.actor === 'human' ? 'HUMAN: ' : ''}}${{s.tool_name}}</span>`;
        html += `<div class="input">${{JSON.stringify(s.tool_input)}}</div>`;
        if (s.assistant_text) html += `<div class="reasoning">${{escapeHtml(s.assistant_text)}}</div>`;
        if (s.action_error) html += `<div class="error">error: ${{escapeHtml(s.action_error)}}</div>`;
        div.innerHTML = html;
        logPane.appendChild(div);
      }}
      logPane.scrollTop = logPane.scrollHeight;
      lastStepCount = steps.length;
    }}
  }} catch (e) {{ /* transient read race -- just retry */ }}
}}

document.getElementById('shot').addEventListener('click', (e) => {{
  if (!e.target.classList.contains('human-control')) return;
  const rect = e.target.getBoundingClientRect();
  const scaleX = viewport.width / rect.width;
  const scaleY = viewport.height / rect.height;
  const x = (e.clientX - rect.left) * scaleX;
  const y = (e.clientY - rect.top) * scaleY;
  fetch(`/runs/${{runId}}/relay/click`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{x, y}})
  }});
}});

function sendText() {{
  const text = document.getElementById('esc-text').value;
  const sensitive = document.getElementById('esc-sensitive').checked;
  const save_as = document.getElementById('esc-save-as').value;
  fetch(`/runs/${{runId}}/relay/type`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{text, sensitive, save_as}})
  }});
  document.getElementById('esc-text').value = '';
}}

function sendKey(key) {{
  fetch(`/runs/${{runId}}/relay/key`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{key}})
  }});
}}

function resumeAgent() {{
  fetch(`/runs/${{runId}}/resume`, {{method: 'POST'}});
}}

function escapeHtml(s) {{
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""

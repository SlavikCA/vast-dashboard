#!/usr/bin/env python3
"""Minimal status page for a vast.ai node.  No dependencies beyond stdlib."""

import json
import os
import time
import urllib.request
from urllib.parse import parse_qs, urlparse
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))
MACHINE_ID = "SET_MACHINE"
API_KEY = "SET_KEY_HERE"
API_URL = f"https://console.vast.ai/api/v0/machines/{MACHINE_ID}/?api_key={API_KEY}"

_cache = None          # (timestamp, data)
_CACHE_TTL = 30        # seconds


def _fetch_machine() -> dict:
    """Return parsed JSON for this machine (short-lived cache)."""
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    req = urllib.request.Request(API_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    machine = data[0] if isinstance(data, list) else data
    _cache = (now, machine)
    return machine


def _mb_to_gb(mb: int | float) -> str:
    gb = mb / 1024
    if gb >= 1:
        return f"{gb:.0f} GB"
    return f"{mb:.0f} MB"


def _status(m: dict) -> str:
    if m.get("current_rentals_running", 0) > 0:
        return "BUSY"
    if m.get("listed"):
        return "AVAILABLE"
    return "IDLE"

def _docker_ps() -> tuple[str, list[dict]]:
    """Return (error_message, [container_dicts]) from docker ps -a.

    Each dict has: name, image, status, ports, state (running / exited / …).
    If docker is unavailable the error string is non-empty and the list empty.
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "-a",
             "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.State}}"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return ("docker CLI not found", [])
    except subprocess.TimeoutExpired:
        return ("docker ps timed out", [])

    if out.returncode != 0:
        msg = out.stderr.strip() or "docker ps failed"
        return (msg, [])

    containers = []
    for line in out.stdout.strip().splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        name, image, status, ports, state = parts
        containers.append({
            "name": name, "image": image, "status": status,
            "ports": ports, "state": state,
        })

    return ("", containers)



CSS = """
body { font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto;
      padding: 0 20px; color: #e0e0e0; background: #1a1a1a; }
h1 { font-size: 2.2em; margin-bottom: 4px; letter-spacing: -0.5px; color: #fff; }
.status { font-size: 1.3em; font-weight: 700; margin-bottom: 28px; }
.status.busy { color: #ef5350; }   .status.idle { color: #66bb6a; }
.status.available { color: #42a5f5; }
table { border-collapse: collapse; width: 100%; }
td { padding: 6px 0; border-bottom: 1px solid #333; }
td:first-child { color: #888; width: 140px; }
.error { color: #ef5350; background: #3e1a1a; padding: 16px; border-radius: 8px; }
h2 { font-size: 1.3em; color: #ccc; margin: 32px 0 12px; }
.containers { margin-bottom: 40px; }
.containers table { width: 100%; border-collapse: collapse; }
.containers td { padding: 5px 8px; border-bottom: 1px solid #2a2a2a;
                 font-size: 0.92em; vertical-align: top; }
.containers td.name { font-weight: 600; color: #ddd; }
.containers td.image { color: #999; font-size: 0.85em; }
.containers tr.running-text td { color: #66bb6a; }
.containers tr.stopped-text td { color: #ef5350; }
.containers .section-label { font-size: 0.82em; color: #666; text-transform: uppercase;
                             letter-spacing: 0.5px; margin: 16px 0 6px; }
.containers .empty { color: #555; font-style: italic; font-size: 0.9em; padding: 4px 8px; }
.containers td.action { width: 70px; }
.containers button { font-size: 0.78em; padding: 2px 10px; border: 1px solid #555;
                    border-radius: 4px; cursor: pointer; background: #2a2a2a; color: #ccc; }
.containers button:hover { background: #3a3a3a; }
.containers button:disabled { opacity: 0.4; cursor: default; }
.containers button.start-btn { border-color: #66bb6a; color: #66bb6a; }
.containers button.start-btn:hover { background: #1a3a1a; }
.containers button.stop-btn  { border-color: #ef5350; color: #ef5350; }
.containers button.stop-btn:hover  { background: #3a1a1a; }
"""

TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{hostname} — vast.ai</title>
<style>{css}</style>
</head>
<body>
<h1>{hostname}</h1>
<div class="status {cls}">{status}</div>
<table>
<tr><td>GPU</td><td>{gpu} ({gpu_ram})</td></tr>
<tr><td>CPU</td><td>{cpu} · {cores}C</td></tr>
<tr><td>RAM</td><td>{ram}</td></tr>
<tr><td>Disk</td><td>{disk} GB</td></tr>
<tr><td>Driver / CUDA</td><td>{driver} / {cuda}</td></tr>
</table>
<h2>Containers</h2>
<div class="containers">
{containers}
</div>
<script>
for (const btn of document.querySelectorAll(".start-btn, .stop-btn")) {{
  btn.addEventListener("click", async () => {{
    btn.disabled = true;
    const action = btn.classList.contains("start-btn") ? "start" : "stop";
    const name = encodeURIComponent(btn.dataset.name);
    try {{ await fetch("/" + action + "?name=" + name, {{ method: "POST" }}); }} catch (_) {{}}
    setTimeout(() => location.reload(), 5000);
  }});
}}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_error(404)
            return

        try:
            m = _fetch_machine()
        except Exception as exc:
            body = f"<p class=error>API error: {exc}</p>"
            self.send_response(502)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        hostname = m["hostname"]
        status = _status(m)
        cls = "busy" if status == "BUSY" else ("available" if status == "AVAILABLE" else "idle")


        err, containers = _docker_ps()
        if err:
            container_html = f'<p class="error">Docker: {err}</p>'
        elif not containers:
            container_html = '<p class="empty">No containers found.</p>'
        else:
            running = [c for c in containers if c["state"] == "running"]
            stopped = [c for c in containers if c["state"] != "running"]
            rows = []
            if running:
                rows.append(f'<div class="section-label">Running ({len(running)})</div>')
                rows.append("<table>")
                for c in running:
                    btn = ""
                    if not c["name"].startswith("C."):
                        btn = f'<td class="action"><button class="stop-btn" data-name="{c["name"]}">STOP</button></td>'
                    rows.append(
                        f'<tr class="running-text">{btn}'
                        f'<td class="name">{c["name"]}</td>'
                        f'<td class="image">{c["image"]}</td></tr>'
                    )
                rows.append("</table>")
            if stopped:
                rows.append(f'<div class="section-label">Stopped ({len(stopped)})</div>')
                rows.append("<table>")
                for c in stopped:
                    btn = ""
                    if not c["name"].startswith("C."):
                        btn = f'<td class="action"><button class="start-btn" data-name="{c["name"]}">START</button></td>'
                    rows.append(
                        f'<tr class="stopped-text">{btn}'
                        f'<td class="name">{c["name"]}</td>'
                        f'<td class="image">{c["image"]}</td></tr>'
                    )
                rows.append("</table>")
            container_html = "\n".join(rows)

        html = TEMPLATE.format(
            css=CSS,
            hostname=hostname,
            status=status,
            cls=cls,
            gpu=m.get("gpu_name", "—"),
            gpu_ram=_mb_to_gb(m.get("gpu_ram", 0)),
            cpu=m.get("cpu_name", "—"),
            cores=m.get("cpu_cores", "—"),
            ram=_mb_to_gb(m.get("cpu_ram", 0)),
            disk=m.get("avail_disk_space", m.get("disk_space", "—")),
            driver=m.get("driver_version", "—"),
            containers=container_html,
            cuda=m.get("cuda_max_good", "—"),
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())


    def do_POST(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        name = (qs.get("name", [""])[0]).strip()

        # basic validation — only allow container-name-ish chars
        if not name or not all(c.isalnum() or c in "_-." for c in name):
            self.send_error(400, "bad container name")
            return

        if url.path == "/start":
            cmd = ["docker", "start", name]
        elif url.path == "/stop":
            cmd = ["docker", "stop", name]
        else:
            self.send_error(404)
            return

        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        except subprocess.CalledProcessError as exc:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": exc.stderr.strip()}).encode())
            return
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def log_message(self, fmt, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"listening on :{PORT}  (machine {MACHINE_ID})")
    HTTPServer(("", PORT), Handler).serve_forever()

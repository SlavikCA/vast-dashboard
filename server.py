#!/usr/bin/env python3
"""Minimal status page for a vast.ai node.  No dependencies beyond stdlib."""

import html
import ipaddress
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from urllib.parse import parse_qs, urlparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 7000))
MACHINE_ID = os.environ.get("MACHINE_ID", "")
API_KEY = os.environ.get("API_KEY", "")
SHOUT = os.environ.get("SHOUT","")
LOG_FILE = os.environ.get("LOG_FILE", "./dashboard.log")
LOG_TIMESTAMP = os.environ.get("LOG_TIMESTAMP", "false").lower() in ("1", "true", "yes", "on")
DEADLOAD_FILE = os.environ.get("DEADLOAD_FILE", "./deadload.json")
DEADLOAD_IMAGE = os.environ.get("DEADLOAD_IMAGE", "nvidia/cuda:13.3.0-devel-ubuntu24.04")
PAGE_REFRESH = int(os.environ.get("PAGE_REFRESH", "1800"))   # seconds
API_URL = "https://console.vast.ai/api"

# https://docs.vast.ai/api-reference/instances/create-instance

_cache = None          # (timestamp, data)
_offers_cache = None   # (timestamp, [offer dicts])
_market_cache = None   # (timestamp, [market offer dicts])
_CACHE_TTL = 30        # seconds

def _log(msg: str) -> None:
    """Append a line to LOG_FILE and print to console."""
    if LOG_TIMESTAMP:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"{ts} {msg}"
    else:
        line = msg
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _fetch_machine(force: bool = False) -> dict:
    """Return parsed JSON for this machine (short-lived cache)."""
    global _cache
    now = time.time()
    if not force and _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    machine_url = f"{API_URL}/v0/machines/{MACHINE_ID}/"
    _log(f"vast.ai GET {machine_url}")
    req = urllib.request.Request(machine_url, headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        data = json.loads(raw)

    machine = data[0] if isinstance(data, list) else data
    _cache = (now, machine)
    _log(f"vast.ai OK hostname={machine.get('hostname')} gpu={machine.get('gpu_name')}")
    return machine


def _fetch_offers(force: bool = False) -> tuple[list[dict], str | None]:
    """Return (offers, error) — rentable on-demand offers for this machine.

    Each offer carries its own ``id`` (the ask id used to start a rental) and
    host details such as ``public_ipaddr``.  Errors fall back to the cached
    list; the error string is returned when nothing usable is available.
    """
    global _offers_cache
    now = time.time()
    if not force and _offers_cache and now - _offers_cache[0] < _CACHE_TTL:
        return _offers_cache[1], None

    status, data = _vast_api("POST", f"{API_URL}/v0/bundles/", {
        "external": {"eq": False},
        "rentable": {"eq": True},
        "machine_id": {"eq": MACHINE_ID},
        "type": "on-demand",
        "order": [["score", "desc"]],
    })
    err = _api_error(status, data)
    if err:
        msg = err.get("msg") or json.dumps(err)
        if _offers_cache:
            return _offers_cache[1], f"offers API error (using cached data): {msg}"
        return [], msg

    # Keep only GPUs that are actually free to rent: on a multi-GPU host some
    # cards may be rented out while others stay available.
    offers = [o for o in (data.get("offers") or [])
              if o.get("rentable", True) and not o.get("rented")]
    _offers_cache = (now, offers)
    _log(f"vast.ai OK offers={[o.get('id') for o in offers]}")
    return offers, None


def _fetch_market_top10(gpu_name: str, force: bool = False) -> tuple[list[dict], str | None]:
    """Return (top10, error) — top-10 rentable on-demand offers by score for a
    GPU model (the examples/offers-on-the-market.md query narrowed to one GPU).

    The example's sample-specific filters (verified, US) are dropped so that a
    rare GPU model still yields a range.  Errors fall back to the cached list.
    """
    global _market_cache
    now = time.time()
    if not force and _market_cache and now - _market_cache[0] < _CACHE_TTL:
        return _market_cache[1], None

    status, data = _vast_api("POST", f"{API_URL}/v0/bundles/", {
        "external": {"eq": False},
        "rentable": {"eq": True},
        "gpu_name": {"in": [gpu_name]},
        "num_gpus": {"eq": "1"},
        "order": [["score", "desc"]],
        "type": "on-demand",
        "limit": 10,
        "allocated_storage": 5.0,
    })
    err = _api_error(status, data)
    if err:
        msg = err.get("msg") or json.dumps(err)
        if _market_cache:
            return _market_cache[1], f"market API error (using cached data): {msg}"
        return [], msg

    offers = data.get("offers") or []
    _market_cache = (now, offers)
    _log(f"vast.ai OK market top10 gpu={gpu_name} n={len(offers)}")
    return offers, None


def _vast_api(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    """Call the vast.ai API with bearer auth; return (http_status, parsed JSON).

    http_status is 0 when the request failed at the transport level.
    """
    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    _log(f"vast.ai {method} {url}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        _log(f"vast.ai {method} transport error: {exc.reason}")
        return 0, {"error": "network", "msg": str(exc.reason)}
    except OSError as exc:
        _log(f"vast.ai {method} transport error: {exc}")
        return 0, {"error": "network", "msg": str(exc)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"error": "bad_response", "msg": f"Non-JSON response (HTTP {status}): {raw[:200]!r}"}


def _api_error(status: int, data) -> dict | None:
    """Return an error payload to surface to the user, or None on success."""
    if not isinstance(data, dict):
        return {"error": "bad_response", "msg": f"Unexpected response from vast.ai: {data!r}"}
    if data.get("error"):
        return data                      # pass through vast's own error payload
    if status >= 400:
        return {"error": f"HTTP {status}", "msg": f"HTTP {status}: {json.dumps(data)}"}
    return None


_VIRTUAL_IFACES = ("docker", "br-", "veth", "virbr", "tun", "tap", "wg", "lo")

def _lan_ips() -> list[str]:
    """Local IPv4 addresses of this host, private (LAN) ones first.

    The vast.ai API only reports the NAT public address, so the LAN address
    has to come from the machine itself.
    """
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []

    addrs = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1].startswith(_VIRTUAL_IFACES):
            continue
        addr = parts[3].split("/")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if addr not in addrs:
            addrs.append(addr)

    private = [a for a in addrs if ipaddress.ip_address(a).is_private]
    return private or addrs


def _host_field(m: dict, offers: list[dict], key: str):
    """Host-level value: the machine response wins, the first offer is the fallback."""
    if m.get(key) is not None:
        return m[key]
    for offer in offers:
        if offer.get(key) is not None:
            return offer[key]
    return None


def _fmt_percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _fmt_mbps(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} Mbps"


def _fmt_gbs(value: float | None) -> str:
    """Bandwidth already reported in GB/s (vast: pcie_bw, gpu_mem_bw)."""
    if value is None:
        return "—"
    return f"{value:.1f} GB/s"


def _fmt_gbs_from_mbs(value: float | None) -> str:
    """MB/s → GB/s (vast reports disk_bw in MB/s)."""
    if value is None:
        return "—"
    return f"{value / 1000:.1f} GB/s"


def _mb_to_gb(mb: int | float) -> str:
    gb = mb / 1024
    if gb >= 1:
        return f"{gb:.0f} GB"
    return f"{mb:.0f} MB"


def _fmt_dollar(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"${value:.2f}{suffix}"


def _mul(value: float | None, factor: float) -> float | None:
    if value is None:
        return None
    return value * factor


def _on_demand_running(m: dict) -> bool:
    """True when the machine status response shows an on-demand container running.

    vast.ai encodes rental types in gpu_occupancy: 'D' = on-demand,
    'I' = interruptible.  The machine is rented when current_rentals_running > 0.
    """
    return (
        m.get("current_rentals_running", 0) > 0
        and "D" in m.get("gpu_occupancy", "")
    )


def _status(m: dict) -> str:
    if _on_demand_running(m):
        return "BUSY with ON-DEMAND"
    if m.get("current_rentals_running", 0) > 0:
        return "BUSY with interruptible"
    if m.get("listed"):
        return "AVAILABLE"
    return "IDLE"

_error_active = False

def _machine_errors(m: dict) -> str:
    """Return combined error text from error_description and vm_error_msg, or empty string."""
    parts = []
    if m.get("error_description"):
        parts.append(f"error_description: {m['error_description']}")
    if m.get("vm_error_msg"):
        parts.append(f"vm_error_msg: {m['vm_error_msg']}")
    return "\n".join(parts)

def _maybe_alert(has_error: bool, msg: str) -> None:
    """Send error via shoutrrr only on new error appearance; skip repeats and resolutions."""
    global _error_active
    if has_error and not _error_active:
        _error_active = True
        _log(f"shoutrrr alert: {msg[:200]}")
        try:
            subprocess.run(
                ["shoutrrr", "send", "--url", SHOUT, "-m", msg],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            _log(f"shoutrrr failed: {exc}")
    elif not has_error:
        _error_active = False

_host_busy = False       # was the host BUSY (any kind) at the last check?

def _maybe_alert_available(m: dict) -> None:
    """Notify when the host transitions from BUSY (any kind) to AVAILABLE."""
    global _host_busy
    status = _status(m)
    is_busy = status.startswith("BUSY")
    if not is_busy and _host_busy and status == "AVAILABLE":
        hostname = m.get("hostname", MACHINE_ID)
        msg = f"[{hostname}] Machine is now AVAILABLE"
        _log(f"shoutrrr available-alert: {msg}")
        try:
            subprocess.run(
                ["shoutrrr", "send", "--url", SHOUT, "-m", msg],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            _log(f"shoutrrr available-alert failed: {exc}")
    _host_busy = is_busy

def _error_check_loop() -> None:
    """Background thread: check immediately, then every 15 min."""
    # First check after a short delay (let the server start)
    time.sleep(10)
    while True:
        try:
            m = _fetch_machine(force=True)
            error_text = _machine_errors(m)
            _maybe_alert(bool(error_text), f"[{m.get('hostname', MACHINE_ID)}] {error_text}")
            _maybe_alert_available(m)
        except Exception as exc:
            _log(f"error check failed: {exc}")
        time.sleep(15 * 60)

def _nv_gpu_map(names: list[str]) -> dict[str, str]:
    """Map container name → NV_GPU env value (containers without it are omitted).

    Vast.ai exposes the assigned GPU to a rented container as NV_GPU
    (e.g. "0", or "0,1" for a multi-GPU contract), so it is read from the
    container env rather than guessed from the host.
    """
    if not names:
        return {}
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}\t{{json .Config.Env}}", *names],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}

    nv = {}
    for line in out.stdout.splitlines():
        name, _, env_json = line.partition("\t")
        try:
            env = json.loads(env_json) or []
        except ValueError:
            continue
        for item in env:
            if item.startswith("NV_GPU="):
                nv[name.lstrip("/")] = item[len("NV_GPU="):]
                break
    return nv


def _docker_ps() -> tuple[str, list[dict]]:
    """Return (error_message, [container_dicts]) from docker ps -a.

    Each dict has: name, image, status, ports, state (running / exited / …)
    and nv_gpu (NV_GPU env value, "" when the container has none).
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
            "ports": ports, "state": state, "nv_gpu": "",
        })

    nv = _nv_gpu_map([c["name"] for c in containers])
    for c in containers:
        c["nv_gpu"] = nv.get(c["name"], "")

    return ("", containers)



CSS = """
body { font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto;
      padding: 0 20px; color: #e0e0e0; background: #1a1a1a; }
body.deadload-off { background: #3e1a1a; }
body.deadload-on  { background: #1a3a1a; }
h1 { font-size: 2.2em; margin-bottom: 4px; letter-spacing: -0.5px; color: #fff; }
.status { font-size: 1.3em; font-weight: 700; margin-bottom: 28px; }
.status .ts { font-size: 0.6em; font-weight: 400; color: #888; margin-left: 8px; }
.status.busy { color: #ff0000; }   .status.idle { color: #00ff00; }
.status.available { color: #42a5f5; }
table { border-collapse: collapse; width: 100%; }
td { padding: 6px 0; border-bottom: 1px solid #333; }
td:first-child { color: #888; width: 140px; }
.error { color: #ff0000; background: #3e1a1a; padding: 16px; border-radius: 8px; }
h2 { font-size: 1.3em; color: #ccc; margin: 32px 0 12px; }
.containers { margin-bottom: 40px; }
.containers table { width: 100%; border-collapse: collapse; }
.containers td { padding: 5px 8px; border-bottom: 1px solid #2a2a2a;
                 font-size: 0.92em; vertical-align: top; }
.containers td.name { font-weight: 600; color: #ddd; }
.containers td.image { color: #999; font-size: 0.85em; }
.containers td.uptime { color: #999; font-size: 0.82em; white-space: nowrap; }
.containers td.gpu { color: #7ab8ff; font-size: 0.85em; white-space: nowrap; }
.containers tr.running-text td { color: #00ff00; }
.containers tr.stopped-text td { color: #ff0000; }
.containers .section-label { font-size: 0.82em; color: #666; text-transform: uppercase;
                             letter-spacing: 0.5px; margin: 16px 0 6px; }
.containers .empty { color: #555; font-style: italic; font-size: 0.9em; padding: 4px 8px; }
.containers td.action { width: 70px; }
.containers button { font-size: 0.78em; padding: 2px 10px; border: 1px solid #555;
                    border-radius: 4px; cursor: pointer; background: #2a2a2a; color: #ccc; }
.containers button:hover { background: #3a3a3a; }
.containers button:disabled { opacity: 0.4; cursor: default; }
.containers button.start-btn { border-color: #00ff00; color: #00ff00; }
.containers button.start-btn:hover { background: #1a3a1a; }
.containers button.stop-btn  { border-color: #ff0000; color: #ff0000; }
.containers button.stop-btn:hover  { background: #3a1a1a; }
@keyframes sweep {
  from { background-size: 0% 100%; }
  to   { background-size: 100% 100%; }
}
button.sweeping {
  background-image: linear-gradient(to right, rgba(255,255,255,0.2), rgba(255,255,255,0.2));
  background-repeat: no-repeat;
  animation: sweep 5s linear forwards;
}
.deadload { margin-bottom: 40px; }
.deadload button { font-size: 1em; padding: 8px 20px; border-radius: 6px;
                   cursor: pointer; background: #2a2a2a; }
.deadload button:disabled { opacity: 0.4; cursor: default; }
.deadload button.start-btn { border: 1px solid #00ff00; color: #00ff00; }
.deadload button.start-btn:hover { background: #1a3a1a; }
.deadload button.stop-btn { border: 1px solid #ff0000; color: #ff0000; }
.deadload button.stop-btn:hover { background: #3a1a1a; }
.deadload .offer { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.deadload .offer-info { font-size: 0.82em; color: #888; }
"""
TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{page_refresh}">
<title>{hostname} — vast.ai</title>
<style>{css}</style>
</head>
<body class="{body_cls}">
<h1>{hostname}</h1>
<div class="status {cls}">{status} <span class="ts" id="ts"></span></div>
{errors}
<table>
<tr><td>Machine ID</td><td>{machine_id}</td></tr>
<tr><td>LAN IP</td><td>{lan_ip}</td></tr>
<tr><td>GPU</td><td>{gpu} ({gpu_ram})</td></tr>
<tr><td>PCIe</td><td>{pcie}</td></tr>
<tr><td>CPU</td><td>{cpu} · {cores}C</td></tr>
<tr><td>RAM</td><td>{ram}</td></tr>
<tr><td>Disk</td><td>{disk}</td></tr>
<tr><td>Disk speed</td><td>{disk_bw}</td></tr>
<tr><td>Network</td><td>{inet}</td></tr>
<tr><td>Reliability</td><td>{reliability}</td></tr>
<tr><td>Driver / CUDA</td><td>{driver} / {cuda}</td></tr>
<tr><td>GPU price</td><td>{gpu_price}</td></tr>
<tr><td>Storage price</td><td>{storage_price}</td></tr>
<tr><td>Volume price</td><td>{volume_price}</td></tr>
<tr><td>Internet price</td><td>{inet_price}</td></tr>
<tr><td>On-demand offers</td><td>{offers}</td></tr>
<tr><td>{market_label}</td><td>{market_range}</td></tr>
</table>
<h2>Deadload</h2>
<div class="deadload">
<div id="deadload-msg" class="error" style="display:none"></div>
{deadload_btn}
</div>
<h2>Containers</h2>
<div class="containers">
{containers}
</div>
<script>
document.getElementById("ts").textContent = new Date().toLocaleString();
for (const btn of document.querySelectorAll(".containers .start-btn, .containers .stop-btn")) {{
  btn.addEventListener("click", async () => {{
    btn.disabled = true;
    btn.classList.add("sweeping");
    const action = btn.classList.contains("start-btn") ? "start" : "stop";
    const name = encodeURIComponent(btn.dataset.name);
    try {{ await fetch("/" + action + "?name=" + name, {{ method: "POST" }}); }} catch (_) {{}}
    setTimeout(() => location.reload(), 5000);
  }});
}}
const dmsg = document.getElementById("deadload-msg");
for (const dbtn of document.querySelectorAll(".deadload button")) {{
  dbtn.addEventListener("click", async () => {{
    dbtn.disabled = true;
    dbtn.classList.add("sweeping");
    const isStart = dbtn.classList.contains("start-btn");
    const action = isStart ? "start" : "stop";
    const qs = isStart ? "?offer=" + encodeURIComponent(dbtn.dataset.offer) : "";
    try {{
      const resp = await fetch("/deadload/" + action + qs, {{ method: "POST" }});
      const raw = await resp.text();
      const data = raw ? JSON.parse(raw) : {{}};
      const errText = data.msg || data.error;
      if (errText) {{
        dmsg.textContent = "Deadload " + action + " failed: " + errText;
        dmsg.style.display = "block";
        dbtn.disabled = false;
        dbtn.classList.remove("sweeping");
      }} else {{
        dmsg.style.display = "none";
        setTimeout(() => location.reload(), 10000);
      }}
    }} catch (_) {{
      dmsg.textContent = "Deadload request failed (server unreachable).";
      dmsg.style.display = "block";
      dbtn.disabled = false;
      dbtn.classList.remove("sweeping");
    }}
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

        api_error = None
        try:
            m = _fetch_machine()
        except Exception as exc:
            if _cache:
                m = _cache[1]
                api_error = f"API error (using cached data): {exc}"
                _log(api_error)
            else:
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

        offers, offers_err = _fetch_offers()
        gpu_name = m.get("gpu_name")
        market, market_err = _fetch_market_top10(gpu_name) if gpu_name else ([], None)

        hostname = m["hostname"]
        status = _status(m)
        _log(f"web {self.command} {self.path} from {self.client_address[0]}")
        cls = "busy" if status.startswith("BUSY") else ("available" if status == "AVAILABLE" else "idle")
        machine_errors = _machine_errors(m)
        combined = "\n".join(filter(None, [api_error, machine_errors]))
        error_html = f'<p class="error">{html.escape(combined)}</p>' if combined else ""
        if offers_err:
            error_html += f'<p class="error">Offers: {html.escape(offers_err)}</p>'
        if market_err:
            error_html += f'<p class="error">Market: {html.escape(market_err)}</p>'
        if combined:
            _maybe_alert(True, f"[{hostname}] {combined}")
        else:
            _maybe_alert(False, "")

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
                    if not c["name"].startswith("C."):
                        btn = f'<td class="action"><button class="stop-btn" data-name="{c["name"]}">STOP</button></td>'
                    else:
                        btn = '<td class="action"></td>'
                    gpu = (f'<td class="gpu">NV_GPU={html.escape(c["nv_gpu"])}</td>'
                           if c["nv_gpu"] else "")
                    rows.append(
                        f'<tr class="running-text">{btn}'
                        f'<td class="name">{c["name"]}</td>'
                        f'<td class="image">{c["image"]}</td>'
                        f'{gpu}'
                        f'<td class="uptime">{c["status"]}</td></tr>'
                    )
                rows.append("</table>")
            if stopped:
                rows.append(f'<div class="section-label">Stopped ({len(stopped)})</div>')
                rows.append("<table>")
                for c in stopped:
                    if not c["name"].startswith("C."):
                        btn = f'<td class="action"><button class="start-btn" data-name="{c["name"]}">START</button></td>'
                    else:
                        btn = '<td class="action"></td>'
                    rows.append(
                        f'<tr class="stopped-text">{btn}'
                        f'<td class="name">{c["name"]}</td>'
                        f'<td class="image">{c["image"]}</td></tr>'
                    )
                rows.append("</table>")
            container_html = "\n".join(rows)

        deadload_on = os.path.exists(DEADLOAD_FILE)
        dl_id = "?"
        if deadload_on:
            try:
                with open(DEADLOAD_FILE) as f:
                    dl = json.load(f)
                dl_id = dl.get("id", "?")
            except (OSError, json.JSONDecodeError):
                dl_id = "?"
            deadload_btn = (
                f'<button class="stop-btn deadload-btn"'
                f' title="Make sure to stop all your personal containers and processes before you release DEADLOAD">'
                f'STOP DEADLOAD ({dl_id})</button>'
            )
        elif offers:
            # One START button per free GPU (rentable offer): the offer id is
            # the ask id the rental is created from.  Only the rented-out GPUs
            # of a multi-GPU host are missing from the list, so a machine that
            # is partially rented still gets buttons for its free cards.
            blocks = []
            for o in offers:
                oid = o.get("id")
                if oid is None:
                    continue
                bits = []
                if o.get("num_gpus"):
                    bits.append(f"{o['num_gpus']}× {o.get('gpu_name', 'GPU')}")
                elif o.get("gpu_name"):
                    bits.append(o["gpu_name"])
                if o.get("gpu_ram"):
                    bits.append(_mb_to_gb(o["gpu_ram"]))
                if o.get("dph_total") is not None:
                    bits.append(_fmt_dollar(o["dph_total"], "/hr"))
                info = html.escape(" · ".join(bits))
                blocks.append(
                    f'<div class="offer">'
                    f'<button class="start-btn deadload-btn" data-offer="{html.escape(str(oid))}"'
                    f' title="DEADLOAD is the container, which does nothing, but marks the GPU as busy, so you can use it for your tasks.">'
                    f'START DEADLOAD {html.escape(str(oid))}</button>'
                    f'<span class="offer-info">{info}</span></div>'
                )
            deadload_btn = "\n".join(blocks)
        elif _on_demand_running(m):
            deadload_btn = '<p class="empty">GPU is rented on-demand — deadload unavailable.</p>'
        else:
            deadload_btn = '<p class="empty">No rentable on-demand offers.</p>'

        # Vast.ai names the host container for a contract C.<contract_id>.
        # The contract file exists as soon as the order is accepted, but the
        # container takes time to appear — show the green state only when the
        # matching container is actually running.
        deadload_running = (
            deadload_on
            and any(c["state"] == "running" and c["name"] == f"C.{dl_id}"
                    for c in containers)
        )

        # The deadload contract occupies the GPU: surface it as the status
        # instead of the generic BUSY/AVAILABLE line.
        if deadload_running:
            status = "DEADLOAD running"
            cls = "busy"

        reliability = _host_field(m, offers, "reliability")
        if reliability is None:
            reliability = m.get("reliability2")   # machines API name for the same metric
        pci_gen = _host_field(m, offers, "pci_gen")
        gpu_lanes = _host_field(m, offers, "gpu_lanes")
        inet_up_cost = _host_field(m, offers, "listed_inet_up_cost")
        inet_down_cost = _host_field(m, offers, "listed_inet_down_cost")

        # One line per rentable offer: ask id followed by its score.
        offer_lines = []
        for o in offers:
            oid = o.get("id")
            if oid is None:
                continue
            score = o.get("score")
            line = str(oid)
            if isinstance(score, (int, float)):
                line += f" · score {score:.2f}"
            offer_lines.append(html.escape(line))
        offers_html = "<br>".join(offer_lines) or "—"

        # Reference range: top-10 market offers for this GPU model by score.
        # A thin market shows the actual count instead of a misleading 10.
        gpu_disp = gpu_name or "—"
        market_label = f"Market top 10 ({gpu_disp})"
        market_scores = [o["score"] for o in market if isinstance(o.get("score"), (int, float))]
        if market_scores:
            market_range = f"{min(market_scores):.2f} – {max(market_scores):.2f}"
            if len(market_scores) < 10:
                market_range += f" (top {len(market_scores)}, limited market data)"
        else:
            market_range = "no listings"
        page = TEMPLATE.format(
            css=CSS,
            body_cls="deadload-on" if deadload_running else "deadload-off",
            hostname=hostname,
            status=status,
            cls=cls,
            errors=error_html,
            page_refresh=PAGE_REFRESH,
            deadload_btn=deadload_btn,
            machine_id=m.get("id", MACHINE_ID),
            lan_ip=", ".join(_lan_ips()) or "—",
            offers=offers_html,
            market_label=market_label,
            market_range=market_range,
            gpu=gpu_disp,
            gpu_ram=_mb_to_gb(m.get("gpu_ram", 0)),
            pcie=" · ".join(filter(None, [
                f"Gen {pci_gen}" if pci_gen is not None else None,
                _fmt_gbs(_host_field(m, offers, "pcie_bw")),
                f"{gpu_lanes} lanes" if gpu_lanes is not None else None,
            ])),
            cpu=m.get("cpu_name", "—"),
            cores=m.get("cpu_cores", "—"),
            ram=_mb_to_gb(m.get("cpu_ram", 0)),
            disk=f"{m.get('avail_disk_space', m.get('disk_space', 0)) / 1024:.1f} TB",
            disk_bw=_fmt_gbs_from_mbs(_host_field(m, offers, "disk_bw")),
            inet=f"↑ {_fmt_mbps(_host_field(m, offers, 'inet_up'))} · ↓ {_fmt_mbps(_host_field(m, offers, 'inet_down'))}",
            reliability=_fmt_percent(reliability),
            driver=m.get("driver_version", "—"),
            containers=container_html,
            cuda=m.get("cuda_max_good", "—"),
            gpu_price=_fmt_dollar(m.get("listed_gpu_cost"), "/hr"),
            storage_price=_fmt_dollar(m.get("listed_storage_cost"), "/GB/month"),
            volume_price=_fmt_dollar(m.get("listed_volume_cost"), "/GB/month"),
            inet_price=(
                f"↑ {_fmt_dollar(_mul(inet_up_cost, 1000), '/TB')}"
                f" · ↓ {_fmt_dollar(_mul(inet_down_cost, 1000), '/TB')}"
            ),
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())


    def do_POST(self):
        url = urlparse(self.path)

        if url.path == "/deadload/start":
            self._deadload_start()
            return
        if url.path == "/deadload/stop":
            self._deadload_stop()
            return

        qs = parse_qs(url.query)
        name = (qs.get("name", [""])[0]).strip()

        # basic validation — only allow container-name-ish chars
        if not name or not all(c.isalnum() or c in "_-." for c in name):
            self.send_error(400, "bad container name")
            return

        _log(f"web {self.command} {self.path} from {self.client_address[0]}")
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

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _deadload_start(self) -> None:
        """Rent this machine as a 'deadload' instance from the chosen offer."""
        offer_id = (parse_qs(urlparse(self.path).query).get("offer", [""])[0]).strip()
        if not offer_id.isdigit():
            self._send_json({"error": "bad_offer", "msg": "Missing or invalid offer id"}, 400)
            return
        _log(f"web POST /deadload/start offer={offer_id} from {self.client_address[0]}")

        # 1) Create the instance from that offer.
        status, data = _vast_api(
            "PUT",
            f"{API_URL}/v0/asks/{offer_id}/",
            {
                "client_id": "me",
                "image": DEADLOAD_IMAGE,
                "env": {},
                "price": None,
                "disk": 10,
                "runtype": "ssh_direct ssh_proxy",
                "label": "deadload",
            },
        )
        err = _api_error(status, data)
        if err:
            self._send_json(err)
            return
        contract_id = data.get("new_contract")
        if not contract_id:
            self._send_json({"error": "no_contract",
                             "msg": f"Unexpected response from vast.ai: {json.dumps(data)}"})
            return

        try:
            with open(DEADLOAD_FILE, "w") as f:
                json.dump({"id": contract_id, "offer": int(offer_id)}, f)
        except OSError as exc:
            self._send_json({"error": "io", "msg": f"Could not write {DEADLOAD_FILE}: {exc}"})
            return
        _log(f"deadload started contract={contract_id}")
        self._send_json({"ok": True, "id": contract_id})

    def _deadload_stop(self) -> None:
        """Delete the deadload instance, then remove the state file."""
        _log(f"web POST /deadload/stop from {self.client_address[0]}")
        try:
            with open(DEADLOAD_FILE) as f:
                contract_id = json.load(f)["id"]
        except (OSError, ValueError, KeyError):
            self._send_json({"error": "no_file", "msg": f"No {DEADLOAD_FILE} found"})
            return

        status, data = _vast_api("DELETE", f"{API_URL}/v0/instances/{contract_id}")

        # The DELETE was sent: drop the state file (per spec). Keep it only when
        # vast was never reached — then the instance is still alive.
        if status != 0:
            try:
                os.remove(DEADLOAD_FILE)
            except OSError:
                pass

        err = _api_error(status, data)
        if err:
            self._send_json(err)
            return
        _log(f"deadload stopped contract={contract_id}")
        self._send_json({"ok": True})

    def log_message(self, fmt, *args):
        pass  # quiet


if __name__ == "__main__":
    threading.Thread(target=_error_check_loop, daemon=True).start()
    print(f"listening on :{PORT}  (machine {MACHINE_ID})")
    HTTPServer(("", PORT), Handler).serve_forever()

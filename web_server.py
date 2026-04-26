"""
web_server.py — ARGOS Master Dashboard Web Server
Слушает порт 18789, отдаёт dashboard_master.html, проксирует Brain API,
предоставляет /api/metrics через psutil.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    import requests
    _REQUESTS = True
except ImportError:
    _REQUESTS = False

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BRAIN_API  = os.getenv("ARGOS_BRAIN_URL", "http://localhost:5010")
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PORT       = int(os.getenv("ARGOS_WEB_PORT", "18789"))

_metrics_cache: dict = {}
_metrics_ts: float   = 0.0
METRICS_TTL: float   = 2.0  # seconds


# ── Static files ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "argos_dashboard.html")


@app.route("/<path:filename>")
def static_files(filename: str):
    safe = os.path.normpath(filename)
    if safe.startswith(".."):
        return "Forbidden", 403
    return send_from_directory(BASE_DIR, safe)


# ── Brain API proxy ───────────────────────────────────────────────────────────

def _proxy(path: str, method: str | None = None) -> Response:
    if not _REQUESTS:
        return jsonify({"error": "requests not installed"}), 500
    url = f"{BRAIN_API}/{path.lstrip('/')}"
    m   = (method or request.method).upper()
    try:
        if m == "GET":
            r = requests.get(url, params=request.args, timeout=10)
        else:
            r = requests.request(
                m, url,
                json=request.get_json(silent=True),
                params=request.args,
                timeout=30,
            )
        return Response(
            r.content, status=r.status_code,
            content_type=r.headers.get("Content-Type", "application/json"),
        )
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Brain API недоступен", "url": url}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/brain/<path:path>", methods=["GET", "POST", "DELETE"])
def brain_proxy(path: str):
    return _proxy(path)


@app.route("/api/health")
def health():
    return _proxy("health")


@app.route("/api/status")
def brain_status():
    return _proxy("brain/status")


@app.route("/api/nodes")
def nodes():
    return _proxy("brain/nodes")


@app.route("/api/docker")
def docker_status():
    return _proxy("docker/status")


@app.route("/api/logs")
def logs_proxy():
    return _proxy("api/logs")


# ── /api/metrics — psutil CPU/RAM/Disk + optional Docker ─────────────────────

def _collect_metrics() -> dict:
    global _metrics_cache, _metrics_ts
    now = time.monotonic()
    if now - _metrics_ts < METRICS_TTL and _metrics_cache:
        return _metrics_cache

    data: dict = {}

    if _PSUTIL:
        cpu   = psutil.cpu_percent(interval=0.3)
        ram   = psutil.virtual_memory()
        disk  = psutil.disk_usage("/") if os.name != "nt" else psutil.disk_usage("C:\\")
        net   = psutil.net_io_counters()
        conns = len(psutil.net_connections())

        data.update({
            "cpu_percent":   round(cpu, 1),
            "ram_percent":   round(ram.percent, 1),
            "ram_used_gb":   round(ram.used   / 1e9, 2),
            "ram_total_gb":  round(ram.total  / 1e9, 2),
            "disk_percent":  round(disk.percent, 1),
            "disk_free_gb":  round(disk.free   / 1e9, 2),
            "disk_total_gb": round(disk.total  / 1e9, 2),
            "net_bytes_sent": net.bytes_sent,
            "net_bytes_recv": net.bytes_recv,
            "net_connections": conns,
        })

        # CPU temperature (Linux hwmon / sensors; Windows returns empty)
        try:
            temps = psutil.sensors_temperatures()
            for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
                if key in temps and temps[key]:
                    data["cpu_temp"] = round(temps[key][0].current, 1)
                    break
        except (AttributeError, NotImplementedError):
            pass

        # GPU via nvidia-smi (optional)
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                timeout=3, stderr=subprocess.DEVNULL,
            ).decode().strip().split("\n")[0]
            parts = [p.strip() for p in out.split(",")]
            if len(parts) >= 3:
                data["gpu_percent"]  = float(parts[0])
                data["gpu_mem_used"] = float(parts[1])
                data["gpu_mem_total"]= float(parts[2])
        except Exception:
            pass

    else:
        data["error"] = "psutil not installed — run: pip install psutil"

    # Docker containers via subprocess (no daemon socket needed on Windows)
    try:
        raw = subprocess.check_output(
            ["docker", "ps", "--format",
             '{"name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}",'
             '"ports":"{{.Ports}}"}'],
            timeout=5, stderr=subprocess.DEVNULL,
        ).decode().strip()
        containers = []
        for line in raw.splitlines():
            try:
                c = json.loads(line)
                c["status"] = "running" if c.get("status", "").startswith("Up") else c.get("status", "")
                containers.append(c)
            except json.JSONDecodeError:
                pass
        data["docker"] = containers
    except Exception:
        data["docker"] = []

    _metrics_cache = data
    _metrics_ts    = now
    return data


@app.route("/api/metrics")
def metrics():
    return jsonify(_collect_metrics())


# ── Content Fetcher ───────────────────────────────────────────────────────────

@app.route("/api/fetch/ping")
def fetch_ping():
    return jsonify({"ok": True, "service": "argos-fetch", "port": PORT})


@app.route("/api/fetch", methods=["POST"])
def fetch_url():
    """Server-side URL fetch — bypasses CORS, returns size + content type."""
    if not _REQUESTS:
        return jsonify({"error": "requests not installed"}), 500

    data    = request.get_json(silent=True) or {}
    url     = data.get("url", "").strip()
    mode    = data.get("mode", "free")
    # lic   = data.get("lic", "all")   # reserved for future filtering

    if not url:
        return jsonify({"error": "url required"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "invalid url"}), 400

    # Offline mode: only allow localhost / LAN
    if mode == "offline":
        import urllib.parse
        host = urllib.parse.urlparse(url).hostname or ""
        if not (host in ("localhost", "127.0.0.1", "::1")
                or host.startswith("192.168.")
                or host.startswith("10.")
                or host.startswith("172.")):
            return jsonify({"error": "offline mode: only localhost/LAN allowed", "host": host}), 403

    try:
        headers = {"User-Agent": "ARGOS-Fetch/2.1 (compatible; +https://argos.local)"}
        r = requests.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True)
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        # Read up to 512 KB
        chunk = r.raw.read(524288)
        size  = len(chunk)
        r.close()

        # Try to detect text vs binary
        is_text = any(t in content_type for t in ("text", "json", "xml", "javascript", "yaml"))
        preview = ""
        if is_text and chunk:
            try:
                preview = chunk.decode("utf-8", errors="replace")[:400]
            except Exception:
                pass

        return jsonify({
            "ok":           True,
            "url":          url,
            "status":       r.status_code,
            "content_type": content_type,
            "size":         size,
            "preview":      preview,
            "mode":         mode,
        })
    except requests.exceptions.Timeout:
        return jsonify({"error": "timeout", "url": url}), 504
    except requests.exceptions.ConnectionError as exc:
        return jsonify({"error": "connection error", "detail": str(exc), "url": url}), 503
    except Exception as exc:
        return jsonify({"error": str(exc), "url": url}), 500


# ── Ollama direct proxy ───────────────────────────────────────────────────────

@app.route("/api/ollama/tags")
def ollama_tags():
    if not _REQUESTS:
        return jsonify({"models": []}), 200
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        return Response(r.content, content_type="application/json")
    except Exception as exc:
        return jsonify({"error": str(exc), "models": []}), 200


@app.route("/api/ollama/generate", methods=["POST"])
def ollama_generate():
    if not _REQUESTS:
        return jsonify({"error": "requests not installed"}), 500
    try:
        r = requests.post(
            "http://localhost:11434/api/generate",
            json=request.get_json(), timeout=120,
        )
        return Response(r.content, content_type="application/json")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


# ── Quick actions ─────────────────────────────────────────────────────────────

@app.route("/api/action/restart-cloudflared", methods=["POST"])
def restart_cloudflared():
    try:
        if os.name == "nt":
            subprocess.Popen(["sc", "stop", "cloudflared"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(1)
            subprocess.Popen(["sc", "start", "cloudflared"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen(["systemctl", "restart", "cloudflared"])
        return jsonify({"ok": True, "message": "Cloudflared restarting…"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/action/start-brain", methods=["POST"])
def start_brain():
    try:
        script = os.path.join(BASE_DIR, "argos_brain_api.py")
        if os.path.exists(script):
            subprocess.Popen([sys.executable, script],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return jsonify({"ok": True, "message": "Brain API starting…"})
        return jsonify({"ok": False, "error": "argos_brain_api.py not found"}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/action/restart-brain", methods=["POST"])
def restart_brain():
    # Kill existing brain process then restart
    try:
        if _PSUTIL:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "argos_brain_api" in cmdline:
                    proc.terminate()
        return start_brain()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/action/run-skill", methods=["POST"])
def run_skill():
    data  = request.get_json(silent=True) or {}
    skill = data.get("skill", "")
    if not skill:
        return jsonify({"error": "skill required"}), 400
    return _proxy(f"skills/{skill}/run", "POST")


@app.route("/api/action/start-vm", methods=["POST"])
def start_vm():
    data = request.get_json(silent=True) or {}
    vm   = data.get("vm", "")
    if not vm:
        return jsonify({"error": "vm required"}), 400
    return _proxy(f"vms/{vm}/start", "POST")


# ── WireGuard peers proxy ─────────────────────────────────────────────────────

@app.route("/api/wg/peers")
def wg_peers():
    return _proxy("wg/peers")


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    missing = []
    if not _PSUTIL:
        missing.append("psutil")
    if not _REQUESTS:
        missing.append("requests")
    if missing:
        print(f"⚠  Missing packages: {', '.join(missing)}")
        print(f"   Install with: pip install {' '.join(missing)}")

    print(f"ARGOS Master Dashboard: http://0.0.0.0:{PORT}/")
    print(f"Brain API proxy:        {BRAIN_API}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

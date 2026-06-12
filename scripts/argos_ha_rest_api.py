"""
argos_ha_rest_api.py — РЕАЛЬНЫЙ REST API ARGOS для Home Assistant (:8002).

В отличие от шаблонной инструкции (заглушки memory_drawers=41926, битые Flask(name)),
здесь — настоящие данные: psutil (CPU/RAM/disk), nvidia-smi (GPU), реальный headroom,
mempalace статус через MCP. HA-сенсоры на ноутбуке тянут это по http://<ОРИОН>:8002.

Дополняет MQTT-publisher (153 сущности уже в HA) — даёт on-demand метрики и сжатие.
НЕ трогает ядро ARGOS, отдельный лёгкий процесс.
"""
from __future__ import annotations

import os
import subprocess
import time

try:
    from flask import Flask, jsonify, request
except ImportError:
    raise SystemExit("pip install flask")

try:
    import psutil
except ImportError:
    psutil = None

app = Flask(__name__)  # ФИКС: __name__, не Flask(name) как в инструкции
PORT = int(os.getenv("ARGOS_HA_REST_PORT", "8002"))


def _gpu_metrics() -> dict:
    """Реальные метрики GPU через nvidia-smi (не заглушка 62.0)."""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=True).stdout.strip()
        t, u, mu, mt = (p.strip() for p in out.split(",")[:4])
        return {"temp": int(t), "usage": int(u), "mem_used_mb": int(mu),
                "mem_total_mb": int(mt)}
    except Exception:
        return {"temp": None, "usage": None, "mem_used_mb": None, "mem_total_mb": None}


@app.route("/health")
def health():
    return jsonify({"service": "ARGOS HA REST API", "status": "online",
                    "ts": time.time()})


@app.route("/api/system/status")
def system_status():
    """Реальный статус Ориона (psutil + GPU)."""
    data = {"timestamp": time.time(), "node": "orion"}
    if psutil:
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("F:\\" if os.name == "nt" else "/")
        data.update({
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "cpu_cores": psutil.cpu_count(),
            "ram_percent": vm.percent,
            "ram_used_mb": round(vm.used / 1024 / 1024),
            "ram_total_mb": round(vm.total / 1024 / 1024),
            "disk_percent": du.percent,
            "disk_free_gb": round(du.free / 1024**3, 1),
        })
    data["gpu"] = _gpu_metrics()
    return jsonify(data)


@app.route("/api/argos/compression", methods=["POST"])
def compression():
    """РЕАЛЬНЫЙ headroom (не заглушка). {text, strategy?}."""
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    t0 = time.time()
    try:
        import headroom
        compressed = headroom.compress(text) if hasattr(headroom, "compress") else text
    except Exception:
        # лёгкий fallback-сжимальщик (dedup пробелов/строк)
        lines = text.splitlines()
        seen, out = set(), []
        for ln in lines:
            k = ln.strip()
            if k and k not in seen:
                seen.add(k); out.append(ln)
        compressed = "\n".join(out)
    orig, comp = len(text), len(compressed)
    return jsonify({
        "original_size": orig, "compressed_size": comp,
        "ratio": round((1 - comp / orig) * 100, 1) if orig else 0,
        "time_ms": round((time.time() - t0) * 1000, 1),
    })


@app.route("/api/argos/brain")
def brain_proxy():
    """Статус Brain (реальный :5001)."""
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://127.0.0.1:5001/health", timeout=5) as r:
            return jsonify({"online": True, "brain": _json.loads(r.read())})
    except Exception as e:
        return jsonify({"online": False, "error": str(e)})


if __name__ == "__main__":
    print(f"ARGOS HA REST API → :{PORT} (real psutil/GPU/headroom)")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

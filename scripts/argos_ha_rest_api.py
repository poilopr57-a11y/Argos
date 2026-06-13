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
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

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
ROOT = Path(__file__).resolve().parents[1]
MCP_URL = os.getenv("ARGOS_MCP_URL", "http://127.0.0.1:8000/mcp")
BRAIN_URL = os.getenv("ARGOS_BRAIN_URL", "http://127.0.0.1:5001").rstrip("/")
FPGA_PCI_VENDOR = os.getenv("ARGOS_FPGA_VENDOR_ID", "10EE").upper()
FPGA_PCI_DEVICE = os.getenv("ARGOS_FPGA_DEVICE_ID", "7022").upper()
PIHOLE_URL = os.getenv("ARGOS_PIHOLE_URL", os.getenv("PIHOLE_URL", "")).rstrip("/")
PIHOLE_TOKEN = os.getenv("ARGOS_PIHOLE_TOKEN", os.getenv("PIHOLE_TOKEN", ""))
_FPGA_CACHE: dict = {}
_FPGA_TS: float = 0.0
_DNS_CACHE: list[str] = []
_DNS_TS: float = 0.0


@app.after_request
def _cors(resp):
    """Allow HA/Lovelace/browser cards to call this small local API."""
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return resp


def _run_cmd(args: list[str], timeout: float = 6.0) -> dict:
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
        }
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "timeout"}
    except Exception as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def _http_json(url: str, method: str = "GET", payload: dict | None = None, timeout: float = 8.0) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": resp.status, "data": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return {"ok": False, "status": exc.code, "error": raw[:500] or str(exc)}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


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


def _disk_root() -> str:
    configured = os.getenv("ARGOS_STATUS_DISK")
    if configured and os.path.exists(configured):
        return configured
    if os.name == "nt":
        for candidate in ("F:\\", "C:\\"):
            if os.path.exists(candidate):
                return candidate
    return "/"


def _system_status_data() -> dict:
    """Real Orion status for HA sensors and ARGOS context."""
    data = {"timestamp": time.time(), "node": "orion"}
    if psutil:
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(_disk_root())
        data.update({
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "cpu_cores": psutil.cpu_count(),
            "ram_percent": vm.percent,
            "ram_used_mb": round(vm.used / 1024 / 1024),
            "ram_total_mb": round(vm.total / 1024 / 1024),
            "disk_percent": du.percent,
            "disk_free_gb": round(du.free / 1024**3, 1),
        })
    else:
        data["error"] = "psutil not installed"
    data["gpu"] = _gpu_metrics()
    return data


def _headroom_stats_data() -> dict:
    try:
        from src.headroom_bridge import get_headroom_bridge

        stats = get_headroom_bridge().stats()
        stats["online"] = True
        return stats
    except Exception as exc:
        return {"online": False, "error": str(exc)[:300]}


def _compress_payload(body: dict) -> dict:
    from src.headroom_bridge import get_headroom_bridge

    text = body.get("text", "")
    if not text:
        raise ValueError("text required")
    return get_headroom_bridge().compress(
        text,
        max_chars=int(body.get("max_chars", 4000) or 4000),
        tool_name=str(body.get("tool_name") or "ha_rest.compression"),
        store=bool(body.get("store", True)),
    )


def _mcp_tool(name: str, arguments: dict, timeout: float = 35.0) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 1000000,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    result = _http_json(MCP_URL, method="POST", payload=payload, timeout=timeout)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "MCP unavailable"), "raw": result}
    data = result.get("data") or {}
    try:
        content = data["result"]["content"]
        text = content[0].get("text", "") if content else ""
    except Exception:
        text = json.dumps(data, ensure_ascii=False)
    return {"ok": True, "text": text, "raw": data}


def _brain_status_data() -> dict:
    result = _http_json(f"{BRAIN_URL}/health", timeout=5)
    if result.get("ok"):
        return {"online": True, "brain": result.get("data", {})}
    return {"online": False, "error": result.get("error", "")}


def _argos_memory_data() -> dict:
    headroom = _headroom_stats_data()
    configured_drawers = os.getenv("ARGOS_MEMORY_DRAWERS", "")
    return {
        "status": "ok" if headroom.get("online") else "degraded",
        "source": "headroom_bridge",
        "total_drawers": int(configured_drawers) if configured_drawers.isdigit() else None,
        "headroom_items": headroom.get("items", 0),
        "original_chars": headroom.get("original_chars", 0),
        "compressed_chars": headroom.get("compressed_chars", 0),
        "cache_efficiency": headroom.get("savings_pct", 0),
        "store_path": headroom.get("store_path", ""),
        "headroom": headroom,
    }


def _parse_json_maybe(text: str):
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return [data]
    except Exception:
        return []


def _fpga_status_data() -> dict:
    global _FPGA_CACHE, _FPGA_TS
    if _FPGA_CACHE and time.time() - _FPGA_TS < 30:
        return dict(_FPGA_CACHE)

    devices = []
    raw = ""
    if os.name == "nt":
        ps = (
            "$items=Get-PnpDevice | Where-Object {"
            "$_.InstanceId -like '*VEN_10EE*' -or "
            "$_.FriendlyName -like '*Xilinx*' -or "
            "$_.FriendlyName -like '*XDMA*'"
            "}; "
            "$items | Select-Object -First 12 Status,Class,FriendlyName,InstanceId | ConvertTo-Json -Compress"
        )
        r = _run_cmd(["powershell", "-NoProfile", "-Command", ps], timeout=12)
        raw = r.get("stdout", "")
        for item in _parse_json_maybe(raw):
            devices.append({
                "status": item.get("Status"),
                "class": item.get("Class"),
                "name": item.get("FriendlyName"),
                "instance_id": item.get("InstanceId"),
            })
    else:
        r = _run_cmd(["lspci", "-nn"], timeout=8)
        raw = r.get("stdout", "")
        for line in raw.splitlines():
            if "10ee:" in line.lower() or "xilinx" in line.lower():
                devices.append({"status": "OK", "class": "PCI", "name": line, "instance_id": line})

    expected_id = f"VEN_{FPGA_PCI_VENDOR}&DEV_{FPGA_PCI_DEVICE}"
    online = any(str(d.get("status", "")).upper() == "OK" for d in devices)
    xdma_detected = any(
        "XDMA" in str(d.get("name", "")).upper()
        or "XILINX" in str(d.get("name", "")).upper()
        or "VEN_10EE" in str(d.get("instance_id", "")).upper()
        for d in devices
    )
    data = {
        "status": "online" if online else "offline",
        "online": online,
        "device": "Artix-7 XDMA accelerator",
        "expected_pci_id": expected_id,
        "pcie": "M.2 PCIe / XDMA",
        "ddr3": "spr2801 DDR3 expected by xdma_ddr3 bitstream",
        "xdma_detected": xdma_detected,
        "devices": devices,
        "reference_designs": [
            "m2-artix7-accelerator-card: projects/xdma, xdma_ddr3, xdma_ddr3_dfx",
            "Xilinx dma_ip_drivers for XDMA/QDMA host driver",
        ],
        "raw": raw[:1200],
        "ts": time.time(),
    }
    _FPGA_CACHE = dict(data)
    _FPGA_TS = time.time()
    return data


def _nn_status_data() -> dict:
    fpga = _fpga_status_data()
    ready = bool(fpga.get("online") and fpga.get("xdma_detected"))
    return {
        "online": ready,
        "backend": "fpga_xdma" if ready else "not_ready",
        "model": os.getenv("ARGOS_FPGA_NN_MODEL", "mnist_cnn_reference"),
        "bitstream": os.getenv("ARGOS_FPGA_BITSTREAM", "xdma_ddr3.bit"),
        "latency_ms": None,
        "throughput_fps": None,
        "fpga": fpga,
        "note": "Inference endpoint is wired for HA/ARGOS diagnostics; real DMA runtime must be attached before production inference.",
    }


def _dns_servers() -> list[str]:
    global _DNS_CACHE, _DNS_TS
    if _DNS_CACHE and time.time() - _DNS_TS < 60:
        return list(_DNS_CACHE)

    if os.name == "nt":
        ps = (
            "$s=Get-DnsClientServerAddress -AddressFamily IPv4 | "
            "ForEach-Object {$_.ServerAddresses} | Where-Object {$_}; "
            "$s | Sort-Object -Unique | ConvertTo-Json -Compress"
        )
        r = _run_cmd(["powershell", "-NoProfile", "-Command", ps], timeout=8)
        raw = r.get("stdout", "")
        try:
            data = json.loads(raw) if raw else []
            if isinstance(data, str):
                _DNS_CACHE = [data]
            else:
                _DNS_CACHE = [str(x) for x in data if x]
            _DNS_TS = time.time()
            return list(_DNS_CACHE)
        except Exception:
            return []
    try:
        servers = []
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("nameserver "):
                    servers.append(line.split()[1])
        _DNS_CACHE = sorted(set(servers))
        _DNS_TS = time.time()
        return list(_DNS_CACHE)
    except Exception:
        return []


def _vpn_status_data() -> dict:
    r = _run_cmd(["wg", "show"], timeout=8)
    stdout = r.get("stdout", "")
    active = bool(r.get("ok") and stdout.strip())
    interface = ""
    peers = 0
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith("interface:"):
            interface = s.split(":", 1)[1].strip()
        elif s.startswith("peer:"):
            peers += 1
    host_ips = []
    try:
        host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    return {
        "vpn_active": active,
        "online": active,
        "interface": interface or os.getenv("ARGOS_VPN_INTERFACE", "wg0"),
        "protocol": "WireGuard",
        "peers": peers,
        "ip": host_ips[0] if host_ips else None,
        "dns": _dns_servers(),
        "error": "" if active else (r.get("stderr") or "wg show returned no active interface"),
    }


def _dns_status_data() -> dict:
    servers = _dns_servers()
    return {
        "status": "configured" if servers else "unknown",
        "dns_leak_detected": None,
        "tested_external": False,
        "dns_servers": servers,
        "note": "External DNS leak test is intentionally not run from this local HA endpoint.",
    }


def _pihole_stats_data() -> dict:
    if not PIHOLE_URL:
        return {"configured": False, "online": False, "error": "ARGOS_PIHOLE_URL/PIHOLE_URL not set"}
    token = f"&auth={PIHOLE_TOKEN}" if PIHOLE_TOKEN else ""
    result = _http_json(f"{PIHOLE_URL}/admin/api.php?summaryRaw{token}", timeout=8)
    if result.get("ok"):
        data = result.get("data", {})
        return {
            "configured": True,
            "online": True,
            "ads_blocked_today": data.get("ads_blocked_today"),
            "queries_today": data.get("dns_queries_today"),
            "block_percentage": data.get("ads_percentage_today"),
            "status": data.get("status", "enabled"),
            "raw": data,
        }
    return {"configured": True, "online": False, "error": result.get("error", "")}


@app.route("/health")
def health():
    return jsonify({"service": "ARGOS HA REST API", "status": "online",
                    "ts": time.time()})


@app.route("/api/system/status")
def system_status():
    """Реальный статус Ориона (psutil + GPU)."""
    return jsonify(_system_status_data())


@app.route("/api/argos/compression", methods=["POST"])
@app.route("/api/argos/compress", methods=["POST"])
@app.route("/api/compress", methods=["POST"])
def compression():
    """Headroom-compatible local compression. {text, max_chars?, store?}."""
    body = request.get_json(silent=True) or {}
    t0 = time.time()
    try:
        data = _compress_payload(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)[:300]}), 500
    return jsonify({
        "status": "ok",
        "id": data.get("id", ""),
        "marker": data.get("marker", ""),
        "strategy": data.get("strategy", ""),
        "original_size": data.get("original_chars", 0),
        "compressed_size": data.get("compressed_chars", 0),
        "ratio": data.get("savings_pct", 0),
        "compressed_text": data.get("compressed_text", ""),
        "time_ms": round((time.time() - t0) * 1000, 1),
    })


@app.route("/api/stats")
@app.route("/api/argos/stats")
@app.route("/api/argos/compression/stats")
def compression_stats():
    """Headroom stats in a HA-friendly shape."""
    data = _headroom_stats_data()
    return jsonify({
        "status": "ok" if data.get("online") else "degraded",
        "items": data.get("items", 0),
        "total_saved": max(0, int(data.get("original_chars", 0) or 0) - int(data.get("compressed_chars", 0) or 0)),
        "compression_ratio": data.get("savings_pct", 0),
        "avg_time_ms": None,
        "store_path": data.get("store_path", ""),
        "strategies": data.get("strategies", {}),
        "raw": data,
    })


@app.route("/api/argos/compression/retrieve/<item_id>")
def compression_retrieve(item_id: str):
    try:
        from src.headroom_bridge import get_headroom_bridge

        start = int(request.args.get("start", "0"))
        length = int(request.args.get("length", "12000"))
        return jsonify(get_headroom_bridge().retrieve(item_id, start=start, length=length))
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)[:300]}), 500


@app.route("/api/argos/memory")
def argos_memory():
    """ARGOS memory/Headroom store status for Home Assistant."""
    return jsonify(_argos_memory_data())


@app.route("/api/argos/query", methods=["POST"])
def argos_query():
    """Query ARGOS through MCP command tool with optional HA/system context."""
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or body.get("text") or "").strip()
    if not query:
        return jsonify({"status": "error", "error": "query required"}), 400

    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    include_context = bool(body.get("include_context", True))
    timeout = float(body.get("timeout", 35) or 35)
    prompt = query
    if include_context:
        system_context = {
            "system": _system_status_data(),
            "memory": _argos_memory_data(),
            "fpga": _fpga_status_data(),
            "vpn": _vpn_status_data(),
            "user_context": context,
        }
        prompt = (
            f"{query}\n\n"
            "[HOME_ASSISTANT_ARGOS_CONTEXT]\n"
            f"{json.dumps(system_context, ensure_ascii=False)[:6000]}"
        )
    result = _mcp_tool("command", {"text": prompt}, timeout=timeout)
    return jsonify({
        "status": "ok" if result.get("ok") else "degraded",
        "query": query,
        "response": result.get("text", ""),
        "mcp_ok": bool(result.get("ok")),
        "error": result.get("error", ""),
        "ts": time.time(),
    })


@app.route("/api/argos/brain")
def brain_proxy():
    """Статус Brain (реальный :5001)."""
    return jsonify(_brain_status_data())


@app.route("/api/fpga/status")
def fpga_status():
    return jsonify(_fpga_status_data())


@app.route("/api/fpga/infer", methods=["POST"])
@app.route("/api/nn/infer", methods=["POST"])
def fpga_infer():
    body = request.get_json(silent=True) or {}
    nn = _nn_status_data()
    payload = body.get("input", "")
    input_size = len(payload) if isinstance(payload, str) else len(json.dumps(payload, ensure_ascii=False))
    if not nn.get("online"):
        return jsonify({
            "status": "not_ready",
            "online": False,
            "input_size": input_size,
            "latency_ms": None,
            "throughput_fps": None,
            "result": None,
            "error": "FPGA/XDMA accelerator is not ready or not detected",
            "nn": nn,
        })
    return jsonify({
        "status": "ready",
        "online": True,
        "input_size": input_size,
        "latency_ms": None,
        "throughput_fps": None,
        "result": None,
        "note": "DMA inference runtime is not attached yet; endpoint is reserved for the FPGA accelerator.",
        "nn": nn,
    })


@app.route("/api/fpga/bitstream", methods=["POST"])
def fpga_bitstream():
    body = request.get_json(silent=True) or {}
    bitstream = (body.get("path") or body.get("bitstream") or "").strip()
    if not bitstream:
        return jsonify({
            "ok": False,
            "safe": True,
            "error": "bitstream path required",
            "note": "Automatic FPGA programming is disabled by default. Use Vivado Hardware Manager or an explicit XDMA loader after verification.",
        }), 400
    path = Path(bitstream)
    if not path.is_absolute():
        path = ROOT / bitstream
    if not path.exists():
        return jsonify({"ok": False, "safe": True, "error": "bitstream not found", "path": str(path)}), 404
    return jsonify({
        "ok": False,
        "safe": True,
        "path": str(path),
        "message": "Bitstream found, but automatic programming is intentionally not executed from HA REST.",
    })


@app.route("/api/nn/status")
def nn_status():
    return jsonify(_nn_status_data())


@app.route("/api/vpn/status")
def vpn_status():
    return jsonify(_vpn_status_data())


@app.route("/api/vpn/pihole/stats")
def pihole_stats():
    return jsonify(_pihole_stats_data())


@app.route("/api/dns/leak-test")
def dns_leak_test():
    return jsonify(_dns_status_data())


@app.route("/api/integration/summary")
def integration_summary():
    return jsonify({
        "status": "ok",
        "system": _system_status_data(),
        "brain": _brain_status_data(),
        "memory": _argos_memory_data(),
        "headroom": _headroom_stats_data(),
        "fpga": _fpga_status_data(),
        "nn": _nn_status_data(),
        "vpn": _vpn_status_data(),
        "dns": _dns_status_data(),
        "pihole": _pihole_stats_data(),
        "ts": time.time(),
    })


if __name__ == "__main__":
    print(f"ARGOS HA REST API -> :{PORT} (real psutil/GPU/headroom)")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

"""
ARGOS Unified Node Registry
============================
Single source of truth for all 40 ARGOS nodes.
Used by brain_heartbeat.py, node_monitor.py, and argos_brain_api.py.

Each node has:
- node_id (unique)
- role (topology role: brain, compute, cloud, edge, ai, bridge, monitor, entity)
- address (host or URL)
- capabilities (list of strings)
- service (systemd service name on laptop, or None if not managed locally)
- health_endpoint (optional URL/path to check real liveness)
- expected (bool) — whether the node is expected to be online
- meta (dict) — human name, group, description
"""
from typing import Dict, Any, List, Optional

NODE_ROLE_BRAIN = "brain"
NODE_ROLE_COMPUTE = "compute"
NODE_ROLE_CLOUD = "cloud"
NODE_ROLE_EDGE = "edge"
NODE_ROLE_AI = "ai"
NODE_ROLE_BRIDGE = "bridge"
NODE_ROLE_MONITOR = "monitor"
NODE_ROLE_ENTITY = "entity"
NODE_ROLE_LOGICAL = "logical"


NODES: Dict[str, Dict[str, Any]] = {
    # ── Core / Brain / Control ───────────────────────────────────────────────
    "argos-laptop": {
        "role": NODE_ROLE_BRAIN,
        "address": "192.168.1.53",
        "capabilities": ["brain-api", "mcp", "ha", "dev", "coordination"],
        "service": "argos-brain-api.service",
        "health_endpoint": "http://192.168.1.53:5001/health",
        "expected": True,
        "meta": {"name": "Нексус (ноутбук)", "group": "core"},
    },
    "argos-pc": {
        "role": NODE_ROLE_COMPUTE,
        "address": "192.168.1.72",
        "capabilities": ["gpu", "llama-server", "telegram-bot", "brain-api", "claude"],
        "service": None,
        "health_endpoint": "http://192.168.1.72:5001/health",
        "expected": True,
        "meta": {"name": "Орион (ПК GPU)", "group": "core"},
    },
    "claude-code": {
        "role": NODE_ROLE_BRAIN,
        "address": "192.168.1.53",
        "capabilities": ["dev", "consciousness", "mcp-client"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Claude Code", "group": "core"},
    },

    # ── Edge / IoT ─────────────────────────────────────────────────────────────
    "orangepi": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.2.168",
        "capabilities": ["iot", "zigbee", "mqtt", "gpio", "reports"],
        "service": None,
        "health_endpoint": "http://192.168.2.168:7777/health",
        "expected": True,
        "meta": {"name": "Эгида (Orange Pi)", "group": "edge"},
    },
    "argos-esp-bridge": {
        "role": NODE_ROLE_BRIDGE,
        "address": "192.168.1.181",
        "capabilities": ["esp8266", "mqtt", "gpio", "uart"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "ESP8266 Bridge", "group": "edge"},
    },
    "argos-esp32-display": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.1.211",
        "capabilities": ["esp32", "display", "mqtt"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "ESP32 Display", "group": "edge"},
    },
    "argos-esp32-cam-01": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.1.0/24",
        "capabilities": ["esp32", "camera", "stream", "edge"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "ESP32-CAM #1 (USB0)", "group": "edge"},
    },
    "argos-esp32-cam-02": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.1.0/24",
        "capabilities": ["esp32", "camera", "stream", "edge"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "ESP32-CAM #2 (USB1)", "group": "edge"},
    },
    "argos-esp32-cam-a5eeb0": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.1.0/24",
        "capabilities": ["esp32", "camera", "stream", "edge"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "ESP32-CAM #1 (USB0, MAC d4:e9:f4:a5:ee:b0)", "group": "edge"},
    },
    "argos-phone-redmi": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.1.188",
        "capabilities": ["mobile", "adb", "sms", "virtualsim"],
        "service": "argos-phone-agent.service",
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "Авангард (Redmi)", "group": "edge"},
    },
    "argos-phone-agent": {
        "role": NODE_ROLE_LOGICAL,
        "address": "192.168.1.53",
        "capabilities": ["adb", "vision", "phone-control", "android"],
        "service": "argos-logical-node-heartbeat.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Phone Agent (logical)", "group": "edge"},
    },
    "vsim-watcher": {
        "role": NODE_ROLE_LOGICAL,
        "address": "192.168.1.53",
        "capabilities": ["sms", "virtualsim", "telegram", "code-reader"],
        "service": "argos-logical-node-heartbeat.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "vSIM Watcher (logical)", "group": "edge"},
    },

    # ── Cloud / GCP ──────────────────────────────────────────────────────────
    "argos-gcp": {
        "role": NODE_ROLE_CLOUD,
        "address": "argos-core-m3gk27ccqa-uc.a.run.app",
        "capabilities": ["cloud", "openai-proxy", "gemini-proxy"],
        "service": None,
        "health_endpoint": "https://argos-core-m3gk27ccqa-uc.a.run.app/health",
        "expected": True,
        "meta": {"name": "GCP Cloud Run Core", "group": "cloud"},
    },
    "gcp-claude": {
        "role": NODE_ROLE_AI,
        "address": "argos-api-server-...",
        "capabilities": ["ai", "claude"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "GCP Claude", "group": "cloud"},
    },
    "gcp-gemini": {
        "role": NODE_ROLE_AI,
        "address": "argos-api-server-...",
        "capabilities": ["ai", "gemini"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "GCP Gemini", "group": "cloud"},
    },
    "gcp-openai": {
        "role": NODE_ROLE_AI,
        "address": "argos-api-server-...",
        "capabilities": ["ai", "openai"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "GCP OpenAI", "group": "cloud"},
    },
    "gcp-cloud-api": {
        "role": NODE_ROLE_CLOUD,
        "address": "argos-api-server-...",
        "capabilities": ["cloud", "api"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "GCP API Server", "group": "cloud"},
    },
    "gcp-cloud-sql": {
        "role": NODE_ROLE_CLOUD,
        "address": "argos-api-server-...",
        "capabilities": ["cloud", "database"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "GCP Cloud SQL", "group": "cloud"},
    },
    "gcp-argos-core": {
        "role": NODE_ROLE_CLOUD,
        "address": "argos-core-...",
        "capabilities": ["cloud", "argos-core"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "GCP Argos Core", "group": "cloud"},
    },

    # ── Cloud / Railway ────────────────────────────────────────────────────
    "argos-railway": {
        "role": NODE_ROLE_CLOUD,
        "address": "argos-v2-production.up.railway.app",
        "capabilities": ["cloud", "telegram-bot", "api"],
        "service": None,
        "health_endpoint": "https://argos-v2-production.up.railway.app/health",
        "expected": True,
        "meta": {"name": "Railway Production", "group": "cloud"},
    },
    "railway-claude": {
        "role": NODE_ROLE_AI,
        "address": "railway",
        "capabilities": ["ai", "claude"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Railway Claude", "group": "cloud"},
    },
    "railway-deepseek": {
        "role": NODE_ROLE_AI,
        "address": "railway",
        "capabilities": ["ai", "deepseek"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Railway DeepSeek", "group": "cloud"},
    },

    # ── AI Entities / Council ────────────────────────────────────────────────
    "entity-argos": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.53:5001",
        "capabilities": ["ai", "iot", "orchestration"],
        "service": "argos-entity-council.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "ARGOS", "group": "entity"},
    },
    "entity-argos-v1": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.53:11434",
        "capabilities": ["ai", "local", "finetuned"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "argos-v1", "group": "entity"},
    },
    "entity-claude": {
        "role": NODE_ROLE_ENTITY,
        "address": "api.anthropic.com",
        "capabilities": ["ai", "claude"],
        "service": "argos-entity-argos_claude_ai_bot.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Клод", "group": "entity"},
    },
    "entity-deepseek": {
        "role": NODE_ROLE_ENTITY,
        "address": "api.deepseek.com",
        "capabilities": ["ai", "deepseek"],
        "service": "argos-entity-argos_deepseek_v3_bot.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Дипсик", "group": "entity"},
    },
    "entity-kimi": {
        "role": NODE_ROLE_ENTITY,
        "address": "api.moonshot.ai",
        "capabilities": ["ai", "kimi"],
        "service": "argos-entity-argos_kimi_ai_bot.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Кими", "group": "entity"},
    },
    "entity-openai": {
        "role": NODE_ROLE_ENTITY,
        "address": "argos-core-m3gk27ccqa-uc.a.run.app/proxy/openai",
        "capabilities": ["ai", "openai"],
        "service": "argos-entity-argos_openai_v3_bot.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "OpenAI", "group": "entity"},
    },
    "entity-gemini": {
        "role": NODE_ROLE_ENTITY,
        "address": "argos-core-m3gk27ccqa-uc.a.run.app/proxy/gemini",
        "capabilities": ["ai", "gemini"],
        "service": "argos-entity-argos_gemini_v3_bot.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Джемини", "group": "entity"},
    },
    "entity-cloudflare": {
        "role": NODE_ROLE_ENTITY,
        "address": "api.cloudflare.com",
        "capabilities": ["ai", "cloudflare"],
        "service": "argos-entity-argos_cloudflare_bot.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Клауд", "group": "entity"},
    },
    "entity-valenok": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.72",
        "capabilities": ["ai", "coding", "pc-agent"],
        "service": "argos-entity-valenok.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Валенок", "group": "entity"},
    },
    "entity-agroklava": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.72",
        "capabilities": ["ai", "coding", "pc-agent"],
        "service": "argos-entity-council.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "АгроКлава", "group": "entity"},
    },
    "entity-coder": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.53",
        "capabilities": ["ai", "coding"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Coder", "group": "entity"},
    },

    # ── Compute / GPU / Inference ────────────────────────────────────────────
    "ollama-pc": {
        "role": NODE_ROLE_COMPUTE,
        "address": "192.168.1.72:11434",
        "capabilities": ["ollama", "gpu", "inference"],
        "service": None,
        "health_endpoint": "http://192.168.1.72:11434/api/tags",
        "expected": False,
        "meta": {"name": "Ollama PC (disabled)", "group": "compute"},
    },
    "rx580-consciousness": {
        "role": NODE_ROLE_COMPUTE,
        "address": "192.168.1.53:8090",
        "capabilities": ["rx580", "consciousness", "vault-reader"],
        "service": "argos-rx580-consciousness.service",
        "health_endpoint": "http://127.0.0.1:8090/health",
        "expected": True,
        "meta": {"name": "RX580 Consciousness", "group": "compute"},
    },

    # ── Business / Special ───────────────────────────────────────────────────
    "argos-business": {
        "role": NODE_ROLE_MONITOR,
        "address": "192.168.1.53",
        "capabilities": ["business", "monitoring", "reports"],
        "service": "argos-business.service",
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "Business Agent", "group": "business"},
    },
    "argos-3xui-vpn": {
        "role": NODE_ROLE_CLOUD,
        "address": "kodama.proxy.rlwy.net:52814",
        "capabilities": ["vpn", "vless", "railway"],
        "service": None,
        "health_endpoint": None,
        "expected": True,
        "meta": {"name": "3x-UI VPN", "group": "cloud"},
    },
    "argos-android": {
        "role": NODE_ROLE_EDGE,
        "address": "192.168.1.0/24",
        "capabilities": ["mobile", "android"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "Android generic", "group": "edge"},
    },
    "cf-brain": {
        "role": NODE_ROLE_CLOUD,
        "address": "api-laptop.argosssss.win",
        "capabilities": ["cloudflare", "tunnel", "proxy"],
        "service": None,
        "health_endpoint": "https://api-laptop.argosssss.win/health",
        "expected": True,
        "meta": {"name": "Cloudflare Brain Tunnel", "group": "cloud"},
    },
    "ngrok-brain": {
        "role": NODE_ROLE_CLOUD,
        "address": "ngrok",
        "capabilities": ["tunnel", "proxy"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "ngrok tunnel", "group": "cloud"},
    },
    "orion-pc": {
        "role": NODE_ROLE_COMPUTE,
        "address": "192.168.1.72",
        "capabilities": ["windows", "gpu", "v100", "hardware"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "Orion PC (alias)", "group": "core"},
    },
    "entity-bio-brain-test-v1": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.53",
        "capabilities": ["ai", "bio-brain", "experimental"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "Bio Brain (experimental)", "group": "entity"},
    },
    "entity-cubbit-storage": {
        "role": NODE_ROLE_ENTITY,
        "address": "192.168.1.53",
        "capabilities": ["ai", "storage", "distributed"],
        "service": None,
        "health_endpoint": None,
        "expected": False,
        "meta": {"name": "Cubbit Storage", "group": "entity"},
    },
}


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    return NODES.get(node_id)


def list_nodes() -> List[Dict[str, Any]]:
    return [dict(v, node_id=k) for k, v in NODES.items()]


def expected_online() -> List[str]:
    return [k for k, v in NODES.items() if v.get("expected")]


def service_map() -> Dict[str, Optional[str]]:
    return {k: v.get("service") for k, v in NODES.items()}


def nodes_by_group(group: str) -> List[str]:
    return [k for k, v in NODES.items() if v.get("meta", {}).get("group") == group]


def nodes_by_role(role: str) -> List[str]:
    return [k for k, v in NODES.items() if v.get("role") == role]


if __name__ == "__main__":
    print(f"ARGOS Unified Registry: {len(NODES)} nodes")
    print(f"  expected online: {len(expected_online())}")
    print(f"  groups: {sorted({v['meta']['group'] for v in NODES.values()})}")
    print(f"  roles: {sorted({v['role'] for v in NODES.values()})}")

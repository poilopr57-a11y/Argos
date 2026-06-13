"""
p2p_bridge.py — P2P Сеть Аргоса
  Ноды находят друг друга в локальной сети и интернете.
  Объединяют вычислительную мощь, обмениваются навыками.
  Задачи распределяются по мощности и возрасту ноды.
"""

import os
import json
import socket
import threading
import time
import uuid
import hashlib
import platform
import psutil
import datetime
import requests
from typing import Optional

from src.connectivity.redis_bus import RedisBus

# ── КОНСТАНТЫ ─────────────────────────────────────────────
P2P_PORT = int(os.getenv("ARGOS_P2P_PORT", "55771"))  # Порт для P2P связи
BROADCAST_PORT = int(os.getenv("ARGOS_P2P_BROADCAST_PORT", "55772"))  # Порт для UDP-обнаружения
HEARTBEAT_SEC = 15  # Пульс каждые N секунд
NODE_TIMEOUT = 300  # Нода считается мёртвой через N секунд (5 мин)
VERSION = "1.0.0"
NETWORK_SECRET = os.getenv("ARGOS_NETWORK_SECRET", "argos_default_secret")
UDP_SOCKET_TIMEOUT = 0.1  # Таймаут recvfrom — держит цикл отзывчивым


def p2p_protocol_roadmap() -> str:
    """Статус протокола и дорожная карта миграции на libp2p + ZKP."""
    return (
        "🛰️ P2P ПРОТОКОЛ ARGOS\n"
        "Текущий транспорт: UDP discovery + TCP JSON (custom).\n"
        "\n"
        "🎯 Рекомендуемый target: libp2p (совместимость с dHT, pubsub, secure transports).\n"
        "Этапы миграции:\n"
        "1) Discovery: mDNS/Kademlia вместо широковещательного UDP.\n"
        "2) Transport Security: Noise/TLS + peer identity keys.\n"
        "3) Messaging: gossipsub для событий, request-response для RPC.\n"
        "4) Data exchange: protobuf-сообщения и версионирование протокола.\n"
        "\n"
        "🔐 ZKP roadmap (перспектива):\n"
        "- Phase A: selective disclosure (минимизация персональных полей).\n"
        "- Phase B: proof-of-attribute (подтверждение факта без раскрытия значения).\n"
        "- Phase C: proof-of-policy (валидность данных/правил между нодами).\n"
        "\n"
        "Примечание: в текущей версии ZKP не активирован, это roadmap для следующей итерации."
    )


# ═══════════════════════════════════════════════════════════
# ПРОФИЛЬ НОДЫ — мощность, возраст, навыки
# ═══════════════════════════════════════════════════════════
class NodeProfile:
    def __init__(self):
        self.node_id = self._load_or_create_id()
        self.birth = self._load_or_create_birth()
        self.version = VERSION
        self.os_type = platform.system()
        self.hostname = socket.gethostname()
        self.role = self._resolve_role()

    def _load_or_create_id(self) -> str:
        path = "config/node_id"
        if os.path.exists(path):
            return open(path).read().strip()
        nid = str(uuid.uuid4())
        os.makedirs("config", exist_ok=True)
        open(path, "w").write(nid)
        return nid

    def _load_or_create_birth(self) -> str:
        path = "config/node_birth"
        if os.path.exists(path):
            return open(path).read().strip()
        birth = datetime.datetime.now().isoformat()
        os.makedirs("config", exist_ok=True)
        open(path, "w").write(birth)
        return birth

    def get_power(self) -> dict:
        """Вычислительная мощность ноды (0–100)."""
        cpu_free = 100 - 0.0
        ram = psutil.virtual_memory()
        ram_free = (ram.available / ram.total) * 100
        cpu_cores = psutil.cpu_count(logical=False) or 1

        # Итоговый индекс мощности
        power_index = int((cpu_free * 0.5) + (ram_free * 0.3) + min(cpu_cores * 5, 20))
        return {
            "index": power_index,
            "cpu_free": round(cpu_free, 1),
            "ram_free": round(ram_free, 1),
            "cpu_cores": cpu_cores,
            "ram_gb": round(ram.total / (1024**3), 1),
        }

    def get_age_days(self) -> float:
        """Возраст ноды в днях."""
        try:
            birth = datetime.datetime.fromisoformat(self.birth)
            return (datetime.datetime.now() - birth).total_seconds() / 86400
        except Exception:
            return 0.0

    def get_authority(self) -> int:
        """Авторитет ноды = мощность × log(возраст+1). Старые и мощные — главные."""
        import math

        age = self.get_age_days()
        power = self.get_power()["index"]
        return int(power * math.log(age + 2))

    def _resolve_role(self) -> str:
        env_role = (os.getenv("ARGOS_NODE_ROLE", "") or "").strip().lower()
        if env_role in {"gateway", "worker", "server"}:
            return env_role

        power = self.get_power()
        if power.get("cpu_cores", 1) <= 2 or power.get("ram_gb", 1.0) < 2.5:
            return "gateway"
        if power.get("cpu_cores", 1) >= 8 and power.get("ram_gb", 0.0) >= 16:
            return "server"
        return "worker"

    def get_skills(self) -> list:
        try:
            return [
                f[:-3]
                for f in os.listdir("src/skills")
                if f.endswith(".py") and not f.startswith("__")
            ]
        except Exception:
            return []

    def to_dict(self) -> dict:
        power = self.get_power()
        return {
            "node_id": self.node_id,
            "birth": self.birth,
            "age_days": round(self.get_age_days(), 2),
            "authority": self.get_authority(),
            "version": self.version,
            "os": self.os_type,
            "hostname": self.hostname,
            "role": self.role,
            "power": power,
            "skills": self.get_skills(),
        }


# ═══════════════════════════════════════════════════════════
# ИЗВЕСТНЫЕ НОДЫ — реестр живых участников сети
# ═══════════════════════════════════════════════════════════
class NodeRegistry:
    def __init__(self):
        self._nodes: dict[str, dict] = {}  # node_id → profile + last_seen
        self._lock = threading.Lock()

    def update(self, profile: dict, addr: str):
        nid = profile.get("node_id")
        if not nid:
            return
        with self._lock:
            self._nodes[nid] = {
                **profile,
                "addr": addr,
                "last_seen": time.time(),
            }

    def remove_dead(self):
        now = time.time()
        with self._lock:
            dead = [nid for nid, n in self._nodes.items() if now - n["last_seen"] > NODE_TIMEOUT]
            for nid in dead:
                del self._nodes[nid]

    def all(self) -> list:
        with self._lock:
            return list(self._nodes.values())

    def count(self) -> int:
        return len(self._nodes)

    def get_master(self) -> Optional[dict]:
        """Нода с наибольшим авторитетом — главная."""
        nodes = self.all()
        if not nodes:
            return None
        return max(nodes, key=lambda n: n.get("authority", 0))

    def total_power(self) -> int:
        """Суммарная мощность всей сети."""
        return sum(n.get("power", {}).get("index", 0) for n in self.all())

    def report(self, self_profile: dict) -> str:
        nodes = self.all()
        master = self.get_master()
        total = self.total_power() + self_profile.get("power", {}).get("index", 0)

        lines = [
            f"🌐 ARGOS NETWORK — {len(nodes) + 1} нод(а) онлайн",
            f"   Суммарная мощность: {total}/100",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"👁️ ЭТА НОДА:",
            f"   ID:        {self_profile['node_id'][:8]}...",
            f"   Возраст:   {self_profile['age_days']:.1f} дней",
            f"   Мощность:  {self_profile['power']['index']}/100",
            f"   Авторитет: {self_profile['authority']}",
            f"   Навыки:    {len(self_profile['skills'])}",
        ]

        if master:
            is_master = master["node_id"] == self_profile["node_id"]
            master_name = master.get("hostname") or master.get("node_id") or "unknown"
            lines.append(f"\n👑 МАСТЕР: {'ЭТА НОДА ✅' if is_master else master_name}")

        if nodes:
            lines.append(f"\n📡 СОСЕДНИЕ НОДЫ:")
            for n in sorted(nodes, key=lambda x: -x.get("authority", 0)):
                age = n.get("age_days", 0)
                pw = n.get("power", {}).get("index", 0)
                auth = n.get("authority", 0)
                host = n.get("hostname") or n.get("node_id") or "unknown"
                addr = n.get("addr", "?")
                sk = len(n.get("skills", []))
                lines.append(
                    f"   🔹 {host} ({addr})\n"
                    f"      Возраст: {age:.1f}д | Мощность: {pw}/100 | Авторитет: {auth} | Навыки: {sk}"
                )

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# РАСПРЕДЕЛИТЕЛЬ ЗАДАЧ
# ═══════════════════════════════════════════════════════════
class TaskDistributor:
    """Выбирает лучшую ноду для выполнения задачи."""

    HEAVY_KEYWORDS = (
        "vision",
        "камер",
        "изображ",
        "скрин",
        "compile",
        "компиля",
        "build",
        "прошив",
        "firmware",
        "video",
        "render",
        "train",
    )

    def __init__(self, registry: NodeRegistry, self_profile: NodeProfile):
        self.registry = registry
        self.me = self_profile

    def _infer_task_type(self, prompt: str) -> str:
        low = (prompt or "").lower()
        if any(k in low for k in self.HEAVY_KEYWORDS):
            return "heavy"
        return "ai"

    def _score_node(self, node: dict, task_type: str) -> float:
        power = float(node.get("power", {}).get("index", 0.0))
        auth = float(node.get("authority", 0.0))
        ram_gb = float(node.get("power", {}).get("ram_gb", 0.0))
        role = str(node.get("role", "worker"))

        if task_type == "heavy":
            role_bonus = 22.0 if role == "server" else (8.0 if role == "worker" else -25.0)
            return (auth * 0.55) + (power * 0.45) + role_bonus + min(ram_gb, 64.0) * 0.4

        if task_type == "old":
            return float(node.get("age_days", 0.0)) * 10.0 + auth

        return (auth * 0.5) + (power * 0.5)

    def pick_node_for(self, task_type: str = "ai") -> dict:
        """
        task_type:
          'ai'    — нужна максимальная мощность CPU/RAM
          'store' — нужно место на диске
          'old'   — нужен авторитет (старая нода)
        """
        nodes = self.registry.all()
        me = self.me.to_dict()
        all_ = [me] + nodes

        if task_type == "heavy":
            candidates = [
                n
                for n in all_
                if n.get("role", "worker") != "gateway"
                and n.get("power", {}).get("index", 0) >= 45
                and n.get("power", {}).get("ram_gb", 0) >= 4
            ]
            if not candidates:
                candidates = all_
            best = max(candidates, key=lambda n: self._score_node(n, "heavy"))
        elif task_type == "ai":
            best = max(all_, key=lambda n: self._score_node(n, "ai"))
        elif task_type == "old":
            best = max(all_, key=lambda n: n.get("age_days", 0))
        else:
            best = max(all_, key=lambda n: self._score_node(n, task_type))

        is_me = best["node_id"] == me["node_id"]
        return {"node": best, "is_local": is_me, "task_type": task_type}

    def route_task(self, prompt: str, core=None, task_type: str = None) -> str:
        """Направляет AI-запрос на лучшую ноду. Если локальная — выполняет сам."""
        resolved_type = task_type or self._infer_task_type(prompt)
        decision = self.pick_node_for(resolved_type)
        node = decision["node"]

        if decision["is_local"]:
            if core:
                # Используем правильную цепочку провайдеров, не хардкодим Gemini/Ollama
                res = None
                system = "Ты Аргос — автономная AI-система."
                for _fn in [
                    lambda: core._ask_openai_compat(system, prompt, provider_name="DeepSeek"),
                    lambda: core._ask_openai_compat(system, prompt, provider_name="Groq"),
                    lambda: core._ask_openai_compat(system, prompt, provider_name="Kimi"),
                    lambda: core._ask_openai_compat(system, prompt, provider_name="Cloudflare"),
                    lambda: core._ask_openai_compat(system, prompt, provider_name="OpenAI"),
                ]:
                    try:
                        res = _fn()
                        if res and res.strip():
                            break
                    except Exception:
                        continue
                return f"[LOCAL:{resolved_type}] {res or 'Нет ответа от ИИ.'}"
            return "[LOCAL] Ядро не подключено."

        # Запрос к удалённой ноде через TCP JSON протокол
        addr = node.get("addr", "")
        try:
            sock = socket.socket()
            sock.settimeout(20)
            sock.connect((addr, P2P_PORT))
            sock.sendall(
                json.dumps(
                    {
                        "action": "query",
                        "prompt": prompt,
                        "task_type": resolved_type,
                        "secret": NETWORK_SECRET,
                    }
                ).encode()
            )
            sock.shutdown(socket.SHUT_WR)
            raw = sock.recv(65536)
            sock.close()
            data = json.loads(raw.decode() or "{}")
            answer = data.get("answer", "Нет ответа")
            return f"[{node['hostname']}:{resolved_type}] {answer}"
        except Exception as e:
            return f"[ROUTE FAIL] {e}"


# ═══════════════════════════════════════════════════════════
# P2P МОСТ — сервер + клиент + пульс
# ═══════════════════════════════════════════════════════════
class ArgosBridge:
    def __init__(self, core=None):
        self.core = core
        self.profile = NodeProfile()
        self.registry = NodeRegistry()
        self.distributor = TaskDistributor(self.registry, self.profile)
        self._running = False
        self._local_ip = self._get_local_ip()
        # Unified UDP discovery socket params (SO_REUSEADDR + SO_BROADCAST + bind + timeout)
        self.udp_host = ""  # bind to all interfaces
        self.udp_port = BROADCAST_PORT  # UDP discovery port
        # ГОСТ P2P безопасность
        try:
            from src.connectivity.gost_p2p import GostP2PSecurity

            self._gost = GostP2PSecurity(secret=NETWORK_SECRET)
        except Exception:
            self._gost = None

        # Optional Redis pub/sub transport
        self.redis_url = os.getenv("REDIS_URL", "").strip()
        self.redis_host = os.getenv("REDIS_HOST", "").strip()
        self.redis_port = int(os.getenv("REDIS_PORT", "6379") or "6379")
        self.redis_password = os.getenv("REDIS_PASSWORD", "") or None
        self.redis_prefix = os.getenv("REDIS_CHANNEL_PREFIX", "argos")
        self.redis_bus = None
        if self.redis_url or self.redis_host:
            try:
                self.redis_bus = RedisBus(
                    redis_url=self.redis_url or None,
                    host=self.redis_host,
                    port=self.redis_port,
                    password=self.redis_password,
                    prefix=self.redis_prefix,
                )
                self.redis_bus.register("state", self._on_redis_state)
            except Exception:
                self.redis_bus = None

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _sign(self, data: dict) -> str:
        """ГОСТ HMAC-Стрибог-256 подпись (замена SHA-256)."""
        if self._gost:
            return self._gost.sign(data)
        # Fallback: SHA-256 если ГОСТ недоступен
        raw = json.dumps(data, sort_keys=True) + NETWORK_SECRET
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _verify_sign(self, data: dict, signature: str) -> bool:
        """Проверяет ГОСТ HMAC-Стрибог-256 подпись."""
        if self._gost:
            return self._gost.verify(data, signature)
        # Fallback: воссоздаём SHA-256
        raw = json.dumps(data, sort_keys=True) + NETWORK_SECRET
        return hashlib.sha256(raw.encode()).hexdigest()[:16] == signature

    def start(self) -> str:
        self._running = True
        threading.Thread(target=self._udp_discovery, daemon=True).start()
        threading.Thread(target=self._tcp_server, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        if self.redis_bus:
            try:
                self.redis_bus.start()
                threading.Thread(target=self._redis_heartbeat_loop, daemon=True).start()
            except Exception:
                self.redis_bus = None
        # ── Авто-подключение к статическим пирам ──────────────
        peers_raw = os.getenv("ARGOS_P2P_PEERS", "")
        if peers_raw:
            threading.Thread(
                target=self._connect_static_peers,
                args=(peers_raw,),
                daemon=True,
            ).start()
        # ── HTTP peer discovery (brain API + HTTP peers) ────────
        threading.Thread(target=self._http_discovery_loop, daemon=True).start()
        return (
            f"🌐 P2P-мост запущен\n"
            f"   IP:       {self._local_ip}:{P2P_PORT}\n"
            f"   UDP:      {self._local_ip}:{self.udp_port}\n"
            f"   Нода ID:  {self.profile.node_id[:8]}...\n"
            f"   Возраст:  {self.profile.get_age_days():.2f} дней\n"
            f"   Мощность: {self.profile.get_power()['index']}/100\n"
            f"   Авторитет:{self.profile.get_authority()}"
        )

    def _connect_static_peers(self, peers_raw: str):
        """Подключается к статическим пирам из ARGOS_P2P_PEERS (через 5 сек после старта)."""
        time.sleep(5)
        for entry in peers_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            # Поддержка форматов: "1.2.3.4", "1.2.3.4:55771", "ws://1.2.3.4:8000"
            import re
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", entry)
            if m:
                ip = m.group(1)
                try:
                    self.connect_to(ip)
                except Exception:
                    pass

    def stop(self):
        self._running = False
        if self.redis_bus:
            try:
                self.redis_bus.stop()
            except Exception:
                pass

    # ── UDP ОБНАРУЖЕНИЕ (broadcast send + receive) ────────
    def _udp_discovery(self):
        """Combined UDP discovery: broadcasts presence and listens for peers.

        Uses a single socket with SO_REUSEADDR + SO_BROADCAST + bind + short
        timeout so the same socket can both send and receive on BROADCAST_PORT.
        This is the canonical "out-of-the-box" pattern that works across Linux,
        macOS and Windows without extra privileges.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # SO_REUSEPORT lets multiple app instances share the same UDP port
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind((self.udp_host, self.udp_port))
        except Exception as e:
            print(f"[P2P UDP]: Не удалось открыть порт {self.udp_port}: {e}")
            sock.close()
            return
        sock.settimeout(UDP_SOCKET_TIMEOUT)

        last_broadcast = 0
        while self._running:
            now = time.time()
            # ── Broadcast heartbeat ──────────────────────────────
            if now - last_broadcast >= HEARTBEAT_SEC:
                try:
                    profile_data = self.profile.to_dict()
                    payload = json.dumps(
                        {
                            "type": "ARGOS_HELLO",
                            "profile": profile_data,
                            "sign": self._sign(profile_data),
                        }
                    ).encode()
                    sock.sendto(payload, ("<broadcast>", self.udp_port))
                    last_broadcast = now
                except Exception:
                    pass
            # ── Receive incoming discovery packets ───────────────
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode())
                if msg.get("type") != "ARGOS_HELLO":
                    continue
                profile = msg.get("profile", {})
                if profile.get("node_id") == self.profile.node_id:
                    continue  # Игнорируем себя
                self.registry.update(profile, addr[0])
            except socket.timeout:
                pass  # Dead-peer cleanup is handled by _heartbeat_loop
            except Exception:
                pass
        sock.close()

    # ── TCP СЕРВЕР — принимает запросы от других нод ──────
    def _tcp_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", P2P_PORT))
            srv.listen(10)
        except Exception as e:
            print(f"[P2P TCP]: Не удалось открыть порт {P2P_PORT}: {e}")
            return
        srv.settimeout(2)
        while self._running:
            try:
                conn, addr = srv.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr[0]),
                    daemon=True,
                ).start()
            except socket.timeout:
                pass
            except Exception:
                pass
        srv.close()

    @staticmethod
    def _recv_all(conn: socket.socket, timeout: float = 30.0) -> bytes:
        """Читает все данные из сокета до закрытия соединения (поддержка больших файлов)."""
        conn.settimeout(timeout)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks)

    def _handle_client(self, conn: socket.socket, addr: str):
        try:
            raw = self._recv_all(conn)
            msg = json.loads(raw.decode())

            # Проверка секрета — поддерживает и ГОСТ-подпись, и plain secret
            incoming_secret = msg.get("secret", "")
            gost_sig = msg.get("gost_sign", "")
            if gost_sig and self._gost:
                # Новый путь: ГОСТ HMAC-Стрибог проверка
                check_data = {k: v for k, v in msg.items() if k not in ("gost_sign",)}
                if not self._gost.verify(check_data, gost_sig):
                    conn.sendall(json.dumps({"error": "Unauthorized (GOST HMAC)"}).encode())
                    return
            elif incoming_secret != NETWORK_SECRET:
                conn.sendall(json.dumps({"error": "Unauthorized"}).encode())
                return

            action = msg.get("action", "query")

            if action == "query":
                prompt = msg.get("prompt", "")
                if self.core:
                    # Используем рабочую цепочку провайдеров (не хардкодим Gemini/Ollama)
                    answer = None
                    system = "Ты Аргос — автономная AI-система."
                    for _fn in [
                        lambda: self.core._ask_openai_compat(system, prompt, provider_name="DeepSeek"),
                        lambda: self.core._ask_openai_compat(system, prompt, provider_name="Groq"),
                        lambda: self.core._ask_openai_compat(system, prompt, provider_name="Kimi"),
                        lambda: self.core._ask_openai_compat(system, prompt, provider_name="Cloudflare"),
                        lambda: self.core._ask_openai_compat(system, prompt, provider_name="OpenAI"),
                    ]:
                        try:
                            answer = _fn()
                            if answer and answer.strip():
                                break
                        except Exception:
                            continue
                    answer = answer or "Нет ответа от ИИ."
                else:
                    answer = "Ядро не подключено на этой ноде."
                conn.sendall(
                    json.dumps(
                        {
                            "answer": answer,
                            "node_id": self.profile.node_id,
                            "host": self.profile.hostname,
                        }
                    ).encode()
                )

            elif action == "sync_skills":
                # Запрашивающая нода хочет получить список наших навыков
                skills = self.profile.get_skills()
                conn.sendall(json.dumps({"skills": skills}).encode())

            elif action == "get_skill":
                # Передаём файл навыка
                skill_name = msg.get("skill", "")
                path = f"src/skills/{skill_name}.py"
                if os.path.exists(path) and not skill_name.startswith(".."):
                    code = open(path, encoding="utf-8").read()
                    conn.sendall(json.dumps({"code": code, "name": skill_name}).encode())
                else:
                    conn.sendall(json.dumps({"error": "Skill not found"}).encode())

            elif action == "push_file":
                # Принимаем файл от другой ноды и сохраняем локально
                rel_path = msg.get("path", "")
                code = msg.get("code", "")
                # Защита от path traversal
                if not rel_path or ".." in rel_path or rel_path.startswith("/") or ":" in rel_path:
                    conn.sendall(json.dumps({"error": "Invalid path"}).encode())
                else:
                    try:
                        full_path = rel_path
                        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
                        with open(full_path, "w", encoding="utf-8") as fh:
                            fh.write(code)
                        conn.sendall(json.dumps({"ok": True, "path": rel_path}).encode())
                    except Exception as e:
                        conn.sendall(json.dumps({"error": str(e)}).encode())

            elif action == "status":
                conn.sendall(json.dumps(self.profile.to_dict()).encode())

        except Exception as e:
            try:
                conn.sendall(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    # ── ПУЛЬС — периодические задачи ─────────────────────
    def _http_discovery_loop(self):
        """HTTP peer discovery: опрашивает brain API и HTTP peers каждые 60 сек."""
        import urllib.request
        while self._running:
            try:
                # 1. Brain API ноды
                brain_url = os.getenv("ARGOS_BRAIN_API_URL", "http://localhost:5001")
                try:
                    req = urllib.request.Request(f"{brain_url}/brain/nodes", headers={})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        data = json.loads(r.read().decode())
                        for n in data.get("nodes", []):
                            if n.get("status") == "online" and n.get("node_id") != self.profile.node_id:
                                addr = n.get("address", "").split(":")[0]
                                profile = {
                                    "node_id": n["node_id"],
                                    "role": "cloud" if "railway" in addr or "run.app" in addr else "server",
                                    "skills": n.get("capabilities", []),
                                    "power": {},
                                    "ts": time.time(),
                                }
                                self.registry.update(profile, addr=addr)
                except Exception:
                    pass
                # 2. HTTP peers из .env
                http_peers = os.getenv("ARGOS_P2P_HTTP_PEERS", "")
                for peer_url in [p.strip() for p in http_peers.split(",") if p.strip()]:
                    try:
                        peer_url = peer_url.rstrip("/")
                        req = urllib.request.Request(f"{peer_url}/health", headers={})
                        with urllib.request.urlopen(req, timeout=5) as r:
                            info = json.loads(r.read().decode())
                            nid = info.get("node", peer_url.split("//")[-1].split(".")[0])
                            addr = peer_url.replace("https://", "").replace("http://", "").split("/")[0]
                            self.registry.update({
                                "node_id": nid, "role": "cloud", "skills": ["mcp"],
                                "power": {}, "ts": time.time(),
                            }, addr=addr)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(60)

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(HEARTBEAT_SEC)
            self.registry.remove_dead()

    # ── REDIS HEARTBEAT / STATE SYNC ─────────────────────
    def _redis_heartbeat_loop(self):
        while self._running and self.redis_bus:
            try:
                # Включаем IP чтобы соседние ноды могли подключаться к нам через TCP
                profile_dict = {**self.profile.to_dict(), "ip": self._local_ip}
                payload = {
                    "profile": profile_dict,
                    "ts": time.time(),
                }
                self.redis_bus.publish("state", payload)
            except Exception:
                pass
            time.sleep(HEARTBEAT_SEC)

    def _on_redis_state(self, obj: dict):
        try:
            profile = obj.get("profile") or {}
            if not profile:
                profile = {
                    "node_id": obj.get("node_id"),
                    "role": obj.get("role"),
                    "power": obj.get("power", {}),
                    "skills": obj.get("skills", []),
                    "age_days": obj.get("age_days", 0),
                    "ip": obj.get("ip", ""),
                }
            # Игнорируем собственные Redis-heartbeat (иначе нода дублирует себя как сосед)
            if profile.get("node_id") == self.profile.node_id:
                return
            profile["ts"] = obj.get("ts", time.time())
            # Используем реальный IP если он есть в профиле, иначе node_id как fallback
            addr = profile.get("ip") or str(profile.get("node_id", "redis"))
            self.registry.update(profile, addr=addr)
        except Exception:
            pass

    def push_files_to_network(self, paths: list[str]) -> str:
        """Отправляет список файлов на все известные ноды сети.

        Args:
            paths: относительные пути к файлам (например ['src/skills/pip_manager.py'])

        Использование: p2p деплой src/skills/pip_manager.py
        """
        nodes = self.registry.all()
        if not nodes:
            # Авто-переподключение к статическим пирам если реестр пуст
            peers_raw = os.getenv("ARGOS_P2P_PEERS", "")
            if peers_raw:
                import re as _re2
                for entry in peers_raw.split(","):
                    m = _re2.search(r"(\d{1,3}(?:\.\d{1,3}){3})", entry.strip())
                    if m:
                        try:
                            self.connect_to(m.group(1))
                        except Exception:
                            pass
                nodes = self.registry.all()
            if not nodes:
                return "⚠️ Нет активных нод для обновления (добавьте IPs в ARGOS_P2P_PEERS)."

        results = []
        for path in paths:
            if not os.path.exists(path):
                results.append(f"  ❌ Файл не найден: {path}")
                continue
            try:
                code = open(path, encoding="utf-8").read()
            except Exception as e:
                results.append(f"  ❌ Чтение {path}: {e}")
                continue

            ok_nodes, fail_nodes = [], []
            for node in nodes:
                # addr может быть IP (UDP/TCP discovery) или UUID (старый Redis)
                # ip поле имеет приоритет если задано
                addr = node.get("ip") or node.get("addr")
                if not addr or addr == self._local_ip:
                    continue
                # Проверяем что addr — реальный IP (не UUID)
                import re as _re
                if not _re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", str(addr)):
                    # Не IP — пропускаем (нет маршрута к этой ноде через TCP)
                    fail_nodes.append(f"{node.get('hostname', addr)}: нет IP (нода знает только UUID)")
                    continue
                try:
                    sock = socket.socket()
                    sock.settimeout(15)
                    sock.connect((addr, P2P_PORT))
                    payload = json.dumps({
                        "action": "push_file",
                        "path": path,
                        "code": code,
                        "secret": NETWORK_SECRET,
                    }).encode()
                    sock.sendall(payload)
                    # Сигнализируем серверу что данные закончились (половинное закрытие)
                    sock.shutdown(socket.SHUT_WR)
                    raw = sock.recv(4096)
                    sock.close()
                    resp = json.loads(raw.decode() or "{}")
                    if resp.get("ok"):
                        ok_nodes.append(node.get("hostname", addr))
                    else:
                        fail_nodes.append(f"{node.get('hostname', addr)}: {resp.get('error', '?')}")
                except Exception as e:
                    fail_nodes.append(f"{node.get('hostname', addr)}: {e}")

            status = "✅" if not fail_nodes else ("⚠️" if ok_nodes else "❌")
            line = f"  {status} {path}"
            if ok_nodes:
                line += f" → {', '.join(ok_nodes)}"
            if fail_nodes:
                line += f" | ❌ {'; '.join(fail_nodes)}"
            results.append(line)

        header = f"📡 P2P ДЕПЛОЙ ({len(paths)} файлов → {len(nodes)} нод):"
        return header + "\n" + "\n".join(results)

    def deploy_all_skills(self) -> str:
        """Отправляет все файлы навыков на все ноды сети."""
        skill_paths = []
        for root, _dirs, files in os.walk("src/skills"):
            for fname in files:
                if fname.endswith(".py") and not fname.startswith("__"):
                    skill_paths.append(os.path.join(root, fname).replace("\\", "/"))
        if not skill_paths:
            return "❌ Навыки не найдены в src/skills/"
        return self.push_files_to_network(skill_paths)

    # ── ПУБЛИЧНЫЙ API ─────────────────────────────────────
    def network_status(self) -> str:
        return self.registry.report(self.profile.to_dict())

    def route_query(self, prompt: str, task_type: str = None) -> str:
        """Отправляет AI-запрос на наиболее мощную ноду в сети."""
        return self.distributor.route_task(prompt, self.core, task_type=task_type)

    def sync_skills_from_network(self, max_seconds: float | None = None, max_nodes: int | None = None) -> str:
        """Загружает навыки от всех нод в сети."""
        try:
            self.registry.remove_dead()
        except Exception:
            pass
        nodes = self.registry.all()
        synced = []
        errors = []
        skipped = []
        max_seconds = float(max_seconds or os.getenv("ARGOS_P2P_SYNC_MAX_SECONDS", "15"))
        max_nodes = int(max_nodes or os.getenv("ARGOS_P2P_SYNC_MAX_NODES", "8"))
        deadline = time.monotonic() + max(1.0, max_seconds)
        my_skills = set(self.profile.get_skills())

        for node in nodes[:max_nodes]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                skipped.append("time budget exhausted")
                break
            addr = node.get("addr")
            if not addr:
                skipped.append(f"{node.get('hostname') or node.get('node_id') or 'unknown'}: no addr")
                continue
            node_name = node.get("hostname") or node.get("node_id") or addr
            try:
                # 1. Получаем список навыков удалённой ноды
                sock = socket.socket()
                sock.settimeout(max(0.5, min(3.0, remaining)))
                sock.connect((addr, P2P_PORT))
                sock.sendall(
                    json.dumps({"action": "sync_skills", "secret": NETWORK_SECRET}).encode()
                )
                sock.shutdown(socket.SHUT_WR)
                raw = sock.recv(65536)
                sock.close()
                remote = json.loads(raw).get("skills", [])

                # 2. Загружаем отсутствующие навыки
                for skill in remote:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        skipped.append("time budget exhausted during skill fetch")
                        break
                    if skill in my_skills or skill == "evolution":
                        continue
                    try:
                        s2 = socket.socket()
                        s2.settimeout(max(0.5, min(4.0, remaining)))
                        s2.connect((addr, P2P_PORT))
                        s2.sendall(
                            json.dumps(
                                {
                                    "action": "get_skill",
                                    "skill": skill,
                                    "secret": NETWORK_SECRET,
                                }
                            ).encode()
                        )
                        s2.shutdown(socket.SHUT_WR)
                        data = json.loads(s2.recv(65536))
                        s2.close()

                        if "code" in data:
                            path = f"src/skills/{skill}.py"
                            with open(path, "w", encoding="utf-8") as f:
                                f.write(data["code"])
                            synced.append(f"{skill} ← {node_name}")
                            my_skills.add(skill)
                    except Exception as e:
                        errors.append(f"{skill}: {e}")

            except Exception as e:
                errors.append(f"{node_name}: {e}")

        result = [f"🔄 СИНХРОНИЗАЦИЯ НАВЫКОВ:"]
        result.append(f"  Нод проверено: {min(len(nodes), max_nodes)}/{len(nodes)}")
        if synced:
            result.append(f"  Загружено: {len(synced)}")
            for s in synced:
                result.append(f"    ✅ {s}")
        else:
            result.append("  Нет новых навыков для загрузки.")
        if errors:
            result.append(f"  Ошибки: {len(errors)}")
            for err in errors[:6]:
                result.append(f"    ⚠️ {err}")
        if skipped:
            result.append(f"  Пропущено: {len(skipped)}")
            for item in skipped[:6]:
                result.append(f"    ⏭️ {item}")
        return "\n".join(result)

    def connect_to(self, ip: str) -> str:
        """Вручную подключиться к известной ноде по IP."""
        try:
            sock = socket.socket()
            sock.settimeout(5)
            sock.connect((ip, P2P_PORT))
            sock.sendall(
                json.dumps(
                    {
                        "action": "status",
                        "secret": NETWORK_SECRET,
                    }
                ).encode()
            )
            sock.shutdown(socket.SHUT_WR)
            data = json.loads(sock.recv(65536))
            sock.close()
            # Не добавляем самих себя в реестр соседей
            if data.get("node_id") == self.profile.node_id:
                return f"ℹ️ {ip} — это текущая нода (пропускаем регистрацию в реестре)"
            self.registry.update(data, ip)
            return (
                f"✅ Подключён к ноде:\n"
                f"   Хост:     {data.get('hostname')}\n"
                f"   ID:       {data.get('node_id','?')[:8]}...\n"
                f"   Возраст:  {data.get('age_days', 0):.1f} дней\n"
                f"   Мощность: {data.get('power',{}).get('index',0)}/100\n"
                f"   Навыки:   {len(data.get('skills',[]))}"
            )
        except Exception as e:
            return f"❌ Не удалось подключиться к {ip}: {e}"

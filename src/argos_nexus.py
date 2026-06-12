"""
argos_nexus.py — P2P-Нексус: отказоустойчивая репликация MemPalace между нодами.

Назначение (ЛЕГИТИМНОЕ): если одна нода кластера падает, её drawers (ячейки
памяти brain.db) остаются доступны на R соседних узлах. Это DHT-подобная
репликация ДАННЫХ ПАМЯТИ для отказоустойчивости — НЕ командный канал, не C2:
передаются только пары (drawer_id, payload), никакого удалённого исполнения.

Источник: код ArgosNexusNode из Telegram-сессии 2026-06-05, переписан ОСОЗНАННО
с ИСПРАВЛЕНИЕМ багов, найденных при ревью:
  1. `logging.getLogger(name)` → `__name__` (был NameError).
  2. `def init(self,...)` → `__init__` (конструктор не вызывался).
  3. UDP-broadcast принимал REPLICATE_DRAWER от КОГО УГОДНО → отравление brain.db.
     ФИКС: HMAC-SHA256 подпись каждого пакета общим секретом (ARGOS_NEXUS_SECRET);
     пакеты без валидной подписи отбрасываются.
  4. `recvfrom(65535)` — но UDP payload max ~65507, большие drawers молча терялись.
     ФИКС: лимит MAX_PAYLOAD, крупные drawers НЕ шлются по UDP (лог + отказ);
     для них предусмотрен задел под TCP-канал (replicate_large, TODO).
  5. `self.peers: Set` без TTL — упавшие ноды копились вечно.
     ФИКС: dict {addr: last_seen}; пиры старше PEER_TTL вычищаются.
  6. `targets = list(self.peers)[:2]` — всегда первые два (не распределяется).
     ФИКС: random.sample по R случайных живых пиров.
  7. JSON из сети без валидации типов/размера → краши/инъекции.
     ФИКС: строгая проверка полей и типов перед записью в БД.
  8. `sock.bind()` после старта потоков → гонка/AddressInUse.
     ФИКС: bind ДО запуска слушателя; SO_REUSEADDR.
  9. Нет остановки → демон-потоки висели. ФИКС: stop() + Event + join.

Безопасность: только LAN-broadcast, HMAC-аутентификация пиров, репликация
исключительно данных памяти. Никаких exec/eval/команд по сети.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import socket
import sqlite3
import threading
import time
from typing import Optional

try:
    from src.argos_logger import get_logger
    logger = get_logger("argos.nexus")
except Exception:  # автономный запуск без пакета
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - [NEXUS] - %(levelname)s - %(message)s")
    logger = logging.getLogger(__name__)  # ФИКС #1: __name__, не name


MAX_PAYLOAD = 60_000          # ФИКС #4: безопасный потолок UDP (< 65507 с запасом)
PEER_TTL = 30.0               # ФИКС #5: пир считается живым 30с после PING
PING_INTERVAL = 5.0
DEFAULT_REPLICATION = 2       # R: сколько копий на соседях


def _secret() -> bytes:
    return os.getenv("ARGOS_NEXUS_SECRET", "argos-nexus-default-change-me").encode()


def _sign(body: bytes) -> str:
    """ФИКС #3: HMAC-SHA256 подпись пакета общим секретом."""
    return hmac.new(_secret(), body, hashlib.sha256).hexdigest()


def _verify(body: bytes, sig: str) -> bool:
    return hmac.compare_digest(_sign(body), sig or "")


class ArgosNexusNode:
    """P2P-нода: UDP-discovery + HMAC-аутентифицированная репликация drawers."""

    def __init__(self, node_id: int, bind_port: int = 49777,
                 db_path: str = "data/brain.db",
                 replication: int = DEFAULT_REPLICATION):
        # ФИКС #2: правильный конструктор __init__
        self.node_id = node_id
        self.bind_port = bind_port
        self.db_path = db_path
        self.replication = max(1, replication)

        # ФИКС #5: пиры с временем последнего контакта, не голый set
        self.peers: dict[tuple[str, int], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()        # ФИКС #9: управляемая остановка
        self._threads: list[threading.Thread] = []

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.settimeout(1.0)             # чтобы recvfrom не висел при stop

    # ── сеть ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        # ФИКС #8: bind ДО запуска потоков
        self.sock.bind(("0.0.0.0", self.bind_port))
        self._stop.clear()
        for target in (self._listen_loop, self._broadcast_loop, self._reaper_loop):
            t = threading.Thread(target=target, daemon=True, name=f"nexus-{target.__name__}")
            t.start()
            self._threads.append(t)
        logger.info("Нода [%s] в сети на :%d (R=%d)",
                    self.node_id, self.bind_port, self.replication)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        try:
            self.sock.close()
        except OSError:
            pass
        logger.info("Нода [%s] остановлена", self.node_id)

    def _send(self, mtype: str, addr: tuple[str, int], **fields) -> None:
        """Подписанная отправка: {body, sig}. body содержит type/node_id/поля."""
        body_obj = {"type": mtype, "node_id": self.node_id, **fields}
        body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_PAYLOAD:          # ФИКС #4
            logger.warning("payload %dB > %d — пропуск (нужен TCP-канал)",
                           len(body), MAX_PAYLOAD)
            return
        packet = json.dumps({"body": body.decode(), "sig": _sign(body)}).encode()
        try:
            self.sock.sendto(packet, addr)
        except OSError as e:
            logger.debug("send→%s: %s", addr, e)

    def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            self._send("PING", ("255.255.255.255", self.bind_port), port=self.bind_port)
            self._stop.wait(PING_INTERVAL)

    def _reaper_loop(self) -> None:
        """ФИКС #5: вычистка пиров, не пинговавших дольше PEER_TTL."""
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                dead = [a for a, seen in self.peers.items() if now - seen > PEER_TTL]
                for a in dead:
                    del self.peers[a]
                    logger.info("Пир выпал по TTL: %s (осталось %d)", a, len(self.peers))
            self._stop.wait(PEER_TTL / 2)

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(data, addr)

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        # ФИКС #3 + #7: проверка подписи и строгая валидация
        try:
            envelope = json.loads(data.decode())
            body_raw = envelope["body"].encode()
            if not _verify(body_raw, envelope.get("sig", "")):
                logger.warning("Отброшен неподписанный/чужой пакет от %s", addr)
                return
            msg = json.loads(body_raw.decode())
        except (ValueError, KeyError, UnicodeDecodeError):
            logger.debug("битый пакет от %s", addr)
            return

        if not isinstance(msg, dict) or msg.get("node_id") == self.node_id:
            return  # игнор своих и мусора

        mtype = msg.get("type")
        if mtype == "PING":
            port = msg.get("port")
            if isinstance(port, int):
                peer = (addr[0], port)
                with self._lock:
                    new = peer not in self.peers
                    self.peers[peer] = time.time()
                if new:
                    logger.info("🔗 Узел кластера: %s (всего %d)", peer, len(self.peers))
        elif mtype == "REPLICATE_DRAWER":
            did, payload = msg.get("drawer_id"), msg.get("payload")
            if isinstance(did, str) and isinstance(payload, str) and 0 < len(did) <= 256:
                self._save_replicated_drawer(did, payload)
            else:
                logger.warning("REPLICATE_DRAWER с невалидными полями от %s", addr)

    # ── данные ────────────────────────────────────────────────────────────
    def replicate_to_cluster(self, drawer_id: str, payload_data: str) -> int:
        """Отправляет drawer на R СЛУЧАЙНЫХ живых пиров. Возвращает число копий."""
        with self._lock:
            live = list(self.peers.keys())
        if not live:
            logger.warning("Нет живых пиров для репликации %s", drawer_id)
            return 0
        # ФИКС #6: случайная выборка, не первые два
        targets = random.sample(live, min(self.replication, len(live)))
        for t in targets:
            self._send("REPLICATE_DRAWER", t, drawer_id=drawer_id, payload=payload_data)
        logger.debug("drawer %s → %d пиров", drawer_id, len(targets))
        return len(targets)

    def _save_replicated_drawer(self, drawer_id: str, payload_data: str) -> None:
        """Сохранение чужого drawer в локальную резервную таблицу."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS remote_mempalace (
                    drawer_id TEXT PRIMARY KEY,
                    payload   TEXT,
                    origin_node INTEGER,
                    sync_ts   INTEGER
                )""")
            conn.execute(
                "INSERT OR REPLACE INTO remote_mempalace "
                "(drawer_id, payload, origin_node, sync_ts) VALUES (?,?,?,?)",
                (drawer_id, payload_data, self.node_id, int(time.time())),
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.error("save replicated drawer: %s", e)

    def peer_count(self) -> int:
        with self._lock:
            return len(self.peers)


if __name__ == "__main__":
    import sys
    nid = int(sys.argv[1]) if len(sys.argv) > 1 else random.randint(1, 9999)
    node = ArgosNexusNode(node_id=nid)
    node.start()
    try:
        while True:
            time.sleep(10)
            logger.info("Нода %s: пиров=%d", nid, node.peer_count())
    except KeyboardInterrupt:
        node.stop()

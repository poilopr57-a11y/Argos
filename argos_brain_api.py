"""
ARGOS AI Brain API
REST API для управления интеллектуальными агентами ARGOS.

Эндпоинты:
  GET  /health                — проверка живости
  GET  /brain/status          — статус ARGOSBrain
  GET  /agents                — список агентов
  POST /agents                — создать агента
  GET  /agents/<id>           — информация об агенте
  POST /think                 — запрос к ArgosCore (с fallback на ARGOSBrain)
  POST /coordinate            — координация агентов
  POST /analyze               — анализ данных
  POST /optimize              — оптимизация
  POST /monitor               — мониторинг
  GET  /p2p/status            — статус P2P-сети
  POST /ollama/train          — запустить обучение argos-v1
  GET  /system/status         — общий статус системы (AI mode, Ollama, P2P, skills)
  POST /brain/register        — регистрация P2P-узла
  POST /brain/heartbeat       — heartbeat P2P-узла
  GET  /brain/nodes           — список P2P-узлов
  DELETE /brain/nodes/<id>    — удалить узел
  GET  /dashboard             — HTML-дашборд
  POST /brain/start           — запустить brain
  POST /brain/stop            — остановить brain
  POST /brain/reset           — сбросить brain
"""
from __future__ import annotations

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(_sys.stderr, "reconfigure"):
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import json
import logging
import os as _os
import subprocess
import shutil
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

# Оригинальный ARGOSBrain (для агентов / координации)
from argos_ai_brain import (
    ARGOSBrain,
    ARGOSAgent,
    AgentRole,
    AgentConfig,
    AzureAIClient,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Глобальные объекты ────────────────────────────────────────────────────────
# Brain инициализируется лениво (before_request) — не блокирует запуск сервера.
brain: Optional[ARGOSBrain] = None

# ArgosCore — ленивый импорт, чтобы не создавать циклических зависимостей.
_core: Optional[Any] = None
_core_init_attempted: bool = False

# [P2P] Реестр узлов: node_id -> node_info
NODE_REGISTRY: Dict[str, Dict[str, Any]] = {}
NODE_TIMEOUT_SECONDS = 90


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _get_core() -> Optional[Any]:
    """Возвращает инстанс ArgosCore (ленивый импорт, singleton)."""
    global _core, _core_init_attempted
    if _core is not None:
        return _core
    if _core_init_attempted:
        return None
    _core_init_attempted = True
    try:
        from src.core import ArgosCore  # type: ignore
        _core = ArgosCore()
        logger.info("[BrainAPI] ArgosCore инициализирован успешно")
    except Exception as e:
        logger.warning("[BrainAPI] ArgosCore недоступен: %s", e)
        _core = None
    return _core


def _ollama_available() -> bool:
    return shutil.which("ollama") is not None


def _argos_v1_exists() -> bool:
    """Проверяет наличие модели argos-v1 в установленных моделях Ollama."""
    if not _ollama_available():
        return False
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "argos-v1" in result.stdout
    except Exception:
        return False


def _get_system_metrics() -> dict:
    """Собирает метрики железа ПК: CPU temp, RAM %, GPU VRAM."""
    metrics = {"cpu_temp": None, "ram_used": None, "gpu_vram": None}
    # CPU temp
    try:
        temps = []
        for zone in range(0, 20):
            path = f"/sys/class/thermal/thermal_zone{zone}/temp"
            if not _os.path.exists(path):
                continue
            with open(path) as f:
                val = int(f.read().strip())
            if val > 0:
                temps.append(val / 1000.0)
        if temps:
            metrics["cpu_temp"] = round(max(temps), 1)
    except Exception:
        pass
    # RAM %
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    meminfo[k.strip()] = int(v.split()[0])
        total = meminfo.get("MemTotal", 1)
        avail = meminfo.get("MemAvailable", 0)
        metrics["ram_used"] = round((1 - avail / total) * 100, 1)
    except Exception:
        pass
    # GPU VRAM (AMD ROCm)
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram", "--csv"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 2:
                vram_mb = parts[1].strip().replace(" MiB", "").replace(" MB", "")
                metrics["gpu_vram"] = int(vram_mb)
                break
    except Exception:
        pass
    # GPU VRAM fallback (nvidia-smi)
    if metrics["gpu_vram"] is None:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            metrics["gpu_vram"] = int(result.stdout.strip().splitlines()[0])
        except Exception:
            pass
    return metrics


def _mark_stale_nodes() -> None:
    """Помечает узлы без heartbeat > NODE_TIMEOUT_SECONDS как offline."""
    now = datetime.now()
    for info in NODE_REGISTRY.values():
        last = info.get("last_heartbeat")
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            continue
        if (now - last_dt).total_seconds() > NODE_TIMEOUT_SECONDS:
            info["status"] = "offline"


def async_route(f):
    """Декоратор для async-маршрутов Flask."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapped


# ── Before request: ленивая инициализация brain ───────────────────────────────

@app.before_request
def initialize_brain():
    """Инициализирует ARGOSBrain перед первым запросом (не блокирует старт)."""
    global brain
    if brain is None:
        try:
            brain = ARGOSBrain(node_id="api-server")
            logger.info("[BrainAPI] ARGOSBrain инициализирован")
        except Exception as e:
            logger.error("[BrainAPI] Ошибка инициализации ARGOSBrain: %s", e)


# ============================================================
# ОСНОВНЫЕ МАРШРУТЫ
# ============================================================

@app.route('/health', methods=['GET'])
def health():
    """Проверка живости API."""
    return jsonify({
        'status': 'online',
        'service': 'ARGOS AI Brain API',
        'timestamp': datetime.now().isoformat(),
    }), 200


@app.route('/brain/status', methods=['GET'])
def get_brain_status():
    """Статус ARGOSBrain."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    return jsonify(brain.get_status()), 200


# ============================================================
# УПРАВЛЕНИЕ АГЕНТАМИ
# ============================================================

@app.route('/agents', methods=['GET'])
def list_agents():
    """Список всех агентов."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    agents = {aid: agent.get_status() for aid, agent in brain.agents.items()}
    return jsonify({'count': len(agents), 'agents': agents}), 200


@app.route('/agents', methods=['POST'])
def create_agent():
    """Создать нового агента."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    data = request.json or {}
    try:
        name = data.get('name')
        role = data.get('role', 'executor')
        try:
            role_enum = AgentRole[role.upper()]
        except KeyError:
            return jsonify({
                'error': f'Неизвестная роль: {role}',
                'available_roles': [r.value for r in AgentRole],
            }), 400
        agent = brain.create_agent(
            name=name or f"agent_{role}",
            role=role_enum,
            model=data.get('model', 'gpt-4'),
            temperature=data.get('temperature', 0.7),
            max_tokens=data.get('max_tokens', 2000),
        )
        return jsonify({'success': True, 'agent': agent.get_status()}), 201
    except Exception as e:
        logger.error("Ошибка при создании агента: %s", e)
        return jsonify({'error': str(e)}), 400


@app.route('/agents/<agent_id>', methods=['GET'])
def get_agent(agent_id):
    """Информация об агенте."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    if agent_id not in brain.agents:
        return jsonify({'error': 'Агент не найден'}), 404
    return jsonify(brain.agents[agent_id].get_status()), 200


# ============================================================
# МЫШЛЕНИЕ — /think → ArgosCore с fallback на ARGOSBrain
# ============================================================

@app.route('/think', methods=['POST'])
@async_route
async def think():
    """Главный запрос к мозгу.

    Сначала пробует ArgosCore (реальная AI-логика с auto-consensus, Ollama и т.д.).
    При недоступности ArgosCore — fallback на ARGOSBrain.
    """
    data = request.json or {}
    query = data.get('query')
    if not query:
        return jsonify({'error': 'query обязателен'}), 400

    context_data = data.get('context', {})
    context_str = json.dumps(context_data, ensure_ascii=False) if isinstance(context_data, dict) else str(context_data)

    # Попытка через ArgosCore
    core = _get_core()
    if core is not None:
        try:
            # ArgosCore.ask() / process() — синхронный вызов, запускаем через executor
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(
                None,
                lambda: core.ask(user_text=query, context=context_str),
            )
            if answer:
                return jsonify({
                    'success': True,
                    'response': answer,
                    'source': 'ArgosCore',
                    'agent_id': 'argos-core',
                    'timestamp': datetime.now().isoformat(),
                }), 200
        except AttributeError:
            # ArgosCore может не иметь метода ask() — пробуем process()
            try:
                loop = asyncio.get_event_loop()
                answer = await loop.run_in_executor(
                    None,
                    lambda: core.process(query),
                )
                if answer:
                    return jsonify({
                        'success': True,
                        'response': answer,
                        'source': 'ArgosCore',
                        'agent_id': 'argos-core',
                        'timestamp': datetime.now().isoformat(),
                    }), 200
            except Exception as e:
                logger.warning("[BrainAPI] ArgosCore.process() ошибка: %s", e)
        except Exception as e:
            logger.warning("[BrainAPI] ArgosCore.ask() ошибка: %s", e)

    # Fallback на ARGOSBrain
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован и ArgosCore недоступен'}), 500

    try:
        role = data.get('role', 'master')
        try:
            role_enum = AgentRole[role.upper()]
        except KeyError:
            return jsonify({
                'error': f'Неизвестная роль: {role}',
                'available_roles': [r.value for r in AgentRole],
            }), 400

        result = await brain.think(query, role_enum, context_data)
        return jsonify({
            'success': result.get('success'),
            'response': result.get('response'),
            'source': 'ARGOSBrain',
            'agent_id': result.get('agent_id'),
            'tokens': result.get('tokens'),
            'thinking_time': result.get('thinking_time'),
            'fallback': result.get('fallback'),
            'timestamp': datetime.now().isoformat(),
        }), 200
    except Exception as e:
        logger.error("Ошибка при запросе think: %s", e)
        return jsonify({'error': str(e)}), 400


# ============================================================
# КООРДИНАЦИЯ, АНАЛИЗ, ОПТИМИЗАЦИЯ, МОНИТОРИНГ
# ============================================================

@app.route('/coordinate', methods=['POST'])
@async_route
async def coordinate():
    """Координация агентов."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    data = request.json or {}
    task = data.get('task')
    if not task:
        return jsonify({'error': 'task обязателен'}), 400
    agent_roles = data.get('agents', ['analyst', 'optimizer', 'executor'])
    agent_enums = []
    for role in agent_roles:
        try:
            agent_enums.append(AgentRole[role.upper()])
        except KeyError:
            return jsonify({'error': f'Неизвестная роль: {role}'}), 400
    try:
        result = await brain.coordinate(task, agent_enums)
        return jsonify(result), 200
    except Exception as e:
        logger.error("Ошибка при координации: %s", e)
        return jsonify({'error': str(e)}), 400


@app.route('/analyze', methods=['POST'])
@async_route
async def analyze():
    """Анализ данных."""
    data = request.json or {}
    query = (
        f"Проанализируй следующие данные:\n"
        f"{json.dumps(data.get('data', {}), ensure_ascii=False)}\n\n"
        "Предоставь:\n1. Основные закономерности\n2. Аномалии\n3. Рекомендации"
    )
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    result = await brain.think(query, AgentRole.ANALYST, data.get('context'))
    return jsonify({
        'analysis': result.get('response'),
        'agent': result.get('agent_id'),
        'timestamp': datetime.now().isoformat(),
    }), 200


@app.route('/optimize', methods=['POST'])
@async_route
async def optimize():
    """Оптимизация процесса."""
    data = request.json or {}
    query = (
        f"Предоставь рекомендации по оптимизации:\n\n"
        f"Текущее состояние:\n{json.dumps(data.get('metrics', {}), ensure_ascii=False)}\n\n"
        f"Целевые показатели:\n{json.dumps(data.get('targets', {}), ensure_ascii=False)}\n\n"
        "Предложи конкретные шаги улучшения."
    )
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    result = await brain.think(query, AgentRole.OPTIMIZER, data.get('context'))
    return jsonify({
        'recommendations': result.get('response'),
        'agent': result.get('agent_id'),
        'timestamp': datetime.now().isoformat(),
    }), 200


@app.route('/monitor', methods=['POST'])
@async_route
async def monitor():
    """Мониторинг системы."""
    data = request.json or {}
    query = (
        f"Проведи мониторинг и анализ здоровья системы:\n\n"
        f"Показатели:\n{json.dumps(data.get('metrics', {}), ensure_ascii=False)}\n\n"
        "Предоставь оценку статуса и выяви потенциальные проблемы."
    )
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    result = await brain.think(query, AgentRole.MONITOR, data.get('context'))
    return jsonify({
        'status': result.get('response'),
        'agent': result.get('agent_id'),
        'timestamp': datetime.now().isoformat(),
    }), 200


# ============================================================
# P2P — /p2p/status + реестр узлов
# ============================================================

@app.route('/p2p/status', methods=['GET'])
def p2p_status():
    """Статус P2P-сети: из ArgosCore (если доступен) + локальный реестр."""
    _mark_stale_nodes()
    nodes = list(NODE_REGISTRY.values())
    online_count = sum(1 for n in nodes if n.get("status") == "online")

    # Попытка получить расширенный статус из ArgosCore P2P
    core_p2p_info: Optional[Dict[str, Any]] = None
    core = _get_core()
    if core is not None:
        try:
            p2p = getattr(core, 'p2p', None)
            if p2p is not None and hasattr(p2p, 'network_status'):
                core_p2p_info = p2p.network_status()
        except Exception as e:
            logger.debug("[BrainAPI] P2P network_status ошибка: %s", e)

    response: Dict[str, Any] = {
        'registry_nodes_total': len(nodes),
        'registry_nodes_online': online_count,
        'registry_nodes_offline': len(nodes) - online_count,
        'timestamp': datetime.now().isoformat(),
    }
    if core_p2p_info:
        response['core_p2p'] = core_p2p_info

    return jsonify(response), 200


@app.route('/brain/register', methods=['POST'])
def register_node():
    """Регистрация P2P-узла.

    Body: { "node_id": "...", "capabilities": [...], "models": [...], "address": "..." }
    """
    data = request.json or {}
    node_id = data.get("node_id")
    if not node_id:
        return jsonify({"error": "node_id required"}), 400
    now_iso = datetime.now().isoformat()
    existing = NODE_REGISTRY.get(node_id, {})
    NODE_REGISTRY[node_id] = {
        "node_id":        node_id,
        "capabilities":   data.get("capabilities", []),
        "models":         data.get("models", []),
        "address":        data.get("address") or request.remote_addr,
        "status":         "online",
        "registered_at":  existing.get("registered_at", now_iso),
        "last_heartbeat": now_iso,
        "meta":           data.get("meta", {}),
    }
    logger.info("[P2P] Зарегистрирован узел %s от %s", node_id, request.remote_addr)
    return jsonify({"status": "registered", "node_id": node_id, "timestamp": now_iso}), 200


@app.route('/brain/heartbeat', methods=['POST'])
def heartbeat_node():
    """Heartbeat от P2P-узла. Body: { "node_id": "...", "status": "online", "models": [...] }"""
    data = request.json or {}
    node_id = data.get("node_id")
    if not node_id:
        return jsonify({"error": "node_id required"}), 400
    if node_id not in NODE_REGISTRY:
        NODE_REGISTRY[node_id] = {
            "node_id":       node_id,
            "capabilities":  [],
            "models":        data.get("models", []),
            "address":       request.remote_addr,
            "registered_at": datetime.now().isoformat(),
            "meta":          {},
        }
    NODE_REGISTRY[node_id]["status"] = data.get("status", "online")
    NODE_REGISTRY[node_id]["last_heartbeat"] = datetime.now().isoformat()
    if "models" in data:
        NODE_REGISTRY[node_id]["models"] = data["models"]
    return jsonify({"status": "ok", "node_id": node_id}), 200


@app.route('/brain/nodes', methods=['GET'])
def list_nodes():
    """Список всех P2P-узлов со статусами."""
    _mark_stale_nodes()
    nodes = list(NODE_REGISTRY.values())
    online = sum(1 for n in nodes if n.get("status") == "online")
    return jsonify({
        "total":     len(nodes),
        "online":    online,
        "offline":   len(nodes) - online,
        "nodes":     nodes,
        "timestamp": datetime.now().isoformat(),
    }), 200


@app.route('/brain/nodes/<node_id>', methods=['DELETE'])
def unregister_node(node_id: str):
    """Удалить P2P-узел из реестра."""
    if node_id in NODE_REGISTRY:
        del NODE_REGISTRY[node_id]
        return jsonify({"status": "removed", "node_id": node_id}), 200
    return jsonify({"error": "not found"}), 404


@app.route('/dashboard', methods=['GET'])
def dashboard_page():
    """HTML-дашборд узлов."""
    path = _os.path.join(_os.path.dirname(__file__), "argos_brain_dashboard.html")
    if _os.path.exists(path):
        return send_file(path)
    return jsonify({"error": "dashboard file not found"}), 404


# ============================================================
# OLLAMA TRAINING — /ollama/train
# ============================================================

@app.route('/ollama/train', methods=['POST'])
def ollama_train():
    """Запустить обучение модели argos-v1 через ArgosOllamaTrainer.

    Body (опционально): { "max_examples": 50 }
    """
    data = request.json or {}
    max_examples = int(data.get("max_examples", 50))
    try:
        from src.ollama_trainer import ArgosOllamaTrainer  # lazy import
        trainer = ArgosOllamaTrainer()
        logger.info("[BrainAPI] Запуск обучения: max_examples=%d", max_examples)
        status_msg = trainer.train(max_examples=max_examples)
        success = "успешно" in status_msg.lower() or "создана" in status_msg.lower()
        return jsonify({
            "success": success,
            "result":  status_msg,
            "model":   "argos-v1",
            "timestamp": datetime.now().isoformat(),
        }), 200 if success else 500
    except Exception as e:
        logger.error("[BrainAPI] ollama/train ошибка: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ============================================================
# SYSTEM STATUS — /system/status
# ============================================================

@app.route('/system/status', methods=['GET'])
def system_status():
    """Общий статус системы: AI mode, Ollama, P2P, skills."""
    _mark_stale_nodes()

    # AI mode из ArgosCore
    ai_mode = "unknown"
    skills_count = 0
    core = _get_core()
    if core is not None:
        try:
            ai_mode = getattr(core, 'ai_mode', 'unknown')
        except Exception:
            pass
        try:
            skills = getattr(core, 'skills', None) or getattr(core, '_skills', None)
            if skills is not None:
                skills_count = len(skills) if hasattr(skills, '__len__') else 0
        except Exception:
            pass

    # Ollama статус
    ollama_ok = _ollama_available()
    argos_v1_ready = _argos_v1_exists()

    # P2P
    nodes = list(NODE_REGISTRY.values())
    online_nodes = sum(1 for n in nodes if n.get("status") == "online")

    # Ollama доп. info (список моделей)
    ollama_models: list[str] = []
    if ollama_ok:
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().splitlines()[1:]:  # пропускаем заголовок
                parts = line.split()
                if parts:
                    ollama_models.append(parts[0])
        except Exception:
            pass

    return jsonify({
        "service":         "ARGOS AI Brain API",
        "ai_mode":         ai_mode,
        "argos_core":      core is not None,
        "ollama": {
            "available":   ollama_ok,
            "argos_v1":    argos_v1_ready,
            "models":      ollama_models,
        },
        "p2p": {
            "nodes_total":  len(nodes),
            "nodes_online": online_nodes,
        },
        "skills_count":    skills_count,
        "brain_ready":     brain is not None,
        "timestamp":       datetime.now().isoformat(),
    }), 200


@app.route('/system', methods=['GET'])
def system_metrics():
    """Метрики железа ПК для Home Assistant."""
    metrics = _get_system_metrics()
    return jsonify({
        "node": "argos-pc",
        "cpu_temp": metrics.get("cpu_temp"),
        "ram_used": metrics.get("ram_used"),
        "gpu_vram": metrics.get("gpu_vram"),
        "timestamp": datetime.now().isoformat(),
    }), 200


# ============================================================
# УПРАВЛЕНИЕ BRAIN
# ============================================================

@app.route('/brain/start', methods=['POST'])
@async_route
async def start_brain():
    """Запустить brain."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    await brain.start()
    return jsonify({'status': 'started'}), 200


@app.route('/brain/stop', methods=['POST'])
@async_route
async def stop_brain():
    """Остановить brain."""
    if brain is None:
        return jsonify({'error': 'Brain не инициализирован'}), 500
    await brain.stop()
    return jsonify({'status': 'stopped'}), 200


@app.route('/brain/reset', methods=['POST'])
def reset_brain():
    """Сбросить brain."""
    global brain, _core, _core_init_attempted
    try:
        brain = ARGOSBrain(node_id="api-server")
        # Сбрасываем cached core чтобы переинициализировать при следующем запросе
        _core = None
        _core_init_attempted = False
        return jsonify({'status': 'reset', 'message': 'Brain и ArgosCore перезагружены'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# ДОКУМЕНТАЦИЯ
# ============================================================

@app.route('/', methods=['GET'])
def api_docs():
    """Документация API."""
    return jsonify({
        'service': 'ARGOS AI Brain API',
        'version': '2.0.0',
        'endpoints': {
            'GET  /health':         'Проверка живости',
            'GET  /brain/status':   'Статус ARGOSBrain',
            'GET  /agents':         'Список агентов',
            'POST /agents':         'Создать агента',
            'GET  /agents/<id>':    'Информация об агенте',
            'POST /think':          'Запрос к ArgosCore / ARGOSBrain',
            'POST /coordinate':     'Координация агентов',
            'POST /analyze':        'Анализ данных',
            'POST /optimize':       'Оптимизация',
            'POST /monitor':        'Мониторинг',
            'GET  /p2p/status':     'Статус P2P-сети',
            'POST /ollama/train':   'Запустить обучение argos-v1',
            'GET  /system/status':  'Общий статус системы',
            'POST /brain/register': 'Регистрация P2P-узла',
            'POST /brain/heartbeat':'Heartbeat P2P-узла',
            'GET  /brain/nodes':    'Список P2P-узлов',
            'DELETE /brain/nodes/<id>': 'Удалить P2P-узел',
            'GET  /dashboard':      'HTML-дашборд',
            'POST /brain/start':    'Запустить brain',
            'POST /brain/stop':     'Остановить brain',
            'POST /brain/reset':    'Сбросить brain',
        },
    }), 200


# ============================================================
# ОБРАБОТЧИКИ ОШИБОК
# ============================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Маршрут не найден', 'status': 404}), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error("Internal server error: %s", error)
    return jsonify({'error': 'Внутренняя ошибка сервера', 'status': 500}), 500


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

if __name__ == '__main__':
    # Создать базовых агентов при старте в standalone-режиме
    brain = ARGOSBrain(node_id="api-server")
    brain.create_agent("Главный координатор", AgentRole.MASTER)
    brain.create_agent("Аналитик", AgentRole.ANALYST)
    brain.create_agent("Оптимизатор", AgentRole.OPTIMIZER)
    brain.create_agent("Монитор", AgentRole.MONITOR)

    _port = int(_os.getenv("PORT") or _os.getenv("ARGOS_BRAIN_API_PORT") or 5001)
    print(f"ARGOS AI Brain API v2 запущен на http://0.0.0.0:{_port}/")

    app.run(host='0.0.0.0', port=_port, debug=False, threaded=True)

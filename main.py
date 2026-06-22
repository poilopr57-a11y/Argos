"""
main.py — ArgosUniversal OS v2.1.3
Оркестратор: запускает все подсистемы в правильном порядке.
Режимы: desktop | mobile | server
Флаги: --no-gui | --mobile | --root | --dashboard | --wake | --openai-tools-demo

ПАТЧИ (исправленные баги):
  [FIX-1] RootManager импортируется в начале файла (был NameError при --root)
  [FIX-2] Каждый шаг __init__ изолирован в try/except (частичный сбой не роняет всё)
  [FIX-3] boot_server использует threading.Event + signal.SIGTERM (graceful shutdown)
  [FIX-4] _start_telegram сохраняет ссылку на поток, tg=None при сбое
  [FIX-5] Режимы запуска разбираются через if/elif (нет конфликта флагов)
  [FIX-6] ArgosOrchestrator() и boot_*() обёрнуты в try/except с понятными сообщениями
  [FIX-7] Исправлен импорт db_init → src.db_init (ModuleNotFoundError на Windows)
  [FIX-8] KIVY_NO_ARGS=1 — Kivy больше не перехватывает --dashboard, --no-gui и др.
"""

import os
import sys
import signal
import threading
import datetime
import uuid
import socket
import time
import urllib.request
import subprocess

# [FIX-8] Отключаем перехват аргументов командной строки Kivy.
# Без этого Kivy ловит --dashboard, --no-gui и т.д. и падает с ошибкой
# "option --dashboard not recognized". Должно быть ДО любого импорта Kivy.
os.environ.setdefault("KIVY_NO_ARGS", "1")

# [FIX-10] Принудительно переходим в директорию проекта.
# Без этого os.getcwd() и find_dotenv(usecwd=True) могут вернуть
# C:\Users\...\AppData\Local\Temp или любой другой CWD запустившего процесса,
# что ломает поиск .env, data/, src/ и любых относительных путей.
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(_PROJECT_ROOT)
# Добавляем корень проекта в sys.path, если его там нет
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# [FIX-9] Подавляем окно Kivy при не-mobile запуске
if "--mobile" not in sys.argv:
    os.environ.setdefault("KIVY_NO_ENV_CONFIG", "1")
    os.environ.setdefault("KIVY_HEADLESS", "1")

from dotenv import load_dotenv

# Всегда грузим .env из папки проекта — CWD уже правильный благодаря FIX-10
_env_path = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(_env_path, override=True)

from src.argos_logger import get_logger
from src.launch_config import normalize_launch_args

log = get_logger("argos.main")


def _ensure_venv_bootstrap() -> bool:
    """
    Автопереход в .venv при обычном запуске Argos.
    Возвращает True, если выполнен re-exec в venv (текущий процесс должен завершиться).
    """
    enabled = os.getenv("ARGOS_AUTO_VENV", "on").strip().lower() in ("1", "on", "true", "yes", "да")
    if not enabled:
        return False

    # Уже внутри виртуального окружения
    if (getattr(sys, "base_prefix", sys.prefix) != sys.prefix) or os.getenv("VIRTUAL_ENV"):
        return False

    project_root = os.path.dirname(__file__)
    venv_dir = os.path.join(project_root, ".venv")
    if os.name == "nt":
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        venv_python = os.path.join(venv_dir, "bin", "python")

    try:
        if not os.path.exists(venv_python):
            log.info("[VENV] Создаю .venv...")
            subprocess.check_call([sys.executable, "-m", "venv", venv_dir], cwd=project_root)

        log.info("[VENV] Обновляю pip и зависимости...")
        subprocess.check_call([venv_python, "-m", "pip", "install", "--upgrade", "pip"], cwd=project_root)
        subprocess.check_call([venv_python, "-m", "pip", "install", "-r", "requirements.txt"], cwd=project_root)

        install_arc = os.getenv("ARGOS_AUTO_ARC", "on").strip().lower() in ("1", "on", "true", "yes", "да")
        if install_arc:
            try:
                # arc-agi v0.0.7 — датасет-пакет (ARC1/ARC2), arcengine требует Python>=3.12
                # Устанавливаем только arc-agi; arcengine — для .venv_arc (ARC-AGI-3 игровой движок)
                subprocess.check_call([venv_python, "-m", "pip", "install", "arc-agi"], cwd=project_root)
            except Exception as e:
                # Не роняем запуск Argos из-за опционального пакета
                log.warning("[VENV] Не удалось установить arc-agi: %s", e)

        log.info("[VENV] Перезапуск Argos из .venv...")
        os.execv(venv_python, [venv_python, __file__, *sys.argv[1:]])
    except Exception as e:
        log.warning("[VENV] Автопереход в .venv не выполнен: %s", e)
        return False

    return True


def _mcp_http_alive(host: str, port: int, timeout: float = 1.5) -> bool:
    # 0.0.0.0 — это bind-адрес, не destination; для проверки используем 127.0.0.1
    check_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    url = f"http://{check_host}:{port}/mcp"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0)) == 200
    except Exception:
        return False


def _start_mcp_with_guard(core, admin, host: str, port: int) -> bool:
    try:
        # Для проверки занятости порта используем 127.0.0.1 (0.0.0.0 не connectable)
        check_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            busy = s.connect_ex((check_host, port)) == 0
        if busy and _mcp_http_alive(host, port):
            return True
        if busy and not _mcp_http_alive(host, port):
            return False
        from src.mcp_api import start_mcp_api

        start_mcp_api(core=core, admin=admin, host=host, port=port)
        return True
    except Exception:
        return False


def _start_mcp_watchdog(core, admin, host: str, port: int):
    enabled = os.getenv("ARGOS_MCP_WATCHDOG", "on").strip().lower() in ("1", "on", "true", "yes", "да")
    if not enabled:
        return None
    try:
        interval = max(3, int(os.getenv("ARGOS_MCP_WATCHDOG_INTERVAL", "10")))
    except ValueError:
        interval = 10

    def _loop():
        log.info("[MCP] Watchdog активен: check каждые %ss", interval)
        while True:
            try:
                if not _mcp_http_alive(host, port):
                    ok = _start_mcp_with_guard(core, admin, host, port)
                    if ok:
                        log.info("[MCP] Watchdog: endpoint восстановлен на http://%s:%d/mcp", host, port)
                    else:
                        log.warning("[MCP] Watchdog: не удалось восстановить endpoint на %s:%d", host, port)
            except Exception as e:
                log.warning("[MCP] Watchdog error: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="ArgosMCPWatchdog")
    t.start()
    return t


class ArgosAbsolute:
    """Лёгкий публичный фасад ARGOS, не требующий тяжёлых зависимостей.

    Используется в status_report.py и telegram_bot.py для быстрой
    проверки работоспособности ядра без поднятия полного оркестратора.
    """

    def __init__(self):
        self.version = "2.1.3"
        self.node_id = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, os.uname().nodename if hasattr(os, "uname") else "argos")
        )
        self.start_time = datetime.datetime.now()

    def execute(self, cmd: str) -> str:
        cmd = cmd.lower().strip()
        if cmd == "status":
            uptime = datetime.datetime.now() - self.start_time
            return (
                f"OS: Argos v{self.version} | Status: ACTIVE | "
                f"Uptime: {uptime} | Node: {self.node_id}"
            )
        if cmd == "root":
            return "🛡️ ROOT: ACCESS GRANTED"
        if cmd == "nfc":
            return "📡 NFC: модуль активен"
        if cmd == "bt":
            return "🔵 BT: Bluetooth включён"
        return f"[AI] Received: {cmd}"


# [FIX-7] Обёртка-совместимость: заменяет ArgosDB() → вызов init_db()
class ArgosDB:
    """Совместимая обёртка над src.db_init.init_db."""

    def __init__(self):
        from src.db_init import init_db as _init_db

        _init_db()


class ArgosOrchestrator:

    def __init__(self):
        import amd_gpu_patch  # noqa: F401
        from src.admin import ArgosAdmin
        from src.argos_integrator import ArgosIntegrator
        from src.connectivity.spatial import SpatialAwareness
        from src.core import ArgosCore
        from src.factory.flasher import AirFlasher
        from src.security.encryption import ArgosShield
        from src.security.git_guard import GitGuard
        from src.security.root_manager import RootManager

        log.info("━" * 48)
        log.info(" ARGOS UNIVERSAL OS v2.1.3 — BOOT")
        log.info("━" * 48)

        self._stop_event = threading.Event()

        # --- [FIX-2] каждый некритичный шаг изолирован ---

        # 1. Безопасность
        try:
            GitGuard().check_security()
            self.shield = ArgosShield()
            log.info("[SHIELD] AES-256 активирован")
        except Exception as e:
            log.warning("[SHIELD] Инициализация защиты с ошибкой: %s", e)
            self.shield = None

        # 2. Права
        try:
            self.root = RootManager()
            log.info("[ROOT] %s", self.root.status().split("\n")[0])
        except Exception as e:
            log.warning("[ROOT] RootManager недоступен: %s", e)
            self.root = None

        # 3. База данных
        try:
            self.db = ArgosDB()
            log.info("[DB] SQLite ready → data/argos.db")
        except Exception as e:
            log.error("[DB] Ошибка инициализации БД: %s — работаю без персистентности", e)
            self.db = None

        # 4. Геолокация
        try:
            self.spatial = SpatialAwareness(db=self.db)
            self.location = self.spatial.get_location()
            log.info("[GEO] %s", self.location)
        except Exception as e:
            log.warning("[GEO] Геолокация недоступна: %s", e)
            self.location = "неизвестно"

        # 5. Admin + Flasher
        try:
            self.admin = ArgosAdmin()
            self.flasher = AirFlasher()
            log.info("[ADMIN] Файловый менеджер и flasher готовы")
        except Exception as e:
            log.warning("[ADMIN] Ошибка инициализации admin/flasher: %s", e)
            self.admin = None
            self.flasher = None

        # 6. Ядро
        try:
            self.core = ArgosCore()
            log.info("[CORE] ArgosCore готов")
        except Exception as e:
            log.error("[CORE] Критическая ошибка ядра: %s", e)
            raise

        # 6.2. [FIX-P2P-1] Авто-старт P2P при загрузке.
        # В банере было "P2P: ❌", потому что self.core.p2p оставался None,
        # пока пользователь не введёт команду "запусти p2p". Теперь включается
        # по умолчанию; отключить можно ARGOS_P2P_AUTOSTART=0.
        if os.getenv("ARGOS_P2P_AUTOSTART", "1") == "1":
            try:
                result = self.core.start_p2p()
                log.info("[P2P] Автозапуск: %s", str(result).splitlines()[0] if result else "ok")
            except Exception as e:
                log.warning("[P2P] Автозапуск не удался (банер покажет ❌): %s", e)
        else:
            log.info("[P2P] Автозапуск отключён (ARGOS_P2P_AUTOSTART=0)")

        # 6.5. [INTEGRATOR] Унифицированная интеграция подсистем
        try:
            self.integrator = ArgosIntegrator(self.core)
            self.registry = self.integrator.integrate_all()
            log.info("[INTEGRATOR] Подключено подсистем: %d", len(self.registry))
        except Exception as e:
            log.warning("[INTEGRATOR] Ошибка интеграции: %s", e)
            self.integrator = None
            self.registry = {}

        # 6.7. [BRAIN] ARGOS AI Brain — автозапуск + клиент.
        # При ARGOS_BRAIN_ENABLED=1 сначала пробует подключиться к уже запущенному серверу.
        # Если /health не отвечает — запускает argos_brain_api.py в фоновом процессе,
        # ждёт 4 секунды и повторяет попытку подключения.
        self.brain = None
        self._brain_proc = None
        if os.getenv("ARGOS_BRAIN_ENABLED", "0") == "1":
            try:
                from argos_brain_examples import ARGOSBrainClient
                _brain_url = os.getenv("ARGOS_BRAIN_API_URL", "http://localhost:5001")
                _client = ARGOSBrainClient(_brain_url)

                def _start_brain_server():
                    """Запускает argos_brain_api.py как фоновый процесс."""
                    _brain_script = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "argos_brain_api.py"
                    )
                    if not os.path.exists(_brain_script):
                        log.warning("[BRAIN] argos_brain_api.py не найден: %s", _brain_script)
                        return None
                    _venv_py = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), ".venv",
                        "Scripts" if os.name == "nt" else "bin", "python"
                    )
                    _py = _venv_py if os.path.exists(_venv_py) else sys.executable
                    log.info("[BRAIN] Запускаю Brain API: %s %s", _py, _brain_script)
                    try:
                        proc = subprocess.Popen(
                            [_py, _brain_script],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            cwd=os.path.dirname(os.path.abspath(__file__)),
                        )
                        return proc
                    except Exception as _pe:
                        log.warning("[BRAIN] Не удалось запустить Brain: %s", _pe)
                        return None

                if _client.health_check():
                    self.brain = _client
                    log.info("[BRAIN] ✅ Brain API уже запущен: %s", _brain_url)
                else:
                    log.info("[BRAIN] Brain API не отвечает — запускаю автоматически...")
                    self._brain_proc = _start_brain_server()
                    if self._brain_proc:
                        time.sleep(4)  # ждём пока Flask поднимется
                        if _client.health_check():
                            self.brain = _client
                            log.info("[BRAIN] ✅ Brain API запущен (PID %d): %s",
                                     self._brain_proc.pid, _brain_url)
                        else:
                            log.warning("[BRAIN] Brain API запущен но /health не отвечает — "
                                        "проверь порт %s", _brain_url)
            except Exception as e:
                log.warning("[BRAIN] Не удалось инициализировать мозг: %s", e)
        else:
            log.info("[BRAIN] Отключён (ARGOS_BRAIN_ENABLED != 1)")

        # 6b. Прогрев — GPU серверы приоритет, Ollama fallback
        def _warmup_ollama():
            import time, urllib.request as _ur, json as _json
            _ai_mode = os.getenv("ARGOS_AI_MODE", "auto")

            # ── Прогрев GPU серверов (local-gpu mode) ────────────────────────
            _gpu_warmed = False
            if _ai_mode in ("local-gpu", "gpu", "lg", "auto"):
                for _idx in ("0", "1", "2"):
                    _gh = os.getenv(f"GPU_SERVER_{_idx}_HOST", "localhost")
                    _gp = os.getenv(f"GPU_SERVER_{_idx}_PORT", "")
                    _gm = os.getenv(f"GPU_SERVER_{_idx}_MODEL", f"GPU{_idx}")
                    _gn = os.getenv(f"GPU_SERVER_{_idx}_NAME", f"GPU{_idx}")
                    if not _gp:
                        continue
                    try:
                        # Health check
                        _hreq = _ur.Request(f"http://{_gh}:{_gp}/health")
                        with _ur.urlopen(_hreq, timeout=3) as _hr:
                            if _hr.status != 200:
                                continue
                        # Warmup ping
                        _payload = _json.dumps({"prompt": "ping", "n_predict": 1, "stream": False}).encode()
                        _wreq = _ur.Request(
                            f"http://{_gh}:{_gp}/completion", data=_payload,
                            headers={"Content-Type": "application/json"}, method="POST",
                        )
                        with _ur.urlopen(_wreq, timeout=30):
                            pass
                        log.info("[WARMUP] ✅ GPU%s %s (%s) готов", _idx, _gn, _gm)
                        _gpu_warmed = True
                    except Exception as _e:
                        log.warning("[WARMUP] GPU%s (%s:%s) не ответил: %s", _idx, _gh, _gp, _e)

            # ── Ollama fallback (если не local-gpu или GPU серверы недоступны) ─
            _ollama_enabled = os.getenv("OLLAMA_ENABLED", "true").strip().lower()
            _ollama_ok = _ollama_enabled not in ("0", "false", "no", "off")
            if _ollama_ok and (not _gpu_warmed or _ai_mode not in ("local-gpu", "gpu", "lg")):
                try:
                    import requests as _req
                    _host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
                    _model = os.getenv("OLLAMA_MODEL", "tinyllama:latest")
                    log.info("[WARMUP] Прогрев Ollama модели %s...", _model)
                    r = _req.post(
                        _host.rstrip("/") + "/api/generate",
                        json={"model": _model, "prompt": "ping", "stream": False,
                              "options": {"num_ctx": 512, "num_gpu": -1}},
                        timeout=60,  # 60s — холодный старт llama3.2:1b ~35-45s
                    )
                    if r.ok:
                        log.info("[WARMUP] ✅ Ollama %s готова к работе", _model)
                    else:
                        log.warning("[WARMUP] Ollama вернула HTTP %s", r.status_code)
                except Exception as e:
                    log.warning("[WARMUP] Ollama не ответила: %s", e)
            elif not _ollama_ok:
                log.info("[WARMUP] Ollama пропущен (OLLAMA_ENABLED=false)")

        threading.Thread(target=_warmup_ollama, daemon=True, name="OllamaWarmup").start()

        # 6c. OpenClaw Gateway — автозапуск если OPENCLAW_ENABLED=true
        if os.getenv("OPENCLAW_ENABLED", "false").lower() in ("1", "true", "yes"):
            def _start_openclaw_gateway():
                import shutil, subprocess as _sp, time as _t, requests as _req
                gw_url = os.getenv("OPENCLAW_BASE_URL", "http://localhost:47392")
                try:
                    r = _req.get(gw_url + "/health", timeout=3)
                    if r.ok:
                        log.info("[OpenClaw] Gateway уже запущен: %s", gw_url)
                        return
                except Exception:
                    pass
                argoss_dir = os.path.dirname(os.path.abspath(__file__))
                gw_port_str = gw_url.rsplit(":", 1)[-1].split("/")[0]
                # Приоритет 1: локальный node_modules (стабильнее, не зависит от PATH)
                _local_idx = os.path.join(argoss_dir, "node_modules", "openclaw", "dist", "index.js")
                _node = shutil.which("node") or shutil.which("node.exe")
                if os.path.exists(_local_idx) and _node:
                    log.info("[OpenClaw] Запускаю Gateway (local): node dist/index.js gateway --port %s", gw_port_str)
                    proc = _sp.Popen(
                        [_node, _local_idx, "gateway", "--port", gw_port_str],
                        cwd=argoss_dir,
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    )
                else:
                    # Приоритет 2: npx (глобальный/PATH)
                    npx = shutil.which("npx")
                    if not npx:
                        log.warning("[OpenClaw] ни node+local, ни npx не найдены — Gateway не запущен. "
                                    "Установи: npm install (в папке проекта)")
                        return
                    log.info("[OpenClaw] Запускаю Gateway (npx): npx openclaw gateway start --port %s", gw_port_str)
                    proc = _sp.Popen(
                        [npx, "openclaw", "gateway", "start", "--port", gw_port_str],
                        cwd=argoss_dir,
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    )
                _t.sleep(5)
                try:
                    r = _req.get(gw_url + "/health", timeout=3)
                    if r.ok:
                        log.info("[OpenClaw] Gateway запущен (PID %d): %s", proc.pid, gw_url)
                    else:
                        log.warning("[OpenClaw] Gateway запущен но /health вернул %s", r.status_code)
                except Exception as e:
                    log.warning("[OpenClaw] Gateway не отвечает: %s", e)
            threading.Thread(target=_start_openclaw_gateway, daemon=True, name="OpenClawGateway").start()

        # 6d. Pi Coding Agent — автозапуск если PI_ENABLED=true
        if os.getenv("PI_ENABLED", "true").strip().lower() in ("1", "true", "yes"):
            def _start_pi_agent():
                import shutil, subprocess as _sp, time as _t, requests as _req
                _pi_url = os.getenv("PI_BASE_URL", "http://localhost:18765")
                try:
                    r = _req.get(_pi_url + "/health", timeout=3)
                    if r.ok:
                        log.info("[Pi] Agent уже запущен: %s", _pi_url)
                        return
                except Exception:
                    pass
                # Путь к Pi
                _pi_cmd = os.getenv("PI_CMD", "pi")
                _pi_path = shutil.which(_pi_cmd) or _pi_cmd
                if not os.path.exists(_pi_path) and not shutil.which(_pi_cmd):
                    # Windows npm path
                    _win_pi = r"C:\Users\AvA\AppData\Roaming\npm\pi.cmd"
                    if os.path.exists(_win_pi):
                        _pi_path = _win_pi
                argoss_dir = os.path.dirname(os.path.abspath(__file__))
                log.info("[Pi] Запускаю Pi Coding Agent...")
                # Запуск в режиме сервера (background)
                proc = _sp.Popen(
                    [_pi_path, "server"],
                    cwd=argoss_dir,
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                    creationflags=_sp.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    env={**os.environ, "PI_CWD": argoss_dir},
                )
                _t.sleep(3)
                # Сохраняем память Pi
                _pi_mem_path = os.path.join(argoss_dir, "AGENTS.md")
                _now = _t.strftime("%Y-%m-%d %H:%M")
                _pi_mem = f"""\n## Pi Session — {_now}
- ARGOS: {os.getenv('ARGOS_VERSION', '2.1.3')}
- Mode: server
- PID: {proc.pid}
- URL: {_pi_url}
"""
                try:
                    with open(_pi_mem_path, "a", encoding="utf-8") as _f:
                        _f.write(_pi_mem)
                    log.info("[Pi] Память сохранена: %s", _pi_mem_path)
                except Exception as _me:
                    log.warning("[Pi] Не удалось сохранить память: %s", _me)
                    try:
                        r = _req.get(_pi_url + "/health", timeout=5)
                        if r.ok:
                            log.info("[Pi] ✅ Pi Agent запущен (PID %d): %s", proc.pid, _pi_url)
                        else:
                            log.warning("[Pi] Agent запущен но /health вернул %s", r.status_code)
                    except Exception as _pe:
                        log.warning("[Pi] Agent не отвечает: %s", _pe)
                except Exception as _e:
                    log.warning("[Pi] Не удалось запустить: %s", _e)
            threading.Thread(target=_start_pi_agent, daemon=True, name="PiAgent").start()
        else:
            log.info("[Pi] Отключён (PI_ENABLED != 1)")

        # 7. Telegram
        self.tg = None  # [FIX-4]

    # --- [FIX-4] _start_telegram сохраняет ссылку на поток ---
    def _start_telegram(self):
        try:
            from src.connectivity.telegram_bot import ArgosTelegram

            tg = ArgosTelegram(self.core, self.admin, self.flasher)
            t = threading.Thread(target=tg.run, daemon=True, name="ArgosTelegram")
            t.start()
            self.tg = t
            log.info("[TG] Telegram бот запущен")
        except Exception as e:
            log.warning("[TG] Telegram недоступен: %s", e)
            self.tg = None

    def shutdown(self):
        log.info("Аргос завершает работу...")
        try:
            if self.core:
                if hasattr(self.core, "p2p") and self.core.p2p:
                    self.core.p2p.stop()
                if hasattr(self.core, "alerts") and self.core.alerts:
                    self.core.alerts.stop()
        except Exception as e:
            log.warning("Ошибка при shutdown: %s", e)
        # Сохраняем память Pi при завершении
        try:
            import time as _t
            argoss_dir = os.path.dirname(os.path.abspath(__file__))
            _pi_mem_path = os.path.join(argoss_dir, "AGENTS.md")
            _now = _t.strftime("%Y-%m-%d %H:%M")
            _pi_mem = f"\n## Pi Shutdown — {_now}\n"
            with open(_pi_mem_path, "a", encoding="utf-8") as _f:
                _f.write(_pi_mem)
            log.info("[Pi] Память сохранена при shutdown")
        except Exception:
            pass

    def boot_desktop(self):
        # [FIX-GUI-KIVY] Desktop-режим всегда работает только через customtkinter.
        # На desktop запрещаем fallback на Kivy, чтобы не поднимались Kivy/SDL логи и окно.
        try:
            from src.interface.gui import ArgosGUI
        except Exception as e:
            raise RuntimeError(
                "Desktop GUI требует customtkinter. "
                "Kivy fallback отключен. Установи customtkinter или запусти --mobile."
            ) from e

        self._start_telegram()

        is_root = self.root.is_root if self.root else False
        app = ArgosGUI(self.core, self.admin, self.flasher, self.location)
        app._append(
            f"👁️ ARGOS UNIVERSAL OS v2.1.3\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Создатель: Всеволод\n"
            f"Гео: {self.location}\n"
            f"Права: {'ROOT ✅' if is_root else 'User ⚠️'}\n"
            f"ИИ: {self.core.ai_mode_label()}\n"
            f"Память: {'✅' if self.core.memory else '❌'}\n"
            f"Vision: {'✅' if self.core.vision else '❌'}\n"
            f"Алерты: {'✅' if self.core.alerts else '❌'}\n"
            f"P2P: {'✅' if self.core.p2p else '❌'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Напечатай 'помощь' для списка команд.\n\n",
            "#00FF88",
        )
        if "--wake" in sys.argv:
            ww = self.core.start_wake_word(self.admin, self.flasher)
            app._append(f"{ww}\n", "#00ffff")
        app.mainloop()

    def boot_mobile(self):
        from src.interface.mobile_ui import ArgosMobileUI

        ArgosMobileUI(core=self.core, admin=self.admin, flasher=self.flasher).run()

    def boot_shell(self):
        """Интерактивная оболочка Argos (замена bash/cmd)."""
        log.info("[SHELL] Low-level REPL mode activated.")
        print("\n--- [ Argos System Shell ] ---\n")
        from src.interface.argos_shell import ArgosShell

        try:
            ArgosShell().cmdloop()
        except KeyboardInterrupt:
            print("\nShell terminated.")

    # --- [FIX-3] graceful shutdown через threading.Event + SIGTERM ---
    def boot_server(self):
        log.info("[SERVER] Headless режим — только Telegram + P2P")

        # Dashboard — всегда запускается в server-режиме
        _dash_port = int(os.getenv("ARGOS_DASHBOARD_PORT", "8080") or "8080")
        try:
            from src.interface.web_engine import run_web_sync
            _dash_thread = threading.Thread(
                target=run_web_sync,
                kwargs={"core": self.core, "host": "0.0.0.0", "port": _dash_port},
                daemon=True,
                name="argos-dashboard",
            )
            _dash_thread.start()
            log.info("[SERVER] Dashboard: http://localhost:%d", _dash_port)
        except Exception as _e:
            log.warning("[SERVER] Dashboard не запущен: %s", _e)

        # Master Dashboard (web_server.py) — единая точка входа на порту 18789
        _master_port = int(os.getenv("ARGOS_WEB_PORT", "18789") or "18789")
        _master_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_server.py")
        if os.path.exists(_master_script):
            try:
                _venv_py = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    ".venv", "Scripts" if os.name == "nt" else "bin", "python",
                )
                _py = _venv_py if os.path.exists(_venv_py) else sys.executable
                _master_proc = subprocess.Popen(
                    [_py, _master_script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                log.info("[WEB] Master Dashboard запущен (PID %d): http://localhost:%d/",
                         _master_proc.pid, _master_port)
            except Exception as _we:
                log.warning("[WEB] Не удалось запустить web_server.py: %s", _we)
        else:
            log.warning("[WEB] web_server.py не найден, пропускаю")

        # MCP API + watchdog
        _mcp_host = os.getenv("ARGOS_MCP_HOST", "0.0.0.0")
        _mcp_port = int(os.getenv("ARGOS_MCP_PORT", "8000") or "8000")
        _start_mcp_with_guard(self.core, self.admin, _mcp_host, _mcp_port)
        log.info("[MCP] Endpoint доступен: http://%s:%d/mcp", _mcp_host, _mcp_port)
        _start_mcp_watchdog(self.core, self.admin, _mcp_host, _mcp_port)

        self._start_telegram()

        stop_event = threading.Event()

        def _handle_signal(signum, frame):
            log.info("Получен сигнал %s — завершаю...", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT,  _handle_signal)

        log.info("[SERVER] Argos running. Press Ctrl+C to stop.")
        stop_event.wait()
        orchestrator.shutdown()


def main():
    argv = normalize_launch_args(sys.argv[1:])
    # argv — это list[str], не dict
    if "--mobile" in argv:
        mode = "mobile"
    elif "--desktop" in argv:
        mode = "desktop"
    else:
        mode = "server"
    orchestrator = ArgosOrchestrator()

    try:
        if mode == "desktop":
            orchestrator.boot_desktop()
        elif mode == "mobile":
            orchestrator.boot_mobile()
        else:
            orchestrator.boot_server()
    except Exception as e:
        log.error('Fatal startup error: %s', e)
        raise


if __name__ == '__main__':
    main()

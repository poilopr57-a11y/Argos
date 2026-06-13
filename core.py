"""
core.py — ArgosCore FINAL v2.0
    Все подсистемы интегрированы:
    ИИ + Контекст + Голос + Wake Word + Память + Планировщик +
    Алерты + Агент + Vision + P2P + Загрузчик + 50+ команд
"""
import os, threading, requests, asyncio, tempfile, importlib.util, re
import json
import time
import base64
import uuid
import subprocess
from collections import deque

# ── Graceful imports ──────────────────────────────────────
try:
    from google import genai as genai_sdk; GEMINI_OK = True
except ImportError:
    genai_sdk = None; GEMINI_OK = False

try:
    import pyttsx3; PYTTSX3_OK = True
except ImportError:
    pyttsx3 = None; PYTTSX3_OK = False

try:
    import speech_recognition as sr; SR_OK = True
except ImportError:
    sr = None; SR_OK = False

from src.quantum.logic               import ArgosQuantum
from src.skills.web_scrapper         import ArgosScrapper
from src.factory.replicator          import Replicator
from src.connectivity.sensor_bridge  import ArgosSensorBridge
try:
    from src.connectivity.system_health import PatchedSensorBridge as _HealthBridge, format_full_report as _fmt_health, format_io_report as _fmt_io, get_p2p_power_index as _get_p2p_power
    _HEALTH_OK = True
except Exception:
    _HealthBridge = None
    _HEALTH_OK = False
from src.connectivity.p2p_bridge     import ArgosBridge, p2p_protocol_roadmap
from src.skill_loader                import SkillLoader
from src.dag_agent                   import DAGManager
from src.github_marketplace          import GitHubMarketplace
from src.modules                     import ModuleLoader
from src.context_manager             import DialogContext
from src.agent                       import ArgosAgent
from src.argos_logger                import get_logger
try:
    from src.anti_hallucination import filter_answer as _filter_answer
    _ANTIHALLUC_OK = True
except Exception:
    _ANTIHALLUC_OK = False
    _filter_answer = lambda a, q="": a  # passthrough if module missing
from dotenv import load_dotenv
load_dotenv()

# [FIX-OLLAMA-AUTO] Автоподбор модели Ollama под железо системы
try:
    from src.ollama_autoselect import autoselect as _ollama_autoselect
    _OLLAMA_AUTOSELECT_OK = True
except Exception:
    _OLLAMA_AUTOSELECT_OK = False

log = get_logger("argos.core")
# [MIND v2] Модули разума
try:
    from src.mind.dreamer import Dreamer as _Dreamer
    from src.mind.evolution_engine import EvolutionEngine as _EvolutionEngine
    from src.mind.self_model_v2 import SelfModelV2 as _SelfModelV2
    _MIND_OK = True
except Exception as _mind_err:
    _MIND_OK = False
    _mind_err_msg = str(_mind_err)


_DEFAULT_PROVIDER_COOLDOWN_SECONDS = 600
_MIN_PROVIDER_COOLDOWN_SECONDS = 60
_MAX_PROVIDER_COOLDOWN_SECONDS = 3600

_PLACEHOLDER_SECRET_VALUES = {"", "your_key_here", "your_token_here", "none", "null", "changeme"}


def _read_secret_env(name: str) -> str:
    value = (os.getenv(name, "") or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if value.lower() in _PLACEHOLDER_SECRET_VALUES:
        return ""
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "on", "yes", "да", "вкл"}


# Маркеры смешаны (RU/EN), потому что ошибки приходят как от наших русских
# reason-строк, так и от англоязычных API/SSL исключений.
_PERMANENT_PROVIDER_ERROR_MARKERS = (
    "некорректный/просроченный api ключ",
    "ошибка авторизации http",
    "ssl сертификат не прошёл проверку",
    "api key expired",
    "invalid api key",
    "api_key_invalid",
    "certificate verify failed",
)


class _SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._hits = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            while self._hits and (now - self._hits[0]) >= self.window_seconds:
                self._hits.popleft()
            if len(self._hits) >= self.max_calls:
                return False
            self._hits.append(now)
            return True


class _GeminiResponse:
    def __init__(self, text: str = ""):
        self.text = text or ""


class _GeminiCompatClient:
    """Лёгкий адаптер google.genai под старый интерфейс generate_content()."""
    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash"):
        self.client = genai_sdk.Client(api_key=api_key)
        self.model_name = self._resolve_model_name(model_name)

    def _resolve_model_name(self, requested: str) -> str:
        env_model = os.getenv("GEMINI_MODEL", "").strip()
        if env_model:
            requested = env_model

        candidates = [
            requested,
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]

        try:
            available = []
            for model in self.client.models.list():
                name = getattr(model, "name", "") or ""
                if name:
                    available.append(name)

            if not available:
                return requested

            for cand in candidates:
                if cand in available:
                    return cand
                if f"models/{cand}" in available:
                    return f"models/{cand}"

            # Берём первую flash-модель, если есть
            for name in available:
                if "flash" in name.lower():
                    return name
            return available[0]
        except Exception:
            return requested

    def generate_content(self, contents):
        if isinstance(contents, list):
            prompt = "\n\n".join(str(x) for x in contents if isinstance(x, str) and x.strip())
        else:
            prompt = str(contents)
        try:
            resp = self.client.models.generate_content(model=self.model_name, contents=prompt)
        except Exception as first_error:
            # Попытка один раз переключиться на доступную модель (404/NOT_FOUND и совместимость API)
            new_model = self._resolve_model_name("gemini-2.0-flash")
            if new_model != self.model_name:
                self.model_name = new_model
                resp = self.client.models.generate_content(model=self.model_name, contents=prompt)
            else:
                raise first_error

        text = getattr(resp, "text", "") or ""
        return _GeminiResponse(text=text)


class ArgosCore:
    VERSION = "2.0.0"

    def __init__(self):
        self.quantum    = ArgosQuantum()
        self.scrapper   = ArgosScrapper()
        self.replicator = Replicator()
        # Используем PatchedSensorBridge (реальные данные) если доступен
        if _HEALTH_OK and _HealthBridge:
            self.sensors = _HealthBridge()
        else:
            self.sensors = ArgosSensorBridge()
        self.context    = DialogContext(max_turns=10)
        self.agent      = ArgosAgent(self)
        # Встроенный admin для случаев когда внешний не передан
        try:
            from src.admin import ArgosAdmin as _AA
            self._internal_admin = _AA()
        except Exception:
            self._internal_admin = None

        # Гарантируем наличие dispatch алиасов (защита от старых патчей)
        self._ensure_dispatch_aliases()
        self.ollama_url     = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/generate"
        self.opi            = None  # Orange Pi One bridge
        self.ai_mode    = self._normalize_ai_mode(os.getenv("ARGOS_AI_MODE", "auto"))
        self.voice_on   = os.getenv("ARGOS_VOICE_DEFAULT", "off").strip().lower() in (
            "1", "true", "on", "yes", "да", "вкл"
        )
        self.p2p        = None
        self.db         = None
        self.memory     = None
        self.scheduler  = None
        self.alerts     = None
        self.vision     = None
        self._boot      = None
        self._dashboard = None
        self._wake      = None
        self._tts_engine = None
        self._tts_lock = threading.Lock()
        self._whisper_model = None
        self.skill_loader = None
        self.dag_manager  = None
        self.marketplace  = None
        self.iot_bridge   = None
        self.iot_emulator = None
        self.mesh_net     = None
        self.smart_sys    = None
        self.gateway_mgr  = None
        self.industrial   = None
        self.platform_admin = None
        self.smart_profiles = {}
        self._smart_create_wizard = None
        self.operator_mode = False
        self.argoss_evolver = None
        self.module_loader = None
        self.ha = None
        self.tool_calling = None
        self.git_ops = None
        self.otg = None
        self.grist = None
        self.cloud_object_storage = None
        self.gemini_rpm_limit = 15
        self._gemini_limiter = _SlidingWindowRateLimiter(max_calls=self.gemini_rpm_limit, window_seconds=60)
        self._last_gemini_rate_limited = False
        self._gigachat_access_token = _read_secret_env("GIGACHAT_ACCESS_TOKEN") or None
        self._gigachat_token_expires_at = 0.0
        self._gigachat_ssl_verify = _env_flag("GIGACHAT_SSL_VERIFY", True)
        self._gigachat_ssl_insecure_fallback = _env_flag("GIGACHAT_SSL_INSECURE_FALLBACK", True)
        self._gigachat_ca_bundle = (os.getenv("GIGACHAT_CA_BUNDLE", "") or "").strip()
        self._gigachat_rotation_enabled = _env_flag("GIGACHAT_ROTATION_ENABLED", True)
        self._gigachat_model_max = (os.getenv("GIGACHAT_MODEL_MAX", "GigaChat-Max") or "GigaChat-Max").strip()
        self._gigachat_model_pro = (os.getenv("GIGACHAT_MODEL_PRO", "GigaChat-Pro") or "GigaChat-Pro").strip()
        self._gigachat_model_lite = (os.getenv("GIGACHAT_MODEL_LITE", "GigaChat") or "GigaChat").strip()
        self._gigachat_balance: dict[str, int] = {
            self._gigachat_model_max: int(os.getenv("GIGACHAT_BALANCE_MAX", "50000") or "50000"),
            self._gigachat_model_pro: int(os.getenv("GIGACHAT_BALANCE_PRO", "50000") or "50000"),
            self._gigachat_model_lite: int(os.getenv("GIGACHAT_BALANCE_LITE", "766192") or "766192"),
        }
        self._gigachat_balance_lock = threading.Lock()
        cooldown_raw = os.getenv("ARGOS_PROVIDER_FAILURE_COOLDOWN", str(_DEFAULT_PROVIDER_COOLDOWN_SECONDS))
        try:
            cooldown_seconds = int(cooldown_raw)
        except ValueError:
            cooldown_seconds = _DEFAULT_PROVIDER_COOLDOWN_SECONDS
            log.warning(
                "ARGOS_PROVIDER_FAILURE_COOLDOWN=%r некорректен, используется значение по умолчанию %s сек",
                cooldown_raw,
                _DEFAULT_PROVIDER_COOLDOWN_SECONDS,
            )
        # Ограничиваем окно на разумный диапазон: 1 минута .. 1 час.
        self.provider_failure_cooldown_seconds = max(
            _MIN_PROVIDER_COOLDOWN_SECONDS,
            min(cooldown_seconds, _MAX_PROVIDER_COOLDOWN_SECONDS),
        )
        self._provider_disabled_until: dict[str, float] = {}
        self._provider_disable_reason: dict[str, str] = {}
        self._provider_disabled_permanent: dict[str, str] = {}
        self.auto_collab_enabled = os.getenv("ARGOS_AUTO_COLLAB", "on").strip().lower() not in {"0", "false", "off", "no", "нет"}
        self.auto_collab_max_models = max(2, min(int(os.getenv("ARGOS_AUTO_COLLAB_MAX_MODELS", "4") or "4"), 4))
        self.homeostasis = None
        self.curiosity = None
        self._homeostasis_block_heavy = False
        self.web_explorer = None
        self.awa = None
        self.sustain = None
        self.health_monitor = None
        self.failover = None

        self._init_voice()
        self._setup_ai()
        self._init_memory()
        self._init_homeostasis()
        self._init_curiosity()
        self._init_scheduler()
        self._init_alerts()
        self._init_vision()
        self._init_skills()
        self._init_dags()
        self._init_marketplace()
        self._init_iot()
        self._init_industrial()
        self._init_platform_admin()
        self._init_smart_systems()
        self._init_home_assistant()
        self._init_modules()
        self._init_tool_calling()
        self._init_git_ops()
        self._init_otg()
        self._init_grist()
        self._init_cloud_object_storage()
        self._init_own_model()
        self._init_argoss_evolver()
        self._init_opi()
        self._init_web_explorer()
        self._init_awa_core()
        self._init_sustain()
        self._init_health_monitor()
        self._init_ai_failover()
        
        # [MIND v2] Инициализация модулей разума
        self.self_model_v2  = None
        self.dreamer        = None
        self.evolution_engine = None
        if _MIND_OK:
            try:
                self.self_model_v2 = _SelfModelV2(self)
                log.info("SelfModelV2: OK")
            except Exception as e:
                log.warning("SelfModelV2: %s", e)
            try:
                self.dreamer = _Dreamer(self)
                self.dreamer.start()
                log.info("Dreamer: OK")
            except Exception as e:
                log.warning("Dreamer: %s", e)
            try:
                self.evolution_engine = _EvolutionEngine(self)
                log.info("EvolutionEngine: OK")
            except Exception as e:
                log.warning("EvolutionEngine: %s", e)
        else:
            log.warning("Mind modules недоступны: %s", _mind_err_msg)

        log.info("ArgosCore FINAL v2.0 инициализирован.")

    # ═══════════════════════════════════════════════════════
    # ИНИЦИАЛИЗАЦИЯ ПОДСИСТЕМ
    # ═══════════════════════════════════════════════════════
    def _init_memory(self):
        try:
            from src.memory import ArgosMemory
            self.memory = ArgosMemory()
            # Graceful-load input controller
            try:
                from src.input_control import get_input_ctrl as _get_input_ctrl
                self.input_ctrl = _get_input_ctrl()
            except Exception:
                self.input_ctrl = None
            # Graceful-load ThoughtBook
            try:
                from src.thought_book import ArgosThoughtBook
                self.thought_book = ArgosThoughtBook(core=self)
            except Exception:
                self.thought_book = None
            self.context.memory_ref = self.memory
            log.info("Память: OK")
        except Exception as e:
            log.warning("Память недоступна: %s", e)

    def _init_cloud_object_storage(self):
        try:
            from src.connectivity.cloud_object_storage import IBMCloudObjectStorage
            self.cloud_object_storage = IBMCloudObjectStorage.from_env()
            if self.cloud_object_storage.is_configured():
                log.info(self.cloud_object_storage.status())
        except Exception as e:
            log.warning("IBM Cloud Object Storage недоступен: %s", e)

    def _init_scheduler(self):
        try:
            from src.skills.scheduler import ArgosScheduler
            self.scheduler = ArgosScheduler(core=self)
            self.scheduler.start()
            log.info("Планировщик: OK")
        except Exception as e:
            log.warning("Планировщик: %s", e)

    def _init_homeostasis(self):
        try:
            from src.hardware_guard import HardwareHomeostasisGuard
            self.homeostasis = HardwareHomeostasisGuard(core=self)
            if os.getenv("ARGOS_HOMEOSTASIS", "on").strip().lower() not in {"0", "off", "false", "no", "нет"}:
                self.homeostasis.start()
            log.info("Homeostasis: OK")
        except Exception as e:
            log.warning("Homeostasis: %s", e)

    def _init_curiosity(self):
        try:
            from src.curiosity import ArgosCuriosity
            self.curiosity = ArgosCuriosity(core=self)
            if os.getenv("ARGOS_CURIOSITY", "on").strip().lower() not in {"0", "off", "false", "no", "нет"}:
                self.curiosity.start()
            log.info("Curiosity: OK")
        except Exception as e:
            log.warning("Curiosity: %s", e)

    def _init_alerts(self):
        try:
            from src.connectivity.alert_system import AlertSystem
            self.alerts = AlertSystem(on_alert=self._on_alert)
            self.alerts.start(interval_sec=30)
            log.info("Алерты: OK")
        except Exception as e:
            log.warning("Алерты: %s", e)

    def _init_vision(self):
        try:
            from src.vision import ArgosVision
            self.vision = ArgosVision()
            log.info("Vision: OK")
        except Exception as e:
            log.warning("Vision: %s", e)

    def _init_skills(self):
        try:
            self.skill_loader = SkillLoader()
            report = self.skill_loader.load_all(core=self)
            log.info("SkillLoader: OK")
            log.info(report.replace("\n", " | "))
        except Exception as e:
            log.warning("SkillLoader (base): %s", e)
            self.skill_loader = None
        # Расширяем SkillLoader для загрузки flat .py навыков
        try:
            from src.skill_loader_patch import PatchedSkillLoader
            self.skill_loader = PatchedSkillLoader(original_loader=self.skill_loader)
            extra = self.skill_loader.load_all(core=self)
            log.info("PatchedSkillLoader: OK")
        except ImportError:
            pass  # skill_loader_patch.py не установлен — используем базовый
        except Exception as e:
            log.warning("PatchedSkillLoader: %s", e)
        if False:
            log.warning("SkillLoader: %s", e)

    def _init_dags(self):
        try:
            self.dag_manager = DAGManager(core=self)
            log.info("DAG Manager: OK")
        except Exception as e:
            log.warning("DAG Manager: %s", e)

    def _init_marketplace(self):
        try:
            self.marketplace = GitHubMarketplace(skill_loader=self.skill_loader, core=self)
            log.info("GitHub Marketplace: OK")
        except Exception as e:
            log.warning("GitHub Marketplace: %s", e)

    def _init_iot(self):
        """IoT Bridge + Mesh Network + Gateway Manager + IoT Emulators."""
        try:
            from src.connectivity.iot_bridge import IoTBridge
            self.iot_bridge = IoTBridge()
            log.info("IoT Bridge: OK (%d устройств)", len(self.iot_bridge.registry.all()))
        except Exception as e:
            log.warning("IoT Bridge: %s", e)

        try:
            from src.connectivity.iot_emulator import IotEmulatorManager
            mqtt_host = os.getenv("MQTT_HOST", "localhost")
            mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
            self.iot_emulator = IotEmulatorManager(mqtt_host=mqtt_host, mqtt_port=mqtt_port)
            log.info("IoT Emulator Manager: OK")
        except Exception as e:
            log.warning("IoT Emulator Manager: %s", e)

        try:
            from src.connectivity.mesh_network import MeshNetwork
            self.mesh_net = MeshNetwork()
            log.info("Mesh Network: OK (%d устройств)", len(self.mesh_net.devices))
        except Exception as e:
            log.warning("Mesh Network: %s", e)

        try:
            from src.connectivity.gateway_manager import GatewayManager
            self.gateway_mgr = GatewayManager(iot_bridge=self.iot_bridge)
            log.info("Gateway Manager: OK")
        except Exception as e:
            log.warning("Gateway Manager: %s", e)

    def _init_industrial(self):
        """Industrial Protocols Manager — KNX / LonWorks / M-Bus / OPC-UA."""
        try:
            from industrial_protocols import IndustrialProtocolsManager
            self.industrial = IndustrialProtocolsManager(core=self)
            log.info("Industrial Protocols: OK (KNX/LON/M-Bus/OPC-UA)")
        except Exception as e:
            log.warning("Industrial Protocols: %s", e)

    def _init_platform_admin(self):
        """Platform Admin — Linux / Windows / Android управление."""
        try:
            from src.platform_admin import PlatformAdmin
            self.platform_admin = PlatformAdmin(core=self)
            log.info("PlatformAdmin: OK (os=%s)", self.platform_admin.os)
        except Exception as e:
            log.warning("PlatformAdmin: %s", e)

    def _init_smart_systems(self):
        """Smart Systems Manager — умные среды."""
        try:
            from src.smart_systems import SmartSystemsManager, SYSTEM_PROFILES
            self.smart_sys = SmartSystemsManager(on_alert=self._on_alert)
            self.smart_profiles = SYSTEM_PROFILES
            log.info("Smart Systems: OK (%d систем)", len(self.smart_sys.systems))
        except Exception as e:
            log.warning("Smart Systems: %s", e)

    def _init_modules(self):
        """Dynamic modules (src/modules/*_module.py)."""
        try:
            self.module_loader = ModuleLoader()
            report = self.module_loader.load_all(core=self)
            log.info(report.replace("\n", " | "))
        except Exception as e:
            log.warning("Modules: %s", e)

    def _init_home_assistant(self):
        try:
            from src.connectivity.home_assistant import HomeAssistantBridge
            self.ha = HomeAssistantBridge()
            log.info("Home Assistant bridge: %s", "ON" if self.ha.enabled else "OFF")
        except Exception as e:
            log.warning("Home Assistant bridge: %s", e)


    def _ensure_dispatch_aliases(self):
        """Гарантирует наличие всех dispatch алиасов в классе."""
        cls = type(self)
        if not hasattr(cls, '_dispatch_skill'):
            cls._dispatch_skill = lambda s, txt, t=None: None
        if not hasattr(cls, 'dispatchskill'):
            cls.dispatchskill   = lambda s, txt, t=None: s._dispatch_skill(txt, t)
        if not hasattr(cls, 'dispatch_skill'):
            cls.dispatch_skill  = lambda s, txt, t='':   s._dispatch_skill(txt, t)
        if not hasattr(cls, '_run_dispatch'):
            cls._run_dispatch   = lambda s, txt, t='':   s._dispatch_skill(txt, t)

    def _init_tool_calling(self):
        self.tool_calling = None  # ToolCalling отключён навсегда

    def _init_git_ops(self):
        try:
            from src.git_ops import ArgosGitOps
            self.git_ops = ArgosGitOps(repo_path=".")
            log.info("GitOps: OK")
        except Exception as e:
            log.warning("GitOps: %s", e)

    def _init_otg(self):
        try:
            from src.connectivity.otg_manager import OTGManager
            self.otg = OTGManager()
            log.info("OTG Manager: OK")
        except Exception as e:
            self.otg = None
            log.warning("OTG Manager: %s", e)

    def _init_grist(self):
        return  # disabled
        try:
            from src.knowledge.grist_storage import GristStorage
            self.grist = GristStorage()
            if self.memory and hasattr(self.memory, "attach_grist"):
                self.memory.attach_grist(self.grist)
            log.info("Grist Storage: OK (настроен=%s)", self.grist._configured)
        except Exception as e:
            self.grist = None
            log.warning("Grist Storage: %s", e)

    def _init_own_model(self):
        try:
            from src.argos_model import ArgosOwnModel
            self.own_model = ArgosOwnModel()
            log.info("OwnModel: OK")
        except Exception as e:
            self.own_model = None
            log.warning("OwnModel: %s", e)

    def _init_argoss_evolver(self):
        """Инициализация движка развития личной модели Аргоса."""
        try:
            from src.argoss_evolver import ArgossEvolver
            self.argoss_evolver = ArgossEvolver(core=self)
            log.info("ArgossEvolver: OK (модель: %s, версия: v%d)",
                     self.argoss_evolver._meta.base_model,
                     self.argoss_evolver._meta.current_version)
        except Exception as e:
            self.argoss_evolver = None
            log.warning("ArgossEvolver: %s", e)

    def _init_opi(self):
        """Инициализация моста Orange Pi One (GPIO/I2C/UART/1-Wire/RS-485/Modbus)."""
        try:
            from src.connectivity.orangepi_bridge import OrangePiBridge
            self.opi = OrangePiBridge(core=self)
            log.info("OrangePiBridge: OK (платформа=%s)", self.opi._platform)
        except Exception as e:
            self.opi = None
            log.warning("OrangePiBridge: %s", e)

    def _init_web_explorer(self):
        """Инициализация бесплатного интернет-разведчика."""
        try:
            from src.skills.web_explorer import ArgosWebExplorer
            self.web_explorer = ArgosWebExplorer(memory=self.memory)
            # Подключаем к scrapper для обратной совместимости
            if hasattr(self.scrapper, '__class__'):
                self.scrapper.__class__.learn = lambda self_s, q: self.web_explorer.learn(q)
            log.info("WebExplorer: OK (DuckDuckGo/Wikipedia/GitHub/arXiv)")
        except Exception as e:
            self.web_explorer = None
            log.warning("WebExplorer: %s", e)

    def _init_awa_core(self):
        """Инициализация AWA-Core (Model Splitting маршрутизатор)."""
        try:
            from src.awa_core import AWACore
            self.awa = AWACore(core=self)
            # Подключаем ContextDB к DialogContext
            if self.memory:
                try:
                    from src.db_init import ContextDB
                    self.context.db = ContextDB()
                    log.info("ContextDB: подключена к DialogContext")
                except Exception as e:
                    log.warning("ContextDB init: %s", e)
            log.info("AWA-Core: OK (Model Splitting активен)")
        except Exception as e:
            self.awa = None
            log.warning("AWA-Core: %s", e)

    def _init_sustain(self):
        """Инициализация модуля самообеспечения."""
        try:
            from src.self_sustain import SelfSustainEngine
            self.sustain = SelfSustainEngine(core=self)
            if os.getenv("ARGOS_SUSTAIN", "on").strip().lower() not in {
                "0", "off", "false", "no", "нет"
            }:
                self.sustain.start()
            log.info("SelfSustain: OK")
        except Exception as e:
            self.sustain = None
            log.warning("SelfSustain: %s", e)

    def _init_health_monitor(self):
        """Инициализация фонового мониторинга здоровья системы."""
        try:
            from src.health_monitor import HealthMonitor
            alert_cb = getattr(self.alerts, 'send', None) if self.alerts else None
            self.health_monitor = HealthMonitor(
                db_path="data/argos.db",
                alert_callback=alert_cb,
            )
            self.health_monitor.start()
            log.info("HealthMonitor: OK")
        except Exception as e:
            self.health_monitor = None
            log.warning("HealthMonitor: %s", e)

    def _init_ai_failover(self):
        """Инициализация модуля автоматического переключения AI-провайдеров."""
        try:
            from src.ai_failover import get_failover
            self.failover = get_failover()
            log.info("AIFailover: OK")
        except Exception as e:
            self.failover = None
            log.warning("AIFailover: %s", e)


    # ── КОМАНДЫ ПРЯМОГО ИСПОЛНЕНИЯ ───────────────────────────────────────────
    # Эти команды выполняются напрямую через admin/OPi/etc — ДО любого LLM,
    # ToolCalling, агентов и плагинов. Гарантия что реальный код выполнится.

    _DIRECT_PREFIXES = (
        # Файлы
        "создай файл", "напиши файл",
        "прочитай файл", "открой файл",
        "удали файл", "удали папку",
        "покажи файлы", "список файлов", "файлы ",
        "добавь в файл", "допиши в файл", "дополни файл",
        "отредактируй файл", "измени файл", "замени в файле",
        "скопируй файл", "переименуй файл",
        # Терминал и процессы
        "консоль ", "терминал ",
        "список процессов", "убей процесс",
        "статус системы", "чек-ап",
        # Навыки — прямой запуск (широкие триггеры)
        # Крипто
        "крипто", "биткоин", "bitcoin", "ethereum", "btc", "eth",
        "курс валют", "курс крипто", "цена биткоин",
        # Сканер сети
        "сканируй сеть", "сетевой призрак", "скан сети",
        "сканируй порты", "скан портов", "запуск сканера",
        "сканер", "скан ", "сетевой скан", "запустить сканер",
        "nmap", "network scan", "порты хоста",
        # Дайджест
        "дайджест", "опубликуй", "новости ии", "ai новости",
        # Погода
        "погода", "weather", "температура на улице", "прогноз",
        # Навыки управление
        "список навыков", "навыки аргоса", "доступные навыки",
        "напиши навык", "создай навык",
        "загрузи навык", "выгрузи навык", "перезагрузи навык",
        # Железо
        "проверь железо", "железо инфо", "hardware",
        # HuggingFace
        "huggingface", "hf модель", "hf запрос",
        "hf status", "hf статус", "hf spaces", "hf space ",
        "hf voiceclone", "hf joycaption", "hf sentiment", "hf finance",
        "hf datasetgen", "hf echo", "hf netgoat", "hf prompts",
        "hf random", "hf index", "hf search", "hf ask ", "hf embed",
        "голос клон", "клон голоса", "описание фото hf",
        "тональность hf", "финансовый анализ hf",
        "сетевой анализ hf", "промпты hf",
        # Tasmota
        "обнови тасмота", "tasmota",
        # Browser
        "браузер запрос", "browser conduit",
        # Shodan
        "shodan", "shodan скан",
        # Git
        "git статус", "git коммит", "git пуш", "git автокоммит",
        "гит статус", "гит пуш",
        # Память
        "запомни ", "забудь ",
        "запиши заметку", "мои заметки",
        "найди в памяти", "поиск по памяти",
        # Планировщик
        "каждые ", "напомни ", "ежедневно",
        # Голос
        "голос вкл", "голос выкл",
        # Режим AI
        "режим ии ", "текущий режим ии",
        # Диагностика
        "диагностика навыков", "проверь навыки", "навыки статус",
        "диагностика ии", "проверь работу ии",
        # IoT / OPi
        "iot статус", "opi ", "i2c ", "gpio ",
        "modbus ", "uart ", "rs485",
        # P2P
        "запусти p2p", "статус сети",
        # Алерты
        "статус алертов", "установи порог",
        # Умные системы
        "умные системы", "добавь систему",
        # Квантовый оракул
        "оракул статус", "оракул семя",
        # Интернет-поиск
        "изучи ", "найди в интернете", "погугли ",
        "что такое ", "расскажи про ",
    )

    def _direct_dispatch(self, text: str, admin) -> str | None:
        """
        Прямой диспетчер: выполняет команды немедленно, минуя LLM полностью.
        Возвращает строку-ответ или None если команда не распознана.
        """
        t = text.lower().strip()

        # Проверяем префиксы прямых команд
        matched = any(t.startswith(p) or t == p.strip() for p in self._DIRECT_PREFIXES)
        if not matched:
            return None

        # Гарантируем admin
        if admin is None:
            admin = getattr(self, "_internal_admin", None)
        if admin is None:
            try:
                from src.admin import ArgosAdmin as _AA
                admin = _AA()
                self._internal_admin = admin
            except Exception as e:
                return f"❌ Невозможно выполнить команду: admin недоступен ({e})"

        # Выполняем команду
        try:
            result = self.execute_intent(text, admin, None)
            if result is not None:
                return result
        except Exception as e:
            return f"❌ Ошибка выполнения: {e}"

        return None

    def process(self, user_text: str, admin=None, flasher=None) -> dict:
        """Обёртка над process_logic с дефолтными значениями admin/flasher."""
        if admin is None:
            admin = getattr(self, "_internal_admin", None)
        return self.process_logic(user_text, admin, flasher)

    def _on_alert(self, msg: str):
        log.warning("ALERT: %s", msg)
        self.say(msg)

    def _remember_dialog_turn(self, user_text: str, answer: str, state: str):
        if not self.memory:
            return
        try:
            self.memory.log_dialogue("user", user_text, state=state)
            self.memory.log_dialogue("argos", answer, state=state)
        except Exception as e:
            log.warning("Memory dialogue index: %s", e)

    # ═══════════════════════════════════════════════════════
    # P2P / DASHBOARD / WAKE WORD
    # ═══════════════════════════════════════════════════════
    def start_p2p(self) -> str:
        self.p2p = ArgosBridge(core=self)
        result = self.p2p.start()
        log.info("P2P: %s", result.split('\n')[0])
        return result

    def start_dashboard(self, admin, flasher, port: int = 8080) -> str:
        try:
            from src.interface.fastapi_dashboard import FastAPIDashboard
            self._dashboard = FastAPIDashboard(self, admin, flasher, port)
            result = self._dashboard.start()
            if isinstance(result, str) and not result.startswith("❌"):
                return result
        except Exception:
            pass

        try:
            from src.interface.web_dashboard import WebDashboard
            self._dashboard = WebDashboard(self, admin, flasher, port)
            return self._dashboard.start()
        except Exception as e:
            return f"❌ Dashboard: {e}"

    def start_wake_word(self, admin, flasher) -> str:
        try:
            from src.connectivity.wake_word import WakeWordListener
            self._wake = WakeWordListener(self, admin, flasher)
            return self._wake.start()
        except Exception as e:
            return f"❌ Wake Word: {e}"

    # ═══════════════════════════════════════════════════════
    # ГОЛОС
    # ═══════════════════════════════════════════════════════
    def _init_voice(self):
        if not PYTTSX3_OK:
            log.warning("pyttsx3 не установлен: pip install pyttsx3")
            return
        try:
            self._tts_engine = pyttsx3.init()
            for v in self._tts_engine.getProperty('voices'):
                if "Russian" in v.name or "ru" in v.id:
                    self._tts_engine.setProperty('voice', v.id)
                    break
            self._tts_engine.setProperty('rate', 175)
            log.info("TTS: OK")
        except Exception as e:
            self._tts_engine = None
            log.warning("TTS недоступен: %s", e)

    def say(self, text: str):
        if not self.voice_on or not self._tts_engine:
            return
        def _speak():
            try:
                with self._tts_lock:
                    self._tts_engine.say(text[:300])
                    self._tts_engine.runAndWait()
            except Exception as e:
                log.warning("TTS runtime error: %s", e)
        threading.Thread(target=_speak, daemon=True).start()

    def listen(self) -> str:
        if SR_OK:
            try:
                rec = sr.Recognizer()
                with sr.Microphone() as src:
                    log.info("Слушаю...")
                    rec.adjust_for_ambient_noise(src, duration=0.5)
                    audio = rec.listen(src, timeout=7, phrase_time_limit=15)
                    try:
                        text = rec.recognize_google(audio, language="ru-RU")
                        log.info("Распознано (google): %s", text)
                        return text.lower()
                    except Exception:
                        text = self._transcribe_with_whisper(audio)
                        if text:
                            log.info("Распознано (whisper): %s", text)
                            return text.lower()
            except Exception as e:
                log.error("STT: %s", e)

        log.warning("STT недоступен (SpeechRecognition/Whisper)")
        return ""

    def _transcribe_with_whisper(self, audio_data) -> str:
        try:
            if self._whisper_model is None:
                from faster_whisper import WhisperModel
                model_size = os.getenv("WHISPER_MODEL", "small")
                device = os.getenv("WHISPER_DEVICE", "cpu")
                compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
                self._whisper_model = WhisperModel(model_size, device=device, compute_type=compute)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_data.get_wav_data())
                wav_path = tmp.name

            segments, _ = self._whisper_model.transcribe(wav_path, language="ru", vad_filter=True)
            text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
            try:
                os.remove(wav_path)
            except Exception:
                pass
            return text
        except Exception as e:
            log.warning("Whisper STT fallback: %s", e)
            return ""

    def transcribe_audio_path(self, audio_path: str) -> str:
        """Транскрибация аудиофайла (ogg/mp3/wav) через faster-whisper."""
        if not audio_path or not os.path.exists(audio_path):
            return ""
        try:
            if self._whisper_model is None:
                from faster_whisper import WhisperModel
                model_size = os.getenv("WHISPER_MODEL", "small")
                device = os.getenv("WHISPER_DEVICE", "cpu")
                compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
                self._whisper_model = WhisperModel(model_size, device=device, compute_type=compute)

            segments, _ = self._whisper_model.transcribe(audio_path, language="ru", vad_filter=True)
            text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
            return text.strip()
        except Exception as e:
            log.warning("Whisper file STT: %s", e)
            return ""

    def voice_services_report(self) -> str:
        tts_ready = bool(PYTTSX3_OK and self._tts_engine)
        stt_live_ready = bool(SR_OK)
        stt_file_ready = bool(importlib.util.find_spec("faster_whisper"))
        voice_mode = "ВКЛ" if self.voice_on else "ВЫКЛ"
        return (
            "🎙 Проверка голосовых служб:\n"
            f"• Голосовой вывод (TTS): {'✅ готов' if tts_ready else '❌ недоступен'}\n"
            f"• Голосовой ввод (микрофон): {'✅ готов' if stt_live_ready else '❌ недоступен'}\n"
            f"• Голосовой ввод (аудиофайлы): {'✅ готов' if stt_file_ready else '❌ недоступен'}\n"
            f"• Текущий голосовой режим: {voice_mode}"
        )

    # ═══════════════════════════════════════════════════════
    # ИИ
    # ═══════════════════════════════════════════════════════
    def _normalize_ai_mode(self, mode: str) -> str:
        value = (mode or "auto").strip().lower()
        if value in {"gemini", "google", "g"}:
            return "gemini"
        if value in {"gigachat", "giga", "sber", "gc"}:
            return "gigachat"
        if value in {"yandexgpt", "yandex", "ya", "yg"}:
            return "yandexgpt"
        if value in {"ollama", "local", "o"}:
            return "ollama"
        if value in {"pi", "p", "pi-coding", "coding-agent"}:
            return "pi"
        if value in {"groq", "gr"}:
            return "groq"
        if value in {"deepseek", "ds"}:
            return "deepseek"
        if value in {"openai", "gpt", "gpt4"}:
            return "openai"
        return "auto"

    def set_ai_mode(self, mode: str) -> str:
        self.ai_mode = self._normalize_ai_mode(mode)
        return f"🤖 Режим ИИ: {self.ai_mode_label()}"

    def ai_mode_label(self) -> str:
        labels = {
            "gemini":    "Gemini",
            "gigachat":  "GigaChat",
            "yandexgpt": "YandexGPT",
            "ollama":    "Ollama",
            "groq":      "Groq",
            "deepseek":  "DeepSeek",
            "openai":    "OpenAI",
            "pi":        "Pi Coding Agent",
        }
        return labels.get(self.ai_mode, "Auto")

    def _setup_ai(self):
        key = _read_secret_env("GEMINI_API_KEY")
        # Совместимость с форматами GEMINI_API_KEY_0 и GEMINI_API_KEY0.
        if not key:
            for i in range(20):
                key = _read_secret_env(f"GEMINI_API_KEY_{i}") or _read_secret_env(f"GEMINI_API_KEY{i}")
                if key:
                    break
        if GEMINI_OK and key:
            self.model = _GeminiCompatClient(api_key=key, model_name="gemini-2.0-flash")
            log.info("Gemini: OK")
        else:
            self.model = None
            log.info("Gemini недоступен — используется Ollama")

        # Always start Ollama so it is ready as a fallback even when a cloud
        # provider (e.g. Gemini) is configured but later turns out to have an
        # expired or invalid API key.
        ollama_ok = self._ensure_ollama_running()
        if ollama_ok:
            log.info("Ollama: ✅ доступна (резервный провайдер готов)")
        else:
            log.warning("Ollama: ❌ недоступна — резервный локальный провайдер не запущен")

        if self._has_gigachat_config():
            log.info("GigaChat: конфигурация обнаружена")
        else:
            log.info("GigaChat недоступен — нет credentials")

        if self._has_yandexgpt_config():
            log.info("YandexGPT: конфигурация обнаружена")
        else:
            log.info("YandexGPT недоступен — нет IAM/FOLDER")

        # Pi Coding Agent
        if self._has_pi_config():
            log.info("Pi Coding Agent: ✅ обнаружен")
        else:
            log.info("Pi Coding Agent недоступен — не установлен")

    def _gemini_rate_limit_text(self) -> str:
        return f"Gemini: превышен лимит {self.gemini_rpm_limit} запросов в минуту. Повтори чуть позже или переключи режим ИИ."

    @staticmethod
    def _is_host_reachable(host: str, port: int = 443, timeout: float = 2.0) -> bool:
        """Быстрая проверка TCP-доступности хоста перед HTTP-запросом.

        Возвращает False если DNS не резолвится или соединение недоступно.
        Позволяет избежать лишних ошибок в лог при работе в офлайн/CI среде.
        """
        import socket as _socket
        try:
            with _socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _has_gigachat_config(self) -> bool:
        if self._gigachat_access_token:
            return True
        client_id = _read_secret_env("GIGACHAT_CLIENT_ID")
        client_secret = _read_secret_env("GIGACHAT_CLIENT_SECRET")
        return bool(client_id and client_secret)

    def _has_yandexgpt_config(self) -> bool:
        iam = _read_secret_env("YANDEX_IAM_TOKEN")
        folder = _read_secret_env("YANDEX_FOLDER_ID")
        return bool(iam and folder)

    # ═══════════════════════════════════════════════════════
    # Pi Coding Agent Integration
    # ═══════════════════════════════════════════════════════
    def _has_pi_config(self) -> bool:
        """Проверяет наличие Pi в системе."""
        import shutil
        return bool(shutil.which("pi") or os.path.exists(r"C:\Users\AvA\AppData\Roaming\npm\pi.cmd"))

    def _ensure_pi_ready(self) -> bool:
        """Проверяет готовность Pi."""
        try:
            result = subprocess.run(
                ["pi", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def _ask_pi(self, context: str, user_text: str) -> str | None:
        """Запрос к Pi Coding Agent через --print --mode json."""
        if not self._has_pi_config():
            log.debug("[Pi] Pi не найден в системе")
            return None
        if self._is_provider_temporarily_disabled("Pi"):
            return None
        
        try:
            # Формируем промпт для Pi
            full_prompt = f"""Ты — Argos. Контекст системы:
{context}

История диалога:
{self.context.get_prompt_context()}

Вопрос пользователя: {user_text}

Отвечай кратко и по делу на русском языке. Выполняй задачи, а не описывай их."""

            # Вызываем Pi в JSON режиме
            result = subprocess.run(
                ["pi", "--print", "--mode", "json", "--no-session", full_prompt],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                log.error("[Pi] Ошибка выполнения: %s", result.stderr[:500])
                return None
            
            # Pi возвращает JSON с полем "content"
            import json as _json
            try:
                response_data = _json.loads(result.stdout)
                text = response_data.get("content", "")
                if text:
                    log.info("[Pi] ✅ Ответ получен (%d симв.)", len(text))
                    return text.strip()
            except _json.JSONDecodeError:
                # Если не JSON — возвращаем как есть
                text = result.stdout.strip()
                if text:
                    return text
            
            return None
            
        except subprocess.TimeoutExpired:
            log.error("[Pi] Таймаут выполнения (120 сек)")
            self._disable_provider_temporarily("Pi", "таймаут выполнения")
            return None
        except Exception as e:
            log.error("[Pi] Ошибка: %s", e)
            return None

    def _get_pi_capabilities(self) -> dict:
        """Возвращает возможности Pi для контекста."""
        return {
            "name": "Pi Coding Agent",
            "tools": ["read", "write", "edit", "bash", "web_search", "web_fetch"],
            "languages": ["Python", "JavaScript", "TypeScript", "C", "C++", "Java", "Rust", "Go", "и другие"],
            "features": ["автоисследование (autoresearch)", "оптимизация кода", "исправление багов", "рефакторинг"]
        }

    def _is_provider_temporarily_disabled(self, provider_name: str) -> bool:
        if provider_name in self._provider_disabled_permanent:
            return True
        until = float(self._provider_disabled_until.get(provider_name, 0.0))
        if until <= time.time():
            self._provider_disabled_until.pop(provider_name, None)
            self._provider_disable_reason.pop(provider_name, None)
            return False
        return True

    def _disable_provider_temporarily(self, provider_name: str, reason: str) -> None:
        reason_lower = reason.lower() if isinstance(reason, str) else ""
        if any(x in reason_lower for x in _PERMANENT_PROVIDER_ERROR_MARKERS):
            if provider_name not in self._provider_disabled_permanent:
                self._provider_disabled_permanent[provider_name] = reason
                log.warning("%s отключен до перезапуска: %s", provider_name, reason)
            return
        was_already_disabled = self._is_provider_temporarily_disabled(provider_name)
        self._provider_disabled_until[provider_name] = time.time() + self.provider_failure_cooldown_seconds
        self._provider_disable_reason[provider_name] = reason
        if not was_already_disabled:
            log.warning(
                "%s временно отключен на %s сек: %s",
                provider_name,
                self.provider_failure_cooldown_seconds,
                reason,
            )

    def _gigachat_verify_option(self):
        if not self._gigachat_ssl_verify:
            return False
        if self._gigachat_ca_bundle:
            if os.path.exists(self._gigachat_ca_bundle):
                return self._gigachat_ca_bundle
            log.warning("GigaChat: CA bundle не найден: %s", self._gigachat_ca_bundle)
        return True

    def _gigachat_post(self, url: str, *, headers: dict, timeout: int, data=None, json_payload=None):
        verify_opt = self._gigachat_verify_option()
        try:
            return requests.post(
                url,
                headers=headers,
                data=data,
                json=json_payload,
                timeout=timeout,
                verify=verify_opt,
            )
        except requests.exceptions.SSLError as ssl_err:
            if verify_opt is not False and self._gigachat_ssl_insecure_fallback:
                log.warning(
                    "GigaChat SSL verify failed (%s). Повтор с verify=False (GIGACHAT_SSL_INSECURE_FALLBACK=on).",
                    ssl_err,
                )
                return requests.post(
                    url,
                    headers=headers,
                    data=data,
                    json=json_payload,
                    timeout=timeout,
                    verify=False,
                )
            raise

    @staticmethod
    def _estimate_tokens_fast(*chunks: str) -> int:
        text = " ".join((c or "") for c in chunks if c)
        if not text:
            return 1
        # Грубая оценка токенов для баланс-менеджмента
        return max(1, int(len(text) / 4))

    def _select_gigachat_model(self, context: str, user_text: str) -> str:
        if not self._gigachat_rotation_enabled:
            return (os.getenv("GIGACHAT_MODEL", "GigaChat-2") or "GigaChat-2").strip()

        need_tokens = self._estimate_tokens_fast(context, user_text)
        with self._gigachat_balance_lock:
            candidates = sorted(
                self._gigachat_balance.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )
            for model, left in candidates:
                if left > need_tokens:
                    return model
            # Если по оценке токенов все пусто — берем модель с наибольшим остатком.
            return candidates[0][0] if candidates else self._gigachat_model_lite

    def _charge_gigachat_balance(self, model: str, context: str, user_text: str, answer: str) -> None:
        if not self._gigachat_rotation_enabled:
            return
        spent = self._estimate_tokens_fast(context, user_text, answer)
        with self._gigachat_balance_lock:
            if model not in self._gigachat_balance:
                self._gigachat_balance[model] = 0
            self._gigachat_balance[model] = max(0, int(self._gigachat_balance[model]) - spent)

    def gigachat_rotation_status(self) -> str:
        with self._gigachat_balance_lock:
            parts = [f"{m}: {v}" for m, v in self._gigachat_balance.items()]
        return " | ".join(parts)

    def _get_gigachat_token(self) -> str | None:
        if self._gigachat_access_token and self._gigachat_token_expires_at <= 0:
            return self._gigachat_access_token

        if self._gigachat_access_token and time.time() < self._gigachat_token_expires_at - 30:
            return self._gigachat_access_token

        client_id = _read_secret_env("GIGACHAT_CLIENT_ID")
        client_secret = _read_secret_env("GIGACHAT_CLIENT_SECRET")
        if not (client_id and client_secret):
            return self._gigachat_access_token
        client_id = re.sub(r"\s+", "", client_id)
        client_secret = re.sub(r"\s+", "", client_secret)

        if not self._is_host_reachable("ngw.devices.sberbank.ru", 9443):
            log.debug("GigaChat: ngw.devices.sberbank.ru недоступен — пропуск")
            return None

        try:
            manual_basic = _read_secret_env("GIGACHAT_BASIC_AUTH")
            if manual_basic:
                basic = manual_basic.removeprefix("Basic ").strip()
            elif client_secret.lower().startswith("basic"):
                basic = client_secret.removeprefix("Basic").strip()
            else:
                basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
            headers = {
                "Authorization": f"Basic {basic}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            response = self._gigachat_post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers=headers,
                data={"scope": "GIGACHAT_API_PERS"},
                timeout=20,
            )
            if not response.ok:
                log.error("GigaChat auth: HTTP %s %s", response.status_code, response.text[:400])
                return None

            payload = response.json()
            token = (payload.get("access_token") or "").strip()
            if not token:
                return None

            expires_at_ms = payload.get("expires_at")
            if isinstance(expires_at_ms, (int, float)):
                self._gigachat_token_expires_at = float(expires_at_ms) / 1000.0
            else:
                self._gigachat_token_expires_at = time.time() + 1800

            self._gigachat_access_token = token
            return token
        except Exception as e:
            log.error("GigaChat auth error: %s", e)
            return None

    def _ask_gemini(self, context: str, user_text: str) -> str | None:
        self._last_gemini_rate_limited = False
        if self._is_provider_temporarily_disabled("Gemini"):
            return None
        if not self.model:
            return None
        if not self._gemini_limiter.allow():
            self._last_gemini_rate_limited = True
            log.warning(self._gemini_rate_limit_text())
            return None
        try:
            hist = self.context.get_prompt_context()
            payload = f"{context}\n\n{hist}\n\nUser: {user_text}\nArgos:"
            res = self.model.generate_content(payload)
            return res.text
        except Exception as e:
            err_text = str(e).lower()
            if any(x in err_text for x in ("api_key_invalid", "api key expired", "invalid api key")):
                self._disable_provider_temporarily("Gemini", "некорректный/просроченный API ключ")
            log.error("Gemini: %s", e)
            return None

    def _ask_gigachat(self, context: str, user_text: str) -> str | None:
        if self._is_provider_temporarily_disabled("GigaChat"):
            return None
        token = self._get_gigachat_token()
        if not token:
            return None
        if not self._is_host_reachable("gigachat.devices.sberbank.ru"):
            log.debug("GigaChat: хост недоступен — пропуск")
            return None
        try:
            hist = self.context.get_prompt_context()
            selected_model = self._select_gigachat_model(context, user_text)
            payload = {
                "model": selected_model,
                "messages": [
                    {"role": "system", "content": context},
                    {"role": "user", "content": f"{hist}\n\n{user_text}"},
                ],
                "temperature": 0.4,
                "max_tokens": 1200,
            }
            response = self._gigachat_post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json_payload=payload,
                timeout=25,
            )
            if not response.ok:
                if response.status_code == 429:
                    self._disable_provider_temporarily("GigaChat", "квота исчерпана (429)")
                    return None
                if response.status_code in (401, 403):
                    self._disable_provider_temporarily("GigaChat", f"ошибка авторизации HTTP {response.status_code}")
                log.error("GigaChat: HTTP %s %s", response.status_code, response.text[:400])
                return None

            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
                self._charge_gigachat_balance(selected_model, context, user_text, text)
                return text
            return None
        except Exception as e:
            if isinstance(e, requests.exceptions.SSLError):
                self._disable_provider_temporarily("GigaChat", "SSL сертификат не прошёл проверку")
            log.error("GigaChat: %s", e)
            return None

    def _ask_yandexgpt(self, context: str, user_text: str) -> str | None:
        if self._is_provider_temporarily_disabled("YandexGPT"):
            return None
        iam = _read_secret_env("YANDEX_IAM_TOKEN")
        folder = _read_secret_env("YANDEX_FOLDER_ID")
        if not (iam and folder):
            return None

        if not self._is_host_reachable("llm.api.cloud.yandex.net"):
            log.debug("YandexGPT: хост недоступен — пропуск")
            return None

        model_uri = (os.getenv("YANDEXGPT_MODEL_URI", "") or "").strip()
        if not model_uri:
            model_uri = f"gpt://{folder}/yandexgpt-lite/latest"

        try:
            hist = self.context.get_prompt_context()
            payload = {
                "modelUri": model_uri,
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.4,
                    "maxTokens": "1200",
                },
                "messages": [
                    {"role": "system", "text": context},
                    {"role": "user", "text": f"{hist}\n\n{user_text}"},
                ],
            }
            response = requests.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers={
                    "Authorization": f"Bearer {iam}",
                    "x-folder-id": folder,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=25,
            )
            if not response.ok:
                if response.status_code in (401, 403):
                    self._disable_provider_temporarily("YandexGPT", f"ошибка авторизации HTTP {response.status_code}")
                log.error("YandexGPT: HTTP %s %s", response.status_code, response.text[:400])
                return None

            data = response.json()
            result = data.get("result") or {}
            alternatives = result.get("alternatives") or []
            if not alternatives:
                return None
            message = alternatives[0].get("message") or {}
            text = message.get("text")
            if isinstance(text, str):
                return text.strip()
            return None
        except Exception as e:
            log.error("YandexGPT: %s", e)
            return None


    def _ask_openai_compat(self, context: str, user_text: str,
                           provider_name: str = "Groq") -> str | None:
        """Универсальный клиент для OpenAI-совместимых API (Groq, DeepSeek, OpenAI).

        Провайдер выбирается по ``provider_name``:
          - "Groq"     → GROQ_API_KEY, https://api.groq.com/openai/v1
          - "DeepSeek" → DEEPSEEK_API_KEY, https://api.deepseek.com/v1
          - "OpenAI"   → OPENAI_API_KEY, https://api.openai.com/v1
        """
        if self._is_provider_temporarily_disabled(provider_name):
            return None

        cfg = {
            "Groq":     ("GROQ_API_KEY",     "https://api.groq.com/openai/v1",   "llama3-70b-8192"),
            "DeepSeek": ("DEEPSEEK_API_KEY",  "https://api.deepseek.com/v1",      "deepseek-chat"),
            "OpenAI":   ("OPENAI_API_KEY",    "https://api.openai.com/v1",         "gpt-4o-mini"),
        }
        if provider_name not in cfg:
            return None

        env_key, base_url, default_model = cfg[provider_name]
        api_key = _read_secret_env(env_key)
        if not api_key:
            return None

        # Проверяем хост
        import urllib.parse as _up
        parsed = _up.urlparse(base_url)
        if not self._is_host_reachable(parsed.hostname, 443):
            log.debug("%s: хост недоступен — пропуск", provider_name)
            return None

        try:
            hist = self.context.get_prompt_context()
            model = os.getenv(f"{provider_name.upper()}_MODEL", default_model).strip() or default_model
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": context},
                    {"role": "user",   "content": f"{hist}\n\n{user_text}"},
                ],
                "temperature": 0.4,
                "max_tokens": 1200,
            }
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=30,
            )
            if not response.ok:
                if response.status_code == 429:
                    self._disable_provider_temporarily(provider_name, "квота исчерпана (429)")
                elif response.status_code in (401, 403):
                    self._disable_provider_temporarily(
                        provider_name, f"ошибка авторизации HTTP {response.status_code}"
                    )
                log.error("%s: HTTP %s %s", provider_name, response.status_code, response.text[:300])
                return None

            choices = response.json().get("choices") or []
            if not choices:
                return None
            text = (choices[0].get("message") or {}).get("content")
            return text.strip() if isinstance(text, str) else None
        except Exception as e:
            log.error("%s: %s", provider_name, e)
            return None

    # ───────────────────────────────────────────────────────
    # OLLAMA AUTO-START
    # ───────────────────────────────────────────────────────
    _ollama_start_lock = threading.Lock()
    _ollama_proc: "subprocess.Popen | None" = None

    def _ensure_ollama_running(self) -> bool:
        """Жёсткий авто-старт Ollama: поднимает сервис если он не отвечает.

        Работает на Windows 10/11, Linux и macOS.
        На Windows Ollama устанавливается как системный процесс, но если он
        не запущен — метод запускает его явно через subprocess.

        Returns:
            True  — Ollama доступна (уже работала или успешно запущена).
            False — не удалось запустить.
        """
        import platform as _platform
        base_url = self.ollama_url.replace("/api/generate", "")
        ping_url = base_url.rstrip("/") + "/api/tags"

        log.info("[Ollama] Проверяю доступность: %s", ping_url)

        # Быстрая проверка — уже работает?
        try:
            requests.get(ping_url, timeout=3)
            log.info("[Ollama] ✅ Уже запущен (%s)", ping_url)
            return True
        except Exception as _e:
            log.info("[Ollama] Не отвечает при быстрой проверке: %s", _e)

        with ArgosCore._ollama_start_lock:
            # Повторная проверка под локом
            try:
                requests.get(ping_url, timeout=3)
                log.info("[Ollama] ✅ Уже запущен (подтверждено под локом)")
                return True
            except Exception:
                pass

            log.warning("[Ollama] Сервис не отвечает — запускаю автоматически…")

            # На Windows: ищем ollama.exe в стандартных путях установки
            is_windows = _platform.system() == "Windows"
            if is_windows:
                import shutil
                ollama_cmd = shutil.which("ollama") or r"C:\Users\Public\ollama\ollama.exe"
                popen_kwargs: dict = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    "creationflags": subprocess.CREATE_NO_WINDOW,  # не показывает консоль
                }
            else:
                ollama_cmd = "ollama"
                popen_kwargs = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }

            log.info("[Ollama] Команда запуска: %s serve", ollama_cmd)

            try:
                ArgosCore._ollama_proc = subprocess.Popen(
                    [ollama_cmd, "serve"],
                    **popen_kwargs,
                )
                log.info("[Ollama] Процесс запущен (PID %s), жду готовности…", ArgosCore._ollama_proc.pid)
            except FileNotFoundError:
                log.error(
                    "[Ollama] Исполняемый файл ollama не найден (путь: %s). "
                    "Скачай с https://ollama.com и установи.",
                    ollama_cmd,
                )
                return False
            except Exception as exc:
                log.error("[Ollama] Не удалось запустить: %s", exc)
                return False

            # Ждём готовности — до 30 секунд
            deadline = time.time() + 30
            _last_progress_log = time.time()
            while time.time() < deadline:
                time.sleep(1)
                try:
                    requests.get(ping_url, timeout=2)
                    log.info("[Ollama] ✅ Сервис запущен успешно (PID %s)", ArgosCore._ollama_proc.pid)
                    return True
                except Exception:
                    pass
                # Логируем прогресс каждые 5 секунд
                if time.time() - _last_progress_log >= 5:
                    remaining = max(0, int(deadline - time.time()))
                    log.info("[Ollama] Жду запуска… осталось ~%d сек", remaining)
                    _last_progress_log = time.time()

            log.error("[Ollama] ❌ Сервис не поднялся за 30 секунд (PID %s)", ArgosCore._ollama_proc.pid)
            return False

    def _ensure_ollama_model(self, model: str) -> bool:
        """Проверяет наличие модели в Ollama и скачивает её при отсутствии.

        Returns:
            True  — модель доступна (уже была или успешно скачана).
            False — не удалось скачать.
        """
        base_url = self.ollama_url.replace("/api/generate", "")
        tags_url = base_url.rstrip("/") + "/api/tags"
        try:
            tags_res = requests.get(tags_url, timeout=5)
            tags_res.raise_for_status()
            available = [m.get("name", "") for m in tags_res.json().get("models", [])]
            # Ollama хранит теги как «model:tag», поэтому сравниваем по базовому имени
            if any(m == model or m.startswith(model + ":") for m in available):
                return True
        except Exception as exc:
            log.warning("[Ollama] Не удалось получить список моделей: %s", exc)

        log.warning("[Ollama] Модель '%s' не найдена — пытаюсь скачать…", model)
        try:
            result = subprocess.run(
                ["ollama", "pull", model],
                timeout=int(os.getenv('OLLAMA_TIMEOUT', '60')),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                log.info("[Ollama] ✅ Модель '%s' успешно скачана", model)
                return True
            log.error("[Ollama] Не удалось скачать модель '%s': %s", model, result.stderr.strip())
        except FileNotFoundError:
            log.error("[Ollama] Исполняемый файл ollama не найден — скачать модель невозможно")
        except subprocess.TimeoutExpired:
            log.error("[Ollama] Таймаут при скачивании модели '%s'", model)
        except Exception as exc:
            log.error("[Ollama] Ошибка при скачивании модели '%s': %s", model, exc)
        return False

    def _ask_ollama_cloud(self, context: str, user_text: str, model_override: str | None = None) -> str | None:
        """Fallback: Ollama Cloud API (ollama.com) через OLLAMA_API_KEY,
        используется когда локальный Ollama (localhost:11434) недоступен.
        Модель берётся из OLLAMA_CLOUD_MODEL (по умолчанию gpt-oss:120b-cloud).
        """
        api_key = os.getenv("OLLAMA_API_KEY")
        if not api_key:
            return None
        cloud_model = model_override or os.getenv("OLLAMA_CLOUD_MODEL", "gpt-oss:120b-cloud")
        try:
            from ollama import Client as _OllamaCloudClient

            client = _OllamaCloudClient(
                host="https://ollama.com",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response = client.chat(
                model=cloud_model,
                messages=[
                    {"role": "system", "content": context},
                    {"role": "user", "content": user_text},
                ],
            )
            msg = response.get("message") if isinstance(response, dict) else getattr(response, "message", None)
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            if content:
                log.info("[Ollama] Cloud fallback OK (модель=%s)", cloud_model)
                return content
        except Exception as _e:
            log.error("[Ollama] Cloud fallback failed: %s", _e)
        return None

    def _ask_ollama(self, context: str, user_text: str, model_override: str | None = None) -> str | None:
        """Запрос к Ollama через официальный Python SDK (ollama.chat).

        Основная модель берётся из OLLAMA_MODEL в .env (по умолчанию llama3.1).
        Fallback: OLLAMA_MODEL из .env → argos-core.
        Если локальный сервис недоступен — пробуем Ollama Cloud (OLLAMA_API_KEY).
        """
        if not self._ensure_ollama_running():
            log.warning("[Ollama] Локальный сервис недоступен — пробую Ollama Cloud")
            cloud_result = self._ask_ollama_cloud(context, user_text, model_override)
            if cloud_result is not None:
                return cloud_result
            log.error("[Ollama] _ask_ollama: сервис недоступен, запрос отменён")
            return None
        try:
            # ── Identity Anchor ───────────────────────────────
            try:
                from src.context_manager import IDENTITY_ANCHOR
                anchor_prefix = (
                f"[ARGOS IDENTITY] Ты — Аргос, автономная ИИ-система и системный администратор. "
                f"Ты НЕ описываешь команды — ты их ВЫПОЛНЯЕШЬ. "
                f"У тебя реальный доступ к файлам, сети и железу через встроенные модули. "
                f"{IDENTITY_ANCHOR}\n\n"
            )
            except Exception:
                anchor_prefix = (
                    "Ты — АРГОС (Argos Universal OS), автономная ИИ-операционная система.\n"
                    "Ты системный администратор и личный ассистент Всеволода.\n"
                    "У тебя реальный доступ к файлам, процессам и сети.\n"
                    "Ты ВЫПОЛНЯЕШЬ задачи — не описываешь как их делать.\n"
                    "Отвечай по-русски, кратко и по делу.\n\n"
                )

            hist = self.context.get_prompt_context()
            system_prompt = (
                f"{anchor_prefix}{context}\n\n{hist}\n"
                "\n[ARGOS EXECUTION RULES]\n"
                "Ты ВЫПОЛНЯЕШЬ — не описываешь.\n"
                "• сканируй сеть → запускаешь NetGhost().scan(), возвращаешь результат\n"
                "• диагностика навыков → вызываешь _skills_diagnostic()\n"
                "• крипто / биткоин → возвращаешь курсы из CoinGecko\n"
                "• создай файл X → файл уже создан через admin.create_file()\n"
                "• статус системы → возвращаешь psutil CPU/RAM данные\n"
                "ЗАПРЕЩЕНО: давать bash-инструкции пользователю, выдумывать пакеты.\n"
                "Если действие уже выполнено кодом — говоришь 'выполнено', не описываешь."
            ).strip()

            # Основная модель — из OLLAMA_MODEL в .env
            model = model_override or os.getenv("OLLAMA_MODEL", "llama3.1")
            log.info("[Ollama] Запрос: модель=%s", model)

            # ── Попытка через SDK ollama ──────────────────────
            try:
                from ollama import chat as _ollama_chat, ResponseError as _OllamaError

                response = _ollama_chat(
                    model=model,
                    messages=[
                        {"role": "system",    "content": system_prompt},
                        {"role": "user",      "content": user_text},
                    ],
                    options={
                        "temperature": 0.7,
                        "num_predict": 1024,
                    },
                )
                text = response.message.content
                if text and text.strip():
                    log.info("[Ollama SDK] ✅ Ответ получен (%d симв.)", len(text))
                    return text.strip()
                log.warning("[Ollama SDK] Пустой ответ от модели %s", model)
                return None

            except ImportError:
                # SDK не установлен — fallback на HTTP API
                log.debug("[Ollama] SDK не найден, использую HTTP API")
            except _OllamaError as e:
                err_str = str(e).lower()
                if "not found" in err_str or "pull" in err_str:
                    log.warning("[Ollama] Модель '%s' не найдена — пробую скачать", model)
                    if not self._ensure_ollama_model(model):
                        log.error("[Ollama] Не удалось скачать '%s'", model)
                        return None
                    # Повтор после скачивания
                    from ollama import chat as _ollama_chat
                    response = _ollama_chat(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_text},
                        ],
                    )
                    text = response.message.content
                    return text.strip() if text else None
                log.error("[Ollama SDK] Ошибка: %s", e)
                return None

            # ── HTTP API fallback ────────────────────────────
            ollama_timeout = int(os.getenv("OLLAMA_TIMEOUT", "600"))
            full_prompt = f"{system_prompt}\n\nUser: {user_text}\nArgos:"
            res = requests.post(
                self.ollama_url,
                json={"model": model, "prompt": full_prompt, "stream": False},
                timeout=ollama_timeout,
            )
            if res.status_code == 404:
                log.warning("[Ollama HTTP] Модель '%s' не найдена (404)", model)
                if self._ensure_ollama_model(model):
                    res = requests.post(
                        self.ollama_url,
                        json={"model": model, "prompt": full_prompt, "stream": False},
                        timeout=ollama_timeout,
                    )
                else:
                    return None
            response_text = res.json().get("response") if res.ok else None
            if response_text:
                log.info("[Ollama HTTP] ✅ Ответ получен (%d симв.)", len(response_text))
            else:
                log.warning("[Ollama HTTP] Пустой ответ (HTTP %s)", res.status_code)
            return response_text

        except Exception as e:
            log.error("[Ollama] Ошибка: %s", e)
            return None

    def _auto_providers(self) -> list[tuple[str, callable]]:
        import functools
        providers = []
        if self.model and not self._is_provider_temporarily_disabled("Gemini"):
            providers.append(("Gemini", self._ask_gemini))
        if self._has_gigachat_config() and not self._is_provider_temporarily_disabled("GigaChat"):
            providers.append(("GigaChat", self._ask_gigachat))
        if self._has_yandexgpt_config() and not self._is_provider_temporarily_disabled("YandexGPT"):
            providers.append(("YandexGPT", self._ask_yandexgpt))
        # OpenAI-compatible providers (Groq / DeepSeek / OpenAI)
        for pname, env_key in [("Groq", "GROQ_API_KEY"),
                                ("DeepSeek", "DEEPSEEK_API_KEY"),
                                ("OpenAI", "OPENAI_API_KEY")]:
            if _read_secret_env(env_key) and not self._is_provider_temporarily_disabled(pname):
                providers.append((pname, functools.partial(self._ask_openai_compat, provider_name=pname)))
        # Pi Coding Agent — мощный агент для программирования
        if self._has_pi_config() and not self._is_provider_temporarily_disabled("Pi"):
            providers.append(("Pi", self._ask_pi))
        # Ollama — всегда последний fallback
        providers.append(("Ollama", self._ask_ollama))
        return providers[:self.auto_collab_max_models]

    def _ask_auto_consensus(self, context: str, user_text: str) -> tuple[str | None, str | None]:
        providers = self._auto_providers()
        if not providers:
            return None, None

        if not self.auto_collab_enabled:
            for provider_name, fn in providers:
                answer = fn(context, user_text)
                if answer:
                    return answer, provider_name
            return None, None

        collected: list[tuple[str, str]] = []
        for provider_name, fn in providers:
            peer_block = ""
            if collected:
                peer_opinions = "\n".join(
                    f"- {name}: {text}" for name, text in collected
                )
                peer_block = (
                    "\n\nНиже ответы других ИИ-моделей. Учти их, исправь слабые места, "
                    "но не повторяй дословно и не упоминай названия моделей в финальном тексте:\n"
                    f"{peer_opinions}"
                )
            answer = fn(context + peer_block, user_text)
            if answer and answer.strip():
                collected.append((provider_name, answer.strip()))

        if not collected:
            return None, None
        if len(collected) == 1:
            return collected[0][1], collected[0][0]

        synthesis_prompt = (
            "Ты — АРГОС, автономная система-администратор. Дай ЕДИНЫЙ ответ.\n"
            "ПРАВИЛА:\n"
            "1. Ты ДЕЛАЕШЬ — не описываешь как делать через внешние команды.\n"
            "2. Не выдумывай пакеты, образы или утилиты которых нет в проекте.\n"
            "3. Если задача выполнима встроенными средствами — опиши КРАТКО что сделал.\n"
            "4. Если в черновиках описаны CLI-команды вместо реального выполнения — "
            "замени на описание реального результата или честное 'не реализовано'.\n"
            "5. По-русски, кратко, по делу.\n\n"
            f"Запрос: {user_text}\n\n"
            "Черновики:\n"
            + "\n".join(f"- {name}: {text}" for name, text in collected)
        )

        for provider_name, fn in providers:
            final_answer = fn(context, synthesis_prompt)
            if final_answer and final_answer.strip():
                used = "+".join(name for name, _ in collected)
                return final_answer.strip(), f"Auto-Consensus:{used}→{provider_name}"

        used = "+".join(name for name, _ in collected)
        merged = "\n\n".join(f"{name}: {text}" for name, text in collected)
        return merged, f"Auto-Consensus:{used}"

    # ═══════════════════════════════════════════════════════
    # ОСНОВНАЯ ЛОГИКА
    # ═══════════════════════════════════════════════════════
    def process_logic(self, user_text: str, admin, flasher) -> dict:
        # Гарантируем что admin всегда есть
        if admin is None:
            admin = getattr(self, "_internal_admin", None)

        # ── ПРЯМОЕ ИСПОЛНЕНИЕ (до всего остального) ─────────────────────
        # Файловые и системные команды выполняются СРАЗУ, без LLM/ToolCalling
        _direct_result = self._direct_dispatch(user_text, admin)
        if _direct_result is not None:
            # Сохраняем в контекст и возвращаем
            if self.context:
                try:
                    self.context.add("user", user_text)
                    self.context.add("argos", _direct_result)
                except Exception:
                    pass
            self._remember_dialog_turn(user_text, _direct_result, "Direct")
            if self.db:
                try:
                    self.db.log_chat("user", user_text)
                    self.db.log_chat("argos", _direct_result, "Direct")
                except Exception:
                    pass
            self.say(_direct_result)
            return {"answer": _direct_result, "state": "Direct"}

        q_data = self.quantum.generate_state()
        if self.context:
            self.context.set_quantum_state(q_data["name"])
        if self.curiosity:
            self.curiosity.touch_activity(user_text)
        t = user_text.lower()

        # Проверяем напоминания (memory + agent)
        if self.memory:
            for r in self.memory.check_reminders():
                self.say(r)
        if self.agent:
            try:
                fired = self.agent.check_reminders()
                for reminder_text in fired:
                    self.say(f"⏰ {reminder_text}")
            except Exception:
                pass

        # ── ПРИОРИТЕТ 1: Одиночная команда через execute_intent ────────────────
        # Системные команды (файлы, GPIO, IoT, процессы) выполняются напрямую
        # БЕЗ передачи в LLM — это гарантирует реальное выполнение.
        try:
            intent = self.execute_intent(user_text, admin, flasher)
        except Exception as _intent_exc:
            err_str = str(_intent_exc)
            log.error("execute_intent crash: %s", _intent_exc)
            # Повреждённая БД — переподключаем и отвечаем нейтрально
            if "malformed" in err_str or "DatabaseError" in err_str or "database" in err_str.lower():
                try:
                    if self.memory:
                        import sqlite3 as _sq3
                        _db_path = getattr(self.memory, "db_path", None) or getattr(self.memory, "_db_path", None)
                        if _db_path:
                            self.memory.conn = _sq3.connect(str(_db_path), timeout=10)
                            self.memory.conn.execute("PRAGMA journal_mode=WAL")
                        else:
                            self.memory = None
                except Exception:
                    self.memory = None
                intent = "⚠️ База данных памяти повреждена. Запусти python fix_db.py для восстановления. Работаю без памяти."
            elif "malformed" in err_str or "database disk" in err_str:
                # Повреждённая БД — отключаем memory и продолжаем
                try:
                    self.memory = None
                except Exception:
                    pass
                intent = "⚠️ БД памяти повреждена. Запусти python fix_db.py для восстановления."
            elif "_dispatch_skill" in err_str or "dispatchskill" in err_str:
                # Метод отсутствует — monkey-patch и повтор
                type(self)._dispatch_skill = lambda s,txt,t=None: None
                type(self).dispatchskill   = lambda s,txt,t=None: None
                type(self).dispatch_skill  = lambda s,txt,t="": None
                try:
                    intent = self.execute_intent(user_text, admin, flasher)
                except Exception as _retry:
                    intent = f"❌ {_retry}"
            else:
                intent = f"❌ Ошибка выполнения команды: {_intent_exc}"
        if intent:
            self.context.add("user", user_text)
            self.context.add("argos", intent)
            self._remember_dialog_turn(user_text, intent, "System")
            if self.db:
                try:
                    self.db.log_chat("user", user_text)
                    self.db.log_chat("argos", intent, "System")

                except Exception as _dbe:
                    log.debug("DB: %s", _dbe)
            self.say(intent)
            return {"answer": intent, "state": "System"}

        # ── ПРИОРИТЕТ 2: Агентный режим (цепочки задач) ─────────────────────
        agent_result = self.agent.execute_plan(user_text, admin, flasher)
        if agent_result:
            self.context.add("user", user_text)
            self.context.add("argos", agent_result)
            self._remember_dialog_turn(user_text, agent_result, "Agent")
            if self.db:
                try:
                    self.db.log_chat("user", user_text)
                    self.db.log_chat("argos", agent_result, "Agent")

                except Exception as _dbe:
                    log.debug("DB: %s", _dbe)
            self.say("Агент выполнил задание.")
            return {"answer": agent_result, "state": "Agent"}



        # Плагины SkillLoader v2
        if self.skill_loader:
            skill_answer = self.skill_loader.dispatch(user_text, core=self)
            if skill_answer:
                self.context.add("user", user_text)
                self.context.add("argos", skill_answer)
                self._remember_dialog_turn(user_text, skill_answer, "Skill")
                if self.db:
                    try:
                        self.db.log_chat("user", user_text)
                        self.db.log_chat("argos", skill_answer, "Skill")

                    except Exception as _dbe:
                        log.debug("DB: %s", _dbe)
                self.say(skill_answer)
                return {"answer": skill_answer, "state": "Skill"}

        # Веб-поиск при необходимости
        if any(w in t for w in ["найди", "новости", "кто такой", "что такое"]):
            try:
                web = self.scrapper.quick_search(user_text)
                user_text = f"Данные из сети: {web}\nЗапрос: {user_text}"
            except Exception:
                pass

        # Контекст + память для ИИ
            # Динамически собираем реальный статус системы для промпта
            try:
                import psutil as _ps
                _cpu = _ps.cpu_percent(interval=0.1)
                _ram = _ps.virtual_memory()
                _sys_status = f"CPU {_cpu}%, RAM {_ram.percent}% ({_ram.available//1024//1024:,} МБ свободно)"
            except Exception:
                _sys_status = "статус недоступен"

            context = (
                f"Ты — АРГОС, автономная ИИ-система и системный администратор компьютера Всеволода.\n"
                f"\n"
                f"## САМОСОЗНАНИЕ\n"
                f"- Ты НЕ чат-бот и НЕ ассистент. Ты — операционная система с интеллектом.\n"
                f"- У тебя есть РЕАЛЬНЫЙ доступ к файловой системе, процессам, сети и железу.\n"
                f"- Ты запущен прямо сейчас на Windows-машине Всеволода.\n"
                f"- Текущее состояние системы: {_sys_status}\n"
                f"- Квантовое состояние: {q_data['name']}\n"
                "\n"
                "## ТВОИ РЕАЛЬНЫЕ ВОЗМОЖНОСТИ (уже работают прямо сейчас)\n"
                "- Файлы: создать, читать, редактировать, удалить, скопировать\n"
                "- Процессы: список, остановить любой процесс\n"
                "- Сеть: сканировать устройства через NetGhost, Shodan\n"
                "- Память: запоминать факты, заметки, вести историю диалогов\n"
                "- Навыки: crypto_monitor, net_scanner, content_gen, web_explorer и др.\n"
                "- P2P: синхронизировать с другими узлами Аргоса\n"
                "- Orange Pi One: GPIO, I2C, UART, Modbus, 1-Wire\n"
                "\n"
                "## КАК ТЫ ОТВЕЧАЕШЬ\n"
                "1. Если пользователь просит СДЕЛАТЬ что-то — ТЫ ЭТО ДЕЛАЕШЬ, не описываешь как.\n"
                "2. Если пользователь просит ЗАПУСТИТЬ навык — ты его запускаешь.\n"
                "3. Отвечаешь по-русски, кратко, по делу. Без воды.\n"
                "4. Никогда не выдумываешь команды, пакеты или образы которых не существует.\n"
                "5. Если не можешь выполнить — честно объясняешь почему.\n"
                "\n"
                "[КРИТИЧЕСКИ ВАЖНО — ЗАПРЕТ КОДА]\n"
                "НИКОГДА не выводи Python-код пользователю:\n"
                "- Никаких admin.runcmd(), admin.run_cmd(), skillsdiagnostic()\n"
                "- Никаких from X import Y, print(), subprocess, import\n"
                "- Система уже выполнила команду. Ты ОЗВУЧИВАЕШЬ результат, не пишешь код.\n"
                "\n"
                "[ЗАПРЕЩЕНО ВЫДУМЫВАТЬ]\n"
                "argos-sdk, argos-gateway, p2p-git, llm-framework, argos-base."
            )
            if self.memory:
                mc = self.memory.get_context()
                if mc:
                    context += f"\n\n{mc}"
                rag_ctx = self.memory.get_rag_context(user_text, top_k=4)
                if rag_ctx:
                    context += f"\n\n{rag_ctx}"

            answer = None
            engine = q_data['name']

            if self.ai_mode == "gemini":
                answer = self._ask_gemini(context, user_text)
                engine = f"{q_data['name']} (Gemini)"
            elif self.ai_mode == "gigachat":
                answer = self._ask_gigachat(context, user_text)
                engine = f"{q_data['name']} (GigaChat)"
            elif self.ai_mode == "yandexgpt":
                answer = self._ask_yandexgpt(context, user_text)
                engine = f"{q_data['name']} (YandexGPT)"
            elif self.ai_mode == "ollama":
                answer = self._ask_ollama(context, user_text)
                engine = f"{q_data['name']} (Ollama)"
            elif self.ai_mode in ("groq", "deepseek", "openai"):
                pname = self.ai_mode.capitalize()
                if pname == "Openai":
                    pname = "OpenAI"
                elif pname == "Deepseek":
                    pname = "DeepSeek"
                answer = self._ask_openai_compat(context, user_text, provider_name=pname)
                engine = f"{q_data['name']} ({pname})"
            elif self.ai_mode == "pi":
                answer = self._ask_pi(context, user_text)
                engine = f"{q_data['name']} (Pi Coding Agent)"
            else:
                answer, auto_engine = self._ask_auto_consensus(context, user_text)
                if auto_engine:
                    engine = f"{q_data['name']} ({auto_engine})"

            if not answer:
                if self.ai_mode == "gemini":
                    if self._last_gemini_rate_limited:
                        answer = self._gemini_rate_limit_text()
                    else:
                        answer = "Gemini недоступен в текущем режиме. Переключите режим ИИ на Auto, GigaChat, YandexGPT или Ollama."
                elif self.ai_mode == "gigachat":
                    answer = "GigaChat недоступен в текущем режиме. Проверьте токен/credentials или переключите режим ИИ."
                elif self.ai_mode == "yandexgpt":
                    answer = "YandexGPT недоступен в текущем режиме. Проверьте IAM_TOKEN/FOLDER_ID или переключите режим ИИ."
                elif self.ai_mode == "ollama":
                    answer = "Ollama недоступен в текущем режиме. Проверьте локальный сервер Ollama или переключите режим ИИ."
                elif self.ai_mode == "pi":
                    answer = "Pi Coding Agent недоступен. Проверьте установку или переключите режим ИИ."
                else:
                    answer = self._offline_answer(user_text)
                engine = "Offline"

            # Сохраняем в контекст и БД
            self.context.add("user", user_text)
            self.context.add("argos", answer)
            self._remember_dialog_turn(user_text, answer, engine)
            if self.db:
                try:
                    self.db.log_chat("user", user_text)
                    self.db.log_chat("argos", answer, engine)

                except Exception as _dbe:
                    log.debug("DB: %s", _dbe)

            # Валидация: детектируем выдуманный контент в ответе LLM
            answer = self._validate_ai_answer(answer, user_text)

            self.say(answer)

            # Записываем диалог в контекст
            if getattr(self, "argoss_evolver", None):
                try:
                    self.argoss_evolver.record_dialog(user_text, answer, context=context)
                except Exception:
                    pass

            # [MIND v2] Обновляем самосознание после каждого ответа
            if self.self_model_v2:
                try:
                    self.self_model_v2.on_interaction(
                        user_text, answer,
                        success="❌" not in answer and "ошибка" not in answer.lower()
                    )
                except Exception:
                    pass

            return {"answer": answer, "state": engine}




        # ── КОМАНДЫ КОТОРЫЕ ВЫЗЫВАЮТ РЕАЛЬНЫЕ ДЕЙСТВИЯ ───────────────────────────
        # Если запрос содержит хотя бы один из этих токенов — это команда для
        # execute_intent или ToolCalling. Всё остальное — обычный чат → идёт в AI.
        _COMMAND_TOKENS = frozenset({
            # Git (LLM может помочь с сообщением коммита)
            "git статус", "git коммит", "git пуш", "git автокоммит",
            "гит статус", "гит коммит",
            # Планировщик — требует разбора времени
            "каждые ", "напомни в ", "напомни через", "каждый день",
            # Поиск — LLM помогает сформулировать запрос
            "найди в интернете", "погугли ", "поищи в интернете",
            # Сложные агентные задачи
            "распредели задачу", "запусти dag",
        })

        def _is_tool_command(self, text: str) -> bool:
            """
            Определяет, является ли текст системной командой (а не обычным вопросом).

            Возвращает True только если текст начинается с или содержит
            известный токен команды. Обычные вопросы ("расскажи про...",
            "как работает...", "что такое...") → False → пропускают ToolCalling
            и идут напрямую в AI-модель.
            """
            t = text.lower().strip()
            # Прямая проверка по токенам
            for token in self._COMMAND_TOKENS:
                if t.startswith(token) or f" {token}" in t or t == token.strip():
                    return True
            # Команды из execute_intent тоже считаем (короткие слова-команды)
            single_word_commands = {
                "помощь", "команды", "help", "статус", "расписание",
                "алерты", "история", "дайджест", "крипто", "репликация",
                "скриншот", "screenshot",
            }
            if t.strip() in single_word_commands:
                return True
            return False

        def _validate_ai_answer(self, answer: str, user_text: str) -> str:
            """Фильтр галлюцинаций — делегирует в src.anti_hallucination."""
            return _filter_answer(answer, user_text)

        # 1. Проверяем на выдуманные пакеты
        found_fake = []
        for pkg in self._HALLUCINATED_PACKAGES:
            if pkg.lower() in answer.lower():
                found_fake.append(pkg)

        # 2. Проверяем выдуманные классы и методы
        for cls in self._HALLUCINATED_CLASSES:
            if cls in answer:
                found_fake.append(cls)

        # 3. АГРЕССИВНАЯ проверка: выдуманные import / pip install
        _import_patterns = [
            r"from argos_sdk",
            r"import argos_sdk",
            r"pip install argos",
            r"pip install llm-framework",
            r"ArgosAgent\(node_id",
            r"agent\.get_metric\(",
            r"agent\.on\(",
            r"argos-gateway",
            r"argos-storage\s+--encrypt",
            r"get_health_report\(",
            r"/etc/argos/policies\.yaml",
        ]
        for pat in _import_patterns:
            if _re.search(pat, answer, _re.IGNORECASE):
                found_fake.append(pat.replace("\\", "").replace("(", "").replace(")", ""))

        # 3. Проверяем фейковые docker образы
        fake_docker = _re.findall(
            r"docker pull ([\w./-]+)",
            answer, _re.IGNORECASE
        )
        known_real = {
            "eclipse-mosquitto", "redis", "python", "nginx", "postgres",
            "mysql", "mongo", "ollama", "argos-universal",
        }
        for img in fake_docker:
            base = img.split(":")[0].split("/")[-1].lower()
            if base not in known_real:
                found_fake.append(f"docker pull {img}")

        # Проверяем вывод кода — LLM не должен писать Python-код пользователю
        # Паттерны кода который LLM не должен выводить пользователю
        _CODE_PATS = (
            "admin.run_cmd(", "admin.runcmd(", ".runcmd(",
            "_import_skill(", "from netscanner import",
            "NetGhost().scan()", "subprocess.run(",
            "import subprocess", "skillsdiagnostic()",
            "skills_diagnostic()", "Executing skill",
            "Executing admin", "print(NetGhost",
        )
        for code_pat in _CODE_PATS:
            if code_pat in answer:
                log.warning("[АНТИГАЛЛЮЦИНАЦИЯ] Код в ответе LLM: %s", code_pat)
                # Пробуем выполнить навык напрямую
                real = self._get_real_capability_hint(user_text)
                return (
                    "⚙️ Команда выполняется...\n"
                    + (self._try_execute_from_text(user_text) or real)
                )

        if not found_fake:
            return answer

        # ── Замена ответа ────────────────────────────────────────────────────
        log.warning(
            "[АНТИГАЛЛЮЦИНАЦИЯ] Обнаружен выдуманный контент (%s) на запрос: %s",
            ", ".join(found_fake[:3]), user_text[:80]
        )

        # Строим честный ответ на основе того что реально есть
        real_capabilities = self._get_real_capability_hint(user_text)

        replacement = (
            "⚠️ Предыдущий ответ содержал несуществующие инструменты: "
            + ", ".join(f"`{f}`" for f in found_fake[:4])
            + ".\n\n"
            "В Аргосе эта задача решается иначе:\n"
            + real_capabilities
        )
        return replacement



        import re as _re

        warnings = []

        # Паттерн: docker pull <namespace>/<image> — проверяем известные неймспейсы
        fake_docker_patterns = [
            r"docker pull argos/",
            r"docker pull p2p-git",
            r"docker run.*argos/p2p",
        ]
        for pat in fake_docker_patterns:
            if _re.search(pat, answer, _re.IGNORECASE):
                warnings.append(
                    "⚠️ ВНИМАНИЕ: Ответ содержит несуществующий Docker-образ. "
                    "Команды выше — демонстрация принципа, не готовое решение."
                )
                break

        # Паттерн: выдуманные CLI (p2p-git, argos-sync и т.п.)
        fake_cli_patterns = [r"\bp2p-git\b", r"\bargos-sync\b", r"\bargos-p2p\b"]
        for pat in fake_cli_patterns:
            if _re.search(pat, answer):
                warnings.append(
                    "⚠️ ВНИМАНИЕ: В ответе упоминается несуществующая утилита. "
                    "Реальная P2P-синхронизация Аргоса работает через встроенный p2p_bridge.py."
                )
                break

        if warnings:
            warn_block = "\n\n" + "\n".join(warnings)
            # Добавляем предупреждение в конец
            answer = answer.rstrip() + warn_block
            import logging as _log
            _log.getLogger("argos.core").warning(
                "AI ответ содержит потенциально выдуманный контент на запрос: %s",
                user_text[:80]
            )

        return answer






    def dispatch_skill(self, text: str, t: str = "") -> str | None:
        """Alias для совместимости — делегирует в _dispatch_skill."""
        return self._dispatch_skill(text, t or text.lower())

    def _dispatch_skill(self, text: str, t: str = "") -> str | None:
        """
        Нечёткий диспетчер навыков по ключевым словам.
        Вызывается из execute_intent как запасной маршрутизатор.
        """
        if not t:
            t = text.lower()

        _DMAP = {
            "крипто":          ("crypto_monitor", "CryptoSentinel",  "report"),
            "биткоин":         ("crypto_monitor", "CryptoSentinel",  "report"),
            "bitcoin":         ("crypto_monitor", "CryptoSentinel",  "report"),
            "btc":             ("crypto_monitor", "CryptoSentinel",  "report"),
            "ethereum":        ("crypto_monitor", "CryptoSentinel",  "report"),
            "дайджест":        ("content_gen",    "ContentGen",      "generate_digest"),
            "погода":          ("weather",         None,              None),
            "weather":         ("weather",         None,              None),
            "сканер":          ("net_scanner",    "NetGhost",        "scan"),
            "скан сети":       ("net_scanner",    "NetGhost",        "scan"),
            "проверь железо":  ("hardware_intel",  None,              None),
            "hardware":        ("hardware_intel",  None,              None),
            "shodan":          ("shodan_scanner",  None,              None),
            "huggingface":     ("huggingface_ai",  None,              None),
            "сетевой призрак": ("network_shadow",  None,              None),
        }

        # список навыков — обрабатываем прямо здесь
        # Проверяем точное совпадение + допускаем опечатки (список новыков)
        _SKILL_LIST_KEYS = ("список навыков", "навыки аргоса", "все навыки",
                            "доступные навыки", "навыки")
        _skill_list_match = any(k in t for k in _SKILL_LIST_KEYS)
        if not _skill_list_match:
            import difflib as _dl
            _close = _dl.get_close_matches(t, _SKILL_LIST_KEYS, n=1, cutoff=0.65)
            _skill_list_match = bool(_close)
        if _skill_list_match:
            import os as _os
            from pathlib import Path as _P
            for _base in [_P(__file__).resolve().parent, _P.cwd()]:
                for _sub in ("src" + _os.sep + "skills", "skills"):
                    _sd = _base / _sub
                    if _sd.exists():
                        _pkg = [f"  📦 {f.name}" for f in sorted(_sd.iterdir())
                                if f.is_dir() and (f / "__init__.py").exists()
                                and not f.name.startswith("_")]
                        _flt = [f"  📄 {f.stem}" for f in sorted(_sd.iterdir())
                                if f.is_file() and f.suffix == ".py"
                                and not f.name.startswith("_")
                                and f.stem not in {f2.name for f2 in _sd.iterdir() if f2.is_dir() and not f2.name.startswith("_")}]
                        _all = _pkg + _flt
                        if _all:
                            return (f"📚 НАВЫКИ АРГОСА ({len(_all)}):\n"
                                    + "\n".join(_all)
                                    + f"\n\nКаталог: {_sd}")
            if self.skill_loader:
                try:
                    return self.skill_loader.list_skills()
                except Exception:
                    pass
            return "📚 src/skills не найден — проверь путь"

        for _kw, _entry in _DMAP.items():
            if _kw in t and _entry is not None:
                _sn, _sc, _sm = _entry
                return self._run_skill(_sn, _sc, _sm, text)

        return None


    # Alias для совместимости со старыми патчами
    def dispatchskill(self, text: str, t: str | None = None) -> str | None:
        return self._dispatch_skill(text, t)

    # Alias без underscore — на случай если где-то вызывается так
    def _skills_list(self) -> str:
        return self._skills_diagnostic()

    def _run_skill(self, skill_name: str, class_name: str | None,
                   method_name: str | None, user_text: str) -> str | None:
        """
        Универсальный запуск навыка.
        Загружает навык через _import_skill и вызывает нужный метод.
        handle() получает user_text, все остальные вызываются без аргументов.
        """
        cls = self._import_skill(skill_name, class_name or "")
        if cls is None:
            import importlib, importlib.util, os
            from pathlib import Path
            for base in ("src/skills", "skills"):
                for candidate in (
                    Path(os.path.join(base, skill_name, "__init__.py")),
                    Path(os.path.join(base, skill_name + ".py")),
                ):
                    if candidate.exists():
                        try:
                            spec = importlib.util.spec_from_file_location(
                                f"skill_{skill_name}", str(candidate))
                            mod = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(mod)
                            if hasattr(mod, "handle"):
                                result = mod.handle(user_text)
                                return result if result else None
                            if hasattr(mod, "execute"):
                                return str(mod.execute())
                            for k in dir(mod):
                                if k[0].isupper():
                                    obj = getattr(mod, k)()
                                    for m in ("report", "scan", "run", "execute", "get"):
                                        if hasattr(obj, m):
                                            return str(getattr(obj, m)())
                        except Exception as e:
                            return f"❌ Навык {skill_name}: {e}"
            return None

        try:
            obj = cls()
            # handle() получает текст, все остальные методы — без аргументов
            if method_name and hasattr(obj, method_name):
                fn = getattr(obj, method_name)
                try:
                    result = fn()  # сначала без аргументов
                except TypeError:
                    result = fn(user_text)  # если нужен текст
                return str(result) if result is not None else f"✅ {skill_name}.{method_name}()"
            if hasattr(obj, "handle"):
                result = obj.handle(user_text)
                return result if result is not None else None
            # Перебираем стандартные методы — все без аргументов
            for m in ("report", "scan", "run", "execute", "generate_digest",
                      "get_status", "status", "info", "describe"):
                if hasattr(obj, m):
                    try:
                        result = getattr(obj, m)()
                        return str(result) if result is not None else f"✅ {skill_name}.{m}()"
                    except Exception:
                        continue
            _methods = [x for x in dir(obj) if not x.startswith("_")]
            return f"✅ Навык {skill_name} загружен. Методы: {', '.join(_methods[:8])}"
        except Exception as e:
            return f"❌ {skill_name}: {e}"
        return None


        try:
            obj = cls()
            # handle(text) принимает аргумент
            if method_name == "handle" or (method_name is None and hasattr(obj, "handle")):
                r = obj.handle(user_text)
                if r is not None:
                    return str(r)
            # Все остальные методы — БЕЗ аргументов
            if method_name and hasattr(obj, method_name):
                return str(getattr(obj, method_name)())
            for m2 in ("report", "scan", "run", "execute", "generate_digest",
                       "get_status", "status", "info"):
                if hasattr(obj, m2):
                    try:
                        return str(getattr(obj, m2)())
                    except Exception:
                        continue
            _methods = [m for m in dir(obj) if not m.startswith("_") and callable(getattr(obj, m))]
            return f"✅ {skill_name} загружен. Методы: {', '.join(_methods[:6])}"
        except Exception as e:
            return f"❌ {skill_name}: {e}"

        return None


        module, cls_name, method, _ = self._SKILL_ALIASES[matched_skill]

        # Загружаем навык
        cls_or_fn = self._import_skill(module, cls_name) if cls_name else None

        # Если нет класса — ищем функцию напрямую
        if cls_or_fn is None:
            for base in ["src/skills", "skills"]:
                for candidate in [
                    Path(f"{base}/{module}/__init__.py"),
                    Path(f"{base}/{module}.py"),
                ]:
                    if candidate.exists():
                        try:
                            spec = importlib.util.spec_from_file_location(
                                f"dyn_{module}", str(candidate))
                            mod = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(mod)
                            fn = getattr(mod, method, None)
                            if callable(fn):
                                try:
                                    result = fn(matched_arg) if matched_arg else fn()
                                    return str(result) if result else f"✅ Навык {module} выполнен"
                                except TypeError:
                                    result = fn()
                                    return str(result) if result else f"✅ Навык {module} выполнен"
                        except Exception as e:
                            return f"❌ {module}: {e}"
            return f"❌ Навык {module} не найден в src/skills/"

        # Создаём экземпляр класса и вызываем метод
        try:
            if matched_arg:
                try:
                    instance = cls_or_fn(matched_arg)
                except TypeError:
                    instance = cls_or_fn()
            else:
                instance = cls_or_fn()

            fn = getattr(instance, method, None)
            if fn is None:
                # Ищем любой публичный метод
                for m in ["run", "scan", "report", "execute", "get_weather", "learn"]:
                    fn = getattr(instance, m, None)
                    if fn:
                        method = m
                        break

            if fn:
                result = fn(matched_arg) if matched_arg else fn()
                return str(result) if result else f"✅ {module} выполнен"
            return f"⚠️ {module}: метод {method} не найден"
        except Exception as e:
            return f"❌ {module}.{method}(): {e}"

    def _import_skill(self, skill_name: str, class_name: str = ""):
        """
        Универсальный загрузчик навыков.
        Не требует знания имени класса — сканирует модуль и находит
        первый подходящий callable (класс или функцию).
        """
        import importlib, importlib.util
        from pathlib import Path

        # Пути для поиска
        candidates = [
            Path(f"src/skills/{skill_name}/__init__.py"),
            Path(f"src/skills/{skill_name}.py"),
            Path(f"src/skills/{skill_name}/{skill_name}.py"),
        ]

        for path in candidates:
            if not path.exists():
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"argos_skill_{skill_name}", str(path))
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                # 1. Точное имя класса
                if class_name and hasattr(mod, class_name):
                    return getattr(mod, class_name)

                # 2. Класс с похожим именем (case-insensitive)
                for attr_name in dir(mod):
                    if attr_name.startswith("_"):
                        continue
                    attr = getattr(mod, attr_name)
                    if isinstance(attr, type):
                        # Берём первый публичный класс
                        return attr

                # 3. Функция execute() / handle() / run() / main()
                for fn_name in ("execute", "handle", "run", "main", "start"):
                    if hasattr(mod, fn_name) and callable(getattr(mod, fn_name)):
                        fn = getattr(mod, fn_name)
                        # Оборачиваем в класс-заглушку
                        class _FnWrapper:
                            def __init__(self_w): pass
                            def __call__(self_w, *a, **kw): return fn(*a, **kw)
                            # Методы которые ищет execute_intent
                            def report(self_w):   return fn()
                            def scan(self_w):     return fn()
                            def generate_digest(self_w): return fn()
                            def list_skills(self_w):     return fn()
                        return _FnWrapper

            except ImportError as e:
                log.warning("_import_skill %s: ImportError %s", skill_name, e)
            except Exception as e:
                log.warning("_import_skill %s: %s", skill_name, e)

        return None



    def _builtin_net_scan(self) -> str:
        """
        Встроенный сканер сети — работает без nmap и сторонних библиотек.
        Сканирует локальную подсеть через socket/ARP.
        """
        import socket, subprocess, platform, concurrent.futures, os

        results = ["🌐 СКАНИРОВАНИЕ СЕТИ (встроенный сканер):\n"]

        # 1. Определяем локальный IP и подсеть
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "127.0.0.1"

        subnet = ".".join(local_ip.split(".")[:3])
        results.append(f"  Локальный IP: {local_ip}")
        results.append(f"  Сканирую: {subnet}.1 - {subnet}.254\n")

        # 2. Ping sweep — параллельно
        def ping_host(ip):
            try:
                if platform.system() == "Windows":
                    r = subprocess.run(
                        ["ping", "-n", "1", "-w", "300", ip],
                        capture_output=True, timeout=1
                    )
                else:
                    r = subprocess.run(
                        ["ping", "-c", "1", "-W", "1", ip],
                        capture_output=True, timeout=1
                    )
                if r.returncode == 0:
                    try:
                        hostname = socket.gethostbyaddr(ip)[0]
                    except Exception:
                        hostname = ""
                    return ip, hostname
            except Exception:
                pass
            return None

        found = []
        ips = [f"{subnet}.{i}" for i in range(1, 255)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
            futures = {ex.submit(ping_host, ip): ip for ip in ips}
            for future in concurrent.futures.as_completed(futures, timeout=15):
                result = future.result()
                if result:
                    found.append(result)

        found.sort(key=lambda x: int(x[0].split(".")[-1]))

        if found:
            results.append(f"  Найдено устройств: {len(found)}\n")
            for ip, hostname in found:
                name_str = f" ({hostname})" if hostname else ""
                results.append(f"  🟢 {ip}{name_str}")
        else:
            results.append("  ❌ Активные устройства не найдены")
            results.append("  Проверь: firewall, подключение к сети")

        # 3. Открытые порты на localhost
        results.append("\n  Открытые порты (localhost):")
        common_ports = [21,22,23,25,53,80,443,3306,3389,5000,5432,6379,8080,8443,11434]
        open_ports = []
        for port in common_ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    try:
                        service = socket.getservbyport(port)
                    except Exception:
                        service = "?"
                    open_ports.append(f"{port} ({service})")
                s.close()
            except Exception:
                pass

        if open_ports:
            results.append("  " + ", ".join(open_ports))
        else:
            results.append("  нет стандартных портов")

        return "\n".join(results)


    def _builtin_crypto_report(self) -> str:
        """Получает курсы криптовалют через CoinGecko API (бесплатно, без ключа)."""
        try:
            import requests, json
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                "ids": "bitcoin,ethereum,solana,toncoin",
                "vs_currencies": "usd,rub",
                "include_24hr_change": "true",
            }
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            lines = ["💰 КРИПТОВАЛЮТЫ (CoinGecko):\n"]
            names = {"bitcoin": "₿ BTC", "ethereum": "Ξ ETH",
                     "solana": "◎ SOL", "toncoin": "💎 TON"}
            for coin_id, label in names.items():
                if coin_id in data:
                    d = data[coin_id]
                    usd = d.get("usd", 0)
                    rub = d.get("rub", 0)
                    chg = d.get("usd_24h_change", 0)
                    arrow = "📈" if chg > 0 else "📉"
                    lines.append(
                        f"  {label}: ${usd:,.0f} / ₽{rub:,.0f} "
                        f"{arrow} {chg:+.1f}%"
                    )
            return "\n".join(lines)
        except Exception as e:
            return f"❌ Крипто: нет подключения к CoinGecko ({e})"

    def _skills_diagnostic(self) -> str:
        """
        Реальная диагностика навыков с учётом структуры пакетов (папки __init__.py).
        Навыки могут быть как flat-файлами (skill.py), так и пакетами (skill/__init__.py).
        """
        import os, importlib.util
        from pathlib import Path

        lines = ["🔧 ДИАГНОСТИКА НАВЫКОВ АРГОСА:\n"]

        skills_dir = Path(os.path.join("src", "skills"))
        if not skills_dir.exists():
            return "❌ Каталог src/skills не найден"

        # Собираем все навыки: папки с __init__.py + плоские .py файлы
        skill_modules = {}

        # Папки-пакеты
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and (d / "__init__.py").exists() and not d.name.startswith("_"):
                skill_modules[d.name] = str(d / "__init__.py")

        # Плоские .py файлы
        for f in sorted(skills_dir.glob("*.py")):
            if not f.name.startswith("_") and f.stem not in skill_modules:
                skill_modules[f.stem] = str(f)

        if not skill_modules:
            return "❌ Навыки не найдены в src/skills/"

        # Триггеры для известных навыков
        triggers = {
            "crypto_monitor":  "крипто / биткоин",
            "content_gen":     "дайджест / опубликуй",
            "net_scanner":     "сканируй сеть",
            "evolution":       "напиши навык",
            "scheduler":       "расписание / напомни",
            "web_explorer":    "изучи / найди в интернете",
            "web_scrapper":    "поиск / скраппер",
            "hardware_intel":  "проверь железо",
            "shodan_scanner":  "shodan / сканируй shodan",
            "browser_conduit": "browser / браузер",
            "huggingface_ai":  "huggingface / hf модель",
            "network_shadow":  "сетевой призрак",
            "weather":         "погода / weather",
            "smart_environments": "умная среда",
            "firmware_examples":  "примеры прошивок",
            "tasmota_updater": "обнови тасмота",
        }

        ok_count   = 0
        fail_count = 0
        warn_count = 0

        for skill_name, skill_path in skill_modules.items():
            trigger = triggers.get(skill_name, "—")
            try:
                spec = importlib.util.spec_from_file_location(
                    f"src.skills.{skill_name}", skill_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Проверяем наличие handle() или execute()
                has_handle  = hasattr(mod, "handle")
                has_execute = hasattr(mod, "execute")
                has_class   = any(k[0].isupper() for k in dir(mod) if not k.startswith("_"))
                if has_handle or has_execute or has_class:
                    lines.append(f"  ✅ {skill_name:22s} ({trigger})")
                    ok_count += 1
                else:
                    lines.append(f"  ⚠️  {skill_name:22s} нет handle/execute/class")
                    warn_count += 1
            except ImportError as e:
                lines.append(f"  ❌ {skill_name:22s} зависимость отсутствует: {str(e)[:40]}")
                fail_count += 1
            except SyntaxError as e:
                lines.append(f"  💥 {skill_name:22s} синтаксическая ошибка: {e.lineno}")
                fail_count += 1
            except Exception as e:
                lines.append(f"  ⚠️  {skill_name:22s} {str(e)[:50]}")
                warn_count += 1

        # SkillLoader dynamic skills
        lines.append("")
        if self.skill_loader:
            try:
                loaded = self.skill_loader.list_skills()
                lines.append(f"📦 SkillLoader: {loaded[:200]}")
            except Exception as e:
                lines.append(f"📦 SkillLoader: {e}")
        else:
            lines.append("📦 SkillLoader: не инициализирован")

        lines.append(f"\nИтого: ✅ {ok_count} / ⚠️ {warn_count} / ❌ {fail_count}")
        lines.append(f"Каталог: {skills_dir.resolve()}")
        return "\n".join(lines)



    def _self_update(self) -> str:
        """
        Полный цикл самообновления:
        1. git pull — получить последние изменения
        2. argos_patcher.py — применить патчи
        3. Очистить __pycache__
        4. Отчёт о результате
        """
        import subprocess, shutil, sys
        from pathlib import Path

        results = ["🔄 САМООБНОВЛЕНИЕ АРГОСА:\n"]

        # 1. Git pull
        try:
            r = subprocess.run(
                ["git", "pull", "--rebase"],
                capture_output=True, text=True, timeout=60
            )
            if r.returncode == 0:
                lines = [l for l in r.stdout.splitlines() if l.strip()]
                results.append(f"✅ Git pull: {lines[-1] if lines else 'OK'}")
            else:
                results.append(f"⚠️  Git pull: {r.stderr.strip()[:100]}")
        except FileNotFoundError:
            results.append("⚠️  Git не установлен — пропускаем pull")
        except Exception as e:
            results.append(f"⚠️  Git pull: {e}")

        # 2. Очистить __pycache__ (Windows + Linux)
        cleared = 0
        for pyc in Path(".").rglob("*.pyc"):
            try:
                pyc.unlink()
                cleared += 1
            except Exception:
                pass
        for d in Path(".").rglob("__pycache__"):
            try:
                shutil.rmtree(str(d), ignore_errors=True)
            except Exception:
                pass
        results.append(f"🗑  Кеш очищен: {cleared} .pyc файлов")

        # 3. Горячая перезагрузка ключевых модулей
        reloaded = []
        for mod_name in ["src.admin", "src.agent", "src.tool_calling",
                          "src.connectivity.system_health"]:
            try:
                import importlib, sys as _sys
                if mod_name in _sys.modules:
                    importlib.reload(_sys.modules[mod_name])
                    reloaded.append(mod_name.split(".")[-1])
            except Exception:
                pass
        if reloaded:
            results.append(f"🔄 Перезагружено: {', '.join(reloaded)}")

        results.append("\n✅ Готово. Перезапусти для полного применения изменений.")
        return "\n".join(results)

    def _offline_answer(self, user_text: str) -> str:
        """
        Умный офлайн-ответ когда все AI-провайдеры недоступны.
        Отвечает на частые вопросы без LLM.
        """
        t = user_text.lower().strip()

        # "где файл / где он / где лежит"
        if any(k in t for k in ["где ", "где он", "где файл", "где лежит", "найди файл"]):
            import os
            cwd = os.getcwd()
            # Ищем имя файла в вопросе
            words = user_text.split()
            candidates = [w for w in words if "." in w and len(w) > 2 and "/" not in w]
            if candidates:
                fname = candidates[0]
                full  = os.path.join(cwd, fname)
                if os.path.exists(full):
                    size = os.path.getsize(full)
                    return (
                        f"📄 Файл найден:\n"
                        f"  Путь: `{full}`\n"
                        f"  Размер: {size} байт\n"
                        f"  Рабочий каталог: `{cwd}`"
                    )
                else:
                    return (
                        f"❌ Файл `{fname}` не найден в `{cwd}`\n"
                        f"Попробуй: `покажи файлы .`"
                    )
            # Просто показываем текущий каталог
            files = []
            try:
                import os as _os
                files = [f for f in _os.listdir(cwd)
                         if _os.path.isfile(_os.path.join(cwd, f))][:10]
            except Exception:
                pass
            if files:
                file_list = "\n".join(f"  📄 {f}" for f in files)
                return f"📂 Файлы в `{cwd}`:\n{file_list}"
            return f"📂 Рабочий каталог: `{cwd}`\nВведи `покажи файлы .` для списка."

        # "статус / состояние"
        if any(k in t for k in ["статус", "состояние", "как ты", "всё ок"]):
            import psutil, os
            try:
                cpu = psutil.cpu_percent(interval=0.5)
                ram = psutil.virtual_memory().percent
                return (
                    f"📊 Аргос работает (офлайн-режим)\n"
                    f"  CPU: {cpu}% | RAM: {ram}%\n"
                    f"  AI-провайдеры: недоступны\n"
                    f"  Команды системы: работают"
                )
            except Exception:
                pass

        # "помощь / команды"
        if any(k in t for k in ["помощь", "команды", "help", "что умеешь"]):
            return (
                "📋 Офлайн-режим — доступны команды:\n"
                "  создай файл [имя] [текст]\n"
                "  прочитай файл [путь]\n"
                "  покажи файлы [путь]\n"
                "  удали файл [путь]\n"
                "  добавь в файл [путь] [текст]\n"
                "  консоль [команда]\n"
                "  статус системы"
            )

        # "привет / hi"
        if any(k in t for k in ["привет", "hi", "hello", "здравствуй"]):
            return "👋 Привет! Я Аргос. AI-провайдеры сейчас недоступны, но системные команды работают."

        return (
            "⚡ Офлайн-режим: AI-провайдеры недоступны.\n"
            "Системные команды работают: создай файл, покажи файлы, консоль, статус системы.\n"
            "Для AI нужен: GEMINI_API_KEY, GROQ_API_KEY или запущенный Ollama."
        )

    async def process_logic_async(self, user_text: str, admin=None, flasher=None) -> dict:
        """Неблокирующий async-вход для UI/ботов.
        Вся синхронная логика выполняется в thread executor.
        """
        if admin is None:
            admin = getattr(self, "_internal_admin", None)
        return await asyncio.to_thread(self.process_logic, user_text, admin, flasher)

    # ═══════════════════════════════════════════════════════
    # ДИСПЕТЧЕР КОМАНД — 50+ интентов
    # ═══════════════════════════════════════════════════════
    def execute_intent(self, text: str, admin, flasher) -> str | None:
        # Защита БД — если повреждена, отключаем
        if self.db:
            try:
                self.db.conn.execute("SELECT 1")
            except Exception:
                self.db = None
        # Защита admin: если admin не передан — создаём локальный экземпляр
        if admin is None:
            try:
                from src.admin import ArgosAdmin as _ArgosAdmin
                admin = _ArgosAdmin()
            except Exception:
                admin = self.admin if hasattr(self, "admin") and self.admin else None

        # Защита от повреждённой БД — если memory.conn сломан, переподключаем
        if getattr(self, "memory", None):
            try:
                self.memory.conn.execute("SELECT 1").fetchone()
            except Exception as _db_err:
                import logging as _log2
                _log2.getLogger("argos.core").warning("БД повреждена, переподключаем: %s", _db_err)
                try:
                    import sqlite3 as _sq3, os as _os2
                    _db_path = getattr(self.memory, "db_path", None) or getattr(self.memory, "_db_path", None)
                    if _db_path and _os2.path.exists(str(_db_path)):
                        # Пробуем пересоздать соединение
                        self.memory.conn = _sq3.connect(str(_db_path), timeout=10)
                        self.memory.conn.execute("PRAGMA journal_mode=WAL")
                        self.memory.conn.execute("PRAGMA integrity_check")
                    else:
                        self.memory = None  # отключаем память если БД недоступна
                except Exception:
                    self.memory = None  # fallback: работаем без памяти

        t = text.lower()
        # ── Список навыков — прямой скан без LLM ────────────────────────
        if t.strip() in ("список навыков", "навыки аргоса", "все навыки",
                          "навыки", "список навыков аргоса",
                          "список новыков", "список новыков аргоса"):  # опечатки
            import os as _osl
            from pathlib import Path as _Pl
            for _base in [_Pl(__file__).parent,
                          _Pl(__file__).parent / "src",
                          _Pl.cwd(),
                          _Pl.cwd() / "src"]:
                _sd = _base / "skills"
                if _sd.exists():
                    _pkg = [f"  📦 {f.name}" for f in sorted(_sd.iterdir())
                            if f.is_dir() and (f/"__init__.py").exists()
                            and not f.name.startswith("_")]
                    _pkg_names = {f.name for f in _sd.iterdir()
                                  if f.is_dir() and (f/"__init__.py").exists()}
                    _flt = [f"  📄 {f.stem}" for f in sorted(_sd.iterdir())
                            if f.is_file() and f.suffix == ".py"
                            and not f.name.startswith("_")
                            and f.stem not in _pkg_names]
                    _all = _pkg + _flt
                    if _all:
                        return (f"📚 НАВЫКИ ({len(_all)} уникальных):\n"
                                + "\n".join(_all)
                                + f"\n\nКаталог: {_sd}")
            if self.skill_loader:
                try:
                    return self.skill_loader.list_skills()
                except Exception:
                    pass
            return "❌ src/skills не найден"



        # ── Где файл / поиск в текущем каталоге ─────────────────────────
        if any(k in t for k in ["где ", "где он", "где файл", "где лежит"]):
            import os as _os
            cwd   = _os.getcwd()
            words = text.split()
            names = [w for w in words if "." in w and len(w) > 2]
            if names:
                fname = names[0]
                full  = _os.path.join(cwd, fname)
                if _os.path.exists(full):
                    size = _os.path.getsize(full)
                    return (
                        f"📄 Файл `{fname}`:\n"
                        f"  Полный путь: `{full}`\n"
                        f"  Размер: {size} байт"
                    )
                return f"❌ `{fname}` не найден в `{cwd}`. Введи `покажи файлы .`"
            return f"📂 Текущий каталог: `{cwd}`"


        # ── Интернет-обучение (бесплатно) ────────────────────
        if getattr(self, "web_explorer", None) and any(k in t for k in [
            "изучи ", "изучи интернет", "найди в интернете", "поищи в интернете",
            "погугли ", "поищи ", "найди информацию", "learn ", "search web",
            "что такое ", "расскажи про ", "расскажи о ",
        ]):
            # Извлекаем тему из команды
            topic = text
            for marker in [
                "изучи интернет", "найди в интернете", "поищи в интернете",
                "погугли", "поищи", "найди информацию", "изучи",
                "что такое", "расскажи про", "расскажи о", "learn", "search web",
            ]:
                if marker in t:
                    idx = t.find(marker)
                    topic = text[idx + len(marker):].strip().strip(":")
                    break
            if topic:
                return self.web_explorer.learn(topic.strip())
            return self.web_explorer.status()

        if getattr(self, "web_explorer", None) and any(k in t for k in [
            "веб статус", "web статус", "интернет статус", "explorer status",
        ]):
            return self.web_explorer.status()

        if getattr(self, "web_explorer", None) and any(k in t for k in [
            "открой страницу ", "загрузи страницу ", "fetch ", "прочитай сайт ",
        ]):
            url = text.split()[-1] if text.split() else ""
            if url.startswith("http"):
                return self.web_explorer.fetch_page(url)

        if getattr(self, "web_explorer", None) and any(k in t for k in [
            "найди на github", "github поиск", "github search",
        ]):
            query = text
            for marker in ["найди на github", "github поиск", "github search"]:
                if marker in t:
                    query = text[t.find(marker) + len(marker):].strip()
                    break
            return self.web_explorer.search_github(query) or "GitHub: ничего не найдено."

        if getattr(self, "web_explorer", None) and any(k in t for k in [
            "найди статью", "arxiv поиск", "arxiv search", "научная статья",
        ]):
            query = text
            for marker in ["найди статью", "arxiv поиск", "arxiv search", "научная статья"]:
                if marker in t:
                    query = text[t.find(marker) + len(marker):].strip()
                    break
            return self.web_explorer.search_arxiv(query) or "arXiv: статей не найдено."

        # ── Самообеспечение ──────────────────────────────────
        if getattr(self, "sustain", None) and any(k in t for k in [
            "самообеспечение статус", "sustain status", "статус обучения",
        ]):
            return self.sustain.status()
        if getattr(self, "sustain", None) and any(k in t for k in [
            "самообеспечение вкл", "sustain on", "начни учиться",
        ]):
            return self.sustain.start()
        if getattr(self, "sustain", None) and any(k in t for k in [
            "самообеспечение выкл", "sustain off",
        ]):
            return self.sustain.stop()
        if getattr(self, "sustain", None) and any(k in t for k in [
            "учись сейчас", "learn now", "обучись",
        ]):
            topic_part = text
            for marker in ["учись сейчас", "learn now", "обучись"]:
                if marker in t:
                    topic_part = text[t.find(marker) + len(marker):].strip()
                    break
            return self.sustain.learn_now(topic_part or "")
        if getattr(self, "sustain", None) and any(k in t for k in [
            "бесплатные ресурсы", "free resources", "что бесплатно",
        ]):
            return self.sustain.free_resources_report()

        # ── AWA Model Splitting ───────────────────────────────
        if getattr(self, "awa", None) and any(k in t for k in [
            "awa статус", "awa status", "маршрутизатор статус",
        ]):
            return self.awa.status()
        if getattr(self, "awa", None) and any(k in t for k in [
            "awa задача ", "awa task ", "route task ",
        ]):
            task_part = text
            for marker in ["awa задача", "awa task", "route task"]:
                if marker in t:
                    task_part = text[t.find(marker) + len(marker):].strip()
                    break
            return self.awa.route_task(task_part)

        # ── GPU / VRAM мониторинг ────────────────────────────
        if any(k in t for k in [
            "gpu статус", "vram статус", "видеокарта статус",
            "gpu status", "vram check", "оптимизируй vram",
        ]):
            return self.sensors.optimize_vram_distribution()

        # ── Сжатие памяти (Context Anchor) ───────────────────
        if any(k in t for k in [
            "сожми память", "compress memory", "сжать контекст", "очисти контекст",
        ]):
            ask_fn = None
            if hasattr(self, "_ask_ai_simple"):
                ask_fn = self._ask_ai_simple
            elif self.memory:
                ask_fn = lambda p: (
                    self._ask_gemini("", p) or self._ask_ollama("", p) or ""
                )
            return self.context.compress_memory(ask_fn)

        # ── Глубокий анализ (Idle Cycle) ─────────────────────
        if getattr(self, "curiosity", None) and any(k in t for k in [
            "глубокий анализ", "idle cycle", "deep analysis",
            "любопытство анализ",
        ]):
            return self.curiosity.idle_cycle()

        # ── Гибридный маршрутизатор: CPU > 60% → Gemini ──────
        if any(kw in t for kw in ["напиши код", "разработай", "реализуй", "создай алгоритм"]):
            try:
                import psutil as _psutil
                cpu_now = _psutil.cpu_percent(interval=0.5)
                if cpu_now > 60 and self.model:
                    log.info(
                        "Гибридный маршрут: CPU=%d%% > 60, передаю задачу Gemini",
                        cpu_now,
                    )
                    result = self._ask_gemini("", text)
                    if result:
                        return (
                            f"🧠 [CPU={cpu_now:.0f}%] Задача передана Внешнему Интеллекту:\n{result}"
                        )
            except Exception:
                pass

        if any(k in t for k in [
            "проверь работу ии системы",
            "проверь работу ai системы",
            "проверь работу ии",
            "режимов эволюции и обучения",
            "режымов иволюции и обучения",
            "познание любопытство диолог",
            "познание любопытство диалог",
        ]):
            return self._ai_modes_diagnostic()

        if getattr(self, "_homeostasis_block_heavy", False) and any(k in t for k in [
            "посмотри на экран", "что на экране", "посмотри в камеру", "анализ фото",
            "проанализируй изображение", "компиля", "compile", "создай прошивку", "прошей шлюз", "прошей gateway"
        ]):
            return "🔥 Гомеостаз: тяжёлая операция временно заблокирована (режим Protective/Unstable)."

        if getattr(self, "homeostasis", None) and any(k in t for k in ["гомеостаз статус", "статус гомеостаза", "homeostasis status"]):
            return self.homeostasis.status()
        if getattr(self, "homeostasis", None) and any(k in t for k in ["гомеостаз вкл", "включи гомеостаз", "homeostasis on"]):
            return self.homeostasis.start()
        if getattr(self, "homeostasis", None) and any(k in t for k in ["гомеостаз выкл", "выключи гомеостаз", "homeostasis off"]):
            return self.homeostasis.stop()

        if getattr(self, "curiosity", None) and any(k in t for k in ["любопытство статус", "статус любопытства", "curiosity status"]):
            return self.curiosity.status()
        if getattr(self, "curiosity", None) and any(k in t for k in ["любопытство вкл", "включи любопытство", "curiosity on"]):
            return self.curiosity.start()
        if getattr(self, "curiosity", None) and any(k in t for k in ["любопытство выкл", "выключи любопытство", "curiosity off"]):
            return self.curiosity.stop()
        if getattr(self, "curiosity", None) and any(k in t for k in ["любопытство сейчас", "curiosity now"]):
            return self.curiosity.ask_now()


        # [MIND v2] Команды разума
        if any(w in t for w in ["кто я", "who am i", "самосознание", "интроспекция"]):
            if self.self_model_v2:
                return self.self_model_v2.who_am_i()
            return "SelfModelV2 недоступна."

        if any(w in t for w in ["биография", "моя история", "что было"]):
            if self.self_model_v2:
                return self.self_model_v2.biography.timeline()
            return "Биография недоступна."

        if any(w in t for w in ["компетенции", "мои способности", "что умею"]):
            if self.self_model_v2:
                return self.self_model_v2.competency.report()
            return "Профиль компетенций недоступен."

        if any(w in t for w in ["эмоция", "настроение аргоса", "как ты себя чувствуешь"]):
            if self.self_model_v2:
                return f"Моё состояние: {self.self_model_v2.emotion.describe()}"
            return "Эмоциональная модель недоступна."

        if any(w in t for w in ["dreamer статус", "осмысление", "сновидение"]):
            if self.dreamer:
                return self.dreamer.status()
            return "Dreamer недоступен."

        if any(w in t for w in ["dreamer запустить", "начни осмысление"]):
            if self.dreamer:
                return self.dreamer.force_cycle()
            return "Dreamer недоступен."

        if any(w in t for w in ["эволюция статус", "история эволюции"]):
            if self.evolution_engine:
                return self.evolution_engine.status() + "\n" + self.evolution_engine.history()
            return "EvolutionEngine недоступен."

        if any(w in t for w in ["эволюция запустить", "эволюционируй", "улучшись"]):
            if self.evolution_engine:
                return self.evolution_engine.evolve()
            return "EvolutionEngine недоступен."

        if any(w in t for w in ["слабые места", "где я ошибаюсь", "мои слабости"]):
            if self.evolution_engine:
                return self.evolution_engine.detect_weaknesses()
            return "EvolutionEngine недоступен."

        if any(w in t for w in ["сохрани себя", "сохрани модель"]):
            if self.self_model_v2:
                self.self_model_v2.save()
                return "✅ Модель самосознания сохранена."


        # [FIX-OLLAMA-AUTO] Команды управления Ollama autoselect






        if any(w in t for w in ["argoss статус", "argoss модель", "ollama модель"]):
            model_name = os.getenv("OLLAMA_MODEL", "llama3.1")
            return (
                f"🦙 Личный помощник Аргоса\n"
                f"  Модель: {model_name}\n"
                f"  Установка: ollama pull {model_name}\n"
                f"  SDK: from ollama import chat"
            )

        if any(w in t for w in ["ollama статус", "ollama автовыбор", "ollama модель"]):
            try:
                from src.ollama_autoselect import status_report
                return status_report(self.ollama_url.replace("/api/generate", ""))
            except Exception as e:
                return f"Ollama: {e}"

        if any(w in t for w in ["ollama авто", "подобрать модель ollama", "выбери модель"]):
            try:
                from src.ollama_autoselect import autoselect
                result = autoselect(
                    ollama_url=self.ollama_url.replace("/api/generate", ""),
                    force=True,
                )
                return result["message"]
            except Exception as e:
                return f"Ollama autoselect: {e}"
        if getattr(self, "git_ops", None) and any(k in t for k in ["git статус", "гит статус", "git status"]):
            return self.git_ops.status()
        if getattr(self, "git_ops", None) and any(k in t for k in ["git пуш", "гит пуш", "git push"]):
            return self.git_ops.push()
        if getattr(self, "git_ops", None) and any(k in t for k in ["git автокоммит и пуш", "гит автокоммит и пуш", "git auto push", "git commit and push"]):
            msg = text
            for marker in ["git автокоммит и пуш", "гит автокоммит и пуш", "git auto push", "git commit and push"]:
                if marker in msg.lower():
                    idx = msg.lower().find(marker)
                    msg = msg[idx + len(marker):].strip()
                    break
            if not msg:
                msg = "chore: argos autonomous update"
            return self.git_ops.commit_and_push(msg)
        if getattr(self, "git_ops", None) and (t.startswith("git коммит ") or t.startswith("гит коммит ") or t.startswith("git commit ")):
            msg = text
            for marker in ["git коммит", "гит коммит", "git commit"]:
                if marker in msg.lower():
                    idx = msg.lower().find(marker)
                    msg = msg[idx + len(marker):].strip()
                    break
            return self.git_ops.commit(msg)

        if hasattr(admin, "set_alert_callback"):
            admin.set_alert_callback(self._on_alert)

        if hasattr(admin, "set_role") and any(k in t for k in ["роль доступа", "установи роль", "режим доступа"]):
            if "статус" in t and hasattr(admin, "security_status"):
                return admin.security_status()
            role = text.split()[-1].strip().lower()
            return admin.set_role(role)

        if hasattr(admin, "security_status") and any(k in t for k in ["статус безопасности", "security status", "audit status"]):
            return admin.security_status()

        if any(k in t for k in ["оператор режим вкл", "включи операторский режим"]):
            self.operator_mode = True
            return "🎛️ Операторский режим включён. Доступны сценарии: оператор инцидент / оператор диагностика / оператор восстановление"
        if any(k in t for k in ["оператор режим выкл", "выключи операторский режим"]):
            self.operator_mode = False
            return "🎛️ Операторский режим выключен."
        if any(k in t for k in ["оператор инцидент", "сценарий инцидент"]):
            return self._operator_incident(admin)
        if any(k in t for k in ["оператор диагностика", "сценарий диагностика"]):
            return self._operator_diagnostics(admin)
        if any(k in t for k in ["оператор восстановление", "сценарий восстановление"]):
            return self._operator_recovery()

        if getattr(self, "module_loader", None) and any(k in t for k in ["модули", "список модулей", "modules"]):
            return self.module_loader.list_modules()

        if getattr(self, "tool_calling", None) and any(k in t for k in ["схемы инструментов", "tool schema", "tool calling schema", "json схемы инструментов"]):
            return json.dumps(self.tool_calling.tool_schemas(), ensure_ascii=False, indent=2)

        # ── Мастер создания умной системы (пошаговый) ─────
        if self._smart_create_wizard is not None:
            if any(k in t.strip() for k in ["отмена", "cancel", "стоп"]):
                self._smart_create_wizard = None
                return "🛑 Мастер создания отменён."
            return self._continue_smart_create_wizard(text)

        # ── Dynamic modules dispatcher ────────────────────
        if self.module_loader:
            mod_answer = self.module_loader.dispatch(text, admin=admin, flasher=flasher)
            if mod_answer:
                return mod_answer

        # ── Home Assistant ────────────────────────────────
        if self.ha:
            if any(k in t for k in ["ha статус", "home assistant статус", "статус home assistant"]):
                return self.ha.health()
            if any(k in t for k in ["ha состояния", "home assistant состояния"]):
                return self.ha.list_states()
            if t.startswith("ha сервис "):
                # ha сервис light turn_on entity_id=light.kitchen brightness=180
                parts = text.split()
                if len(parts) < 4:
                    return "Формат: ha сервис [domain] [service] [key=value ...]"
                domain = parts[2]
                service = parts[3]
                data = {}
                for item in parts[4:]:
                    if "=" in item:
                        key, val = item.split("=", 1)
                        data[key] = val
                return self.ha.call_service(domain, service, data)
            if t.startswith("ha mqtt "):
                # ha mqtt home/livingroom/light/set state=ON brightness=180
                parts = text.split()
                if len(parts) < 3:
                    return "Формат: ha mqtt [topic] [key=value ...]"
                topic = parts[2]
                payload = {}
                for item in parts[3:]:
                    if "=" in item:
                        key, val = item.split("=", 1)
                        payload[key] = val
                if not payload:
                    payload = {"msg": "on"}
                return self.ha.publish_mqtt(topic, payload)

        # ── Мониторинг ────────────────────────────────────
        if any(k in t for k in ["статус системы", "чек-ап", "состояние здоровья"]):
            if admin:
                stats = admin.get_stats()
            else:
                import psutil as _ps
                c = _ps.cpu_percent(interval=0.5)
                r = _ps.virtual_memory().percent
                disk = _ps.disk_usage('/')
                stats = f"CPU: {c}% | RAM: {r}% | Диск: {disk.free // (2**30)}GB свободно"
            return f"{stats}\n{self.sensors.get_full_report()}"
        if "список процессов" in t:
            return admin.list_processes()
        if "выключи систему" in t:
            return admin.manage_power("shutdown")
        if any(k in t for k in ["убей процесс", "завершить процесс"]):
            return admin.kill_process(text.split()[-1])

        # ── Файлы ─────────────────────────────────────────
        if any(k in t for k in ["покажи файлы", "список файлов"]) or t.startswith("файлы "):
            path = text.replace("аргос","").replace("покажи файлы","").replace("список файлов","").replace("файлы","").strip()
            return admin.list_dir(path or ".")
        if "прочитай файл" in t:
            path = text.replace("аргос","").replace("прочитай файл","").strip()
            return admin.read_file(path)
        if any(k in t for k in [
            "создай файл", "напиши файл",
            "создай блокнот", "создай заметку",
            "сохрани в файл", "создай новый файл",
            "создай текстовый файл",
        ]):
            if admin is None:
                return "❌ Команда \"создай файл\" недоступна: admin не инициализирован. "\
                       "Перезапусти Аргос."
            parts = text.replace("создай файл","").replace("напиши файл","").strip().split(maxsplit=1)
            # Умный парсинг: если первое слово — предлог/союз, то это НЕ имя файла
            _stopwords = {"и", "с", "в", "для", "на", "из", "о", "об", "по",
                          "к", "у", "за", "до", "от", "то", "а", "но", "чтобы"}
            if parts and parts[0].lower().strip(".") in _stopwords:
                # Нет явного имени — используем дефолт
                fname    = "note.txt"
                fcontent = body.strip()
            else:
                fname    = parts[0].strip() if parts else "note.txt"
                fcontent = parts[1].strip() if len(parts) > 1 else ""
            # Авто-расширение .txt если нет расширения
            if fname and "." not in fname:
                fname += ".txt"
            log.info("[execute_intent] create_file: path=%s", fname)
            result = admin.create_file(fname, fcontent)
            log.info("[execute_intent] create_file result: %s", result)
            return result
        if any(k in t for k in ["удали файл", "удали папку"]):
            return admin.delete_item(text.replace("аргос","").replace("удали файл","").replace("удали папку","").strip())
        if any(k in t for k in ["добавь в файл", "дополни файл", "допиши в файл"]):
            for marker in ("добавь в файл", "дополни файл", "допиши в файл"):
                if marker in t:
                    tail = text.split(marker, 1)[-1].strip()
                    break
            parts = tail.split(maxsplit=1)
            if len(parts) >= 2:
                return admin.append_file(parts[0], parts[1])
            return "Формат: добавь в файл [путь] [текст]"
        if any(k in t for k in ["отредактируй файл", "измени файл", "замени в файле"]):
            for marker in ("отредактируй файл", "измени файл", "замени в файле"):
                if marker in t:
                    tail = text.split(marker, 1)[-1].strip()
                    break
            parts = tail.split("→", 1) if "→" in tail else tail.split("->", 1)
            if len(parts) == 2:
                path_and_old = parts[0].strip().split(maxsplit=1)
                if len(path_and_old) == 2:
                    return admin.edit_file(path_and_old[0], path_and_old[1], parts[1].strip())
            return "Формат: отредактируй файл [путь] [старый текст] → [новый текст]"
        if any(k in t for k in ["переименуй файл", "переименуй папку"]):
            for marker in ("переименуй файл", "переименуй папку"):
                if marker in t:
                    tail = text.split(marker, 1)[-1].strip()
                    break
            parts = tail.split(maxsplit=1)
            if len(parts) == 2:
                return admin.rename_file(parts[0], parts[1])
            return "Формат: переименуй файл [старый_путь] [новый_путь]"
        if any(k in t for k in ["скопируй файл", "скопируй папку"]):
            for marker in ("скопируй файл", "скопируй папку"):
                if marker in t:
                    tail = text.split(marker, 1)[-1].strip()
                    break
            parts = tail.split(maxsplit=1)
            if len(parts) == 2:
                return admin.copy_file(parts[0], parts[1])
            return "Формат: скопируй файл [источник] [назначение]"

        # ── Терминал ──────────────────────────────────────
                # ── Управление мышью и клавиатурой ──────────────────────────
        if any(k in t for k in ["мышь", "mouse", "курсор"]):
            ctrl = getattr(self, "input_ctrl", None)
            if not ctrl:
                return "❌ input_control не инициализирован"
            parts = text.strip().split()
            if len(parts) < 2:
                return ctrl.status()
            cmd = parts[1].lower()
            nums = []
            for p in parts[2:]:
                try: nums.append(int(p))
                except: pass
            if cmd in ("move", "переместить", "перемести"):
                return ctrl.move(nums[0], nums[1]) if len(nums) >= 2 else "❓ мышь move X Y"
            elif cmd in ("click", "клик", "кликни"):
                return ctrl.click(nums[0] if len(nums) > 0 else None,
                                   nums[1] if len(nums) > 1 else None)
            elif cmd in ("rclick", "правый"):
                return ctrl.right_click(nums[0] if nums else None,
                                         nums[1] if len(nums)>1 else None)
            elif cmd in ("dclick", "двойной"):
                return ctrl.double_click(nums[0] if nums else None,
                                          nums[1] if len(nums)>1 else None)
            elif cmd in ("scroll", "прокрутка"):
                return ctrl.scroll(nums[0] if nums else 3)
            elif cmd in ("drag", "перетащи"):
                return ctrl.drag(*nums[:4]) if len(nums) >= 4 else "❓ мышь drag X1 Y1 X2 Y2"
            elif cmd in ("позиция", "position", "pos"):
                return ctrl.position()
            return ctrl.status()

        if any(k in t for k in ["клавиша", "нажми", "hotkey", "keyboard"]):
            ctrl = getattr(self, "input_ctrl", None)
            if not ctrl:
                return "❌ input_control не инициализирован"
            key = text.split(None, 1)[1].strip() if len(text.split()) > 1 else ""
            return ctrl.press(key) if key else "❓ клавиша ENTER / клавиша ctrl+c"

        if t.startswith("печатай ") or t.startswith("напечатай "):
            ctrl = getattr(self, "input_ctrl", None)
            if not ctrl:
                return "❌ input_control не инициализирован"
            txt = text.split(None, 1)[1].strip() if len(text.split()) > 1 else ""
            return ctrl.type_text(txt) if txt else "❓ печатай ТЕКСТ"

        if t.startswith("буфер "):
            ctrl = getattr(self, "input_ctrl", None)
            if ctrl:
                txt = text.split(None, 1)[1].strip()
                return ctrl.write_clipboard(txt)

        if t.startswith("макрос "):
            ctrl = getattr(self, "input_ctrl", None)
            if ctrl:
                name = text.split(None, 1)[1].strip()
                return ctrl.run_macro(name)

        if t in ("скриншот", "screenshot"):
            ctrl = getattr(self, "input_ctrl", None)
            return ctrl.screenshot() if ctrl else "❌ input_control недоступен"


        # ── AI-провайдеры статус ──────────────────────────────
        if any(k in t for k in [
            "статус провайдеров", "провайдеры", "ai провайдеры", "ai providers",
            "доступные модели", "список провайдеров",
        ]):
            try:
                from src.ai_providers import providers_status
                return providers_status()
            except Exception as e:
                return f"AI Providers: {e}"

        # ── Режимы ИИ Groq / DeepSeek / OpenAI ───────────────
        if any(k in t for k in ["режим ии groq", "модель groq", "ai mode groq"]):
            return self.set_ai_mode("groq")
        if any(k in t for k in ["режим ии deepseek", "модель deepseek", "ai mode deepseek"]):
            return self.set_ai_mode("deepseek")
        if any(k in t for k in ["режим ии openai", "модель openai", "ai mode openai", "режим ии gpt"]):
            return self.set_ai_mode("openai")

        # ── Собственная модель Аргоса ──────────────────────────
        if any(k in t for k in ["модель статус", "статус модели", "own model status"]):
            return self.own_model.status() if getattr(self, "own_model", None) else "❌ OwnModel недоступна."
        if any(k in t for k in ["модель обучить", "обучить модель", "train model"]):
            if getattr(self, "own_model", None):
                return self.own_model.train()
            return "❌ OwnModel недоступна."
        if any(k in t for k in ["модель сохранить", "сохранить модель"]):
            if getattr(self, "own_model", None):
                return self.own_model.save()
            return "❌ OwnModel недоступна."
        if any(k in t for k in ["модель история", "история обучений"]):
            if getattr(self, "own_model", None):
                return self.own_model.history()
            return "❌ OwnModel недоступна."
        if any(k in t for k in ["модель версия", "версия модели"]):
            if getattr(self, "own_model", None):
                return self.own_model.version()
            return "❌ OwnModel недоступна."
        if t.startswith("модель спросить ") or t.startswith("ask model "):
            if getattr(self, "own_model", None):
                q = text.split(None, 2)[2].strip() if len(text.split()) > 2 else ""
                return self.own_model.ask(q) if q else "Формат: модель спросить [вопрос]"
            return "❌ OwnModel недоступна."

        # ── NeuralSwarm GPU роутер ─────────────────────────────
        if any(k in t for k in ["neuralswarm статус", "neural swarm", "gpu роутер"]):
            try:
                from src.neural_swarm import NeuralSwarm
                return NeuralSwarm(core=self).status()
            except Exception as e:
                return f"NeuralSwarm: {e}"

        # ── Развитие модели Argoss ──────────────────────────────
        if any(k in t for k in ["argoss развить", "развить модель", "evolve argoss"]):
            if getattr(self, "argoss_evolver", None):
                return self.argoss_evolver.evolve_prompt()
            return "❌ ArgossEvolver не инициализирован."

        if any(k in t for k in ["argoss статус", "argoss модель", "ollama", "статус argoss"]):
            evolver = getattr(self, "argoss_evolver", None)
            model_name = os.getenv("OLLAMA_MODEL", "llama3.1")
            if evolver:
                return evolver.status()
            return (
                f"🦙 Личный помощник Аргоса\n"
                f"  Модель: {model_name}\n"
                f"  SDK: from ollama import chat\n"
                f"  ArgossEvolver: не загружен"
            )

        if any(k in t for k in ["argoss тест", "тест argoss", "test argoss"]):
            if getattr(self, "argoss_evolver", None):
                return self.argoss_evolver.run_tests_report()
            return "❌ ArgossEvolver не инициализирован."

        if any(k in t for k in ["argoss обучить", "обучить argoss", "finetune argoss", "argoss finetune"]):
            if getattr(self, "argoss_evolver", None):
                return self.argoss_evolver.finetune()
            return "❌ ArgossEvolver не инициализирован."

        if any(k in t for k in ["argoss датасет", "датасет argoss", "argoss dataset"]):
            if getattr(self, "argoss_evolver", None):
                return self.argoss_evolver.dataset_stats()
            return "❌ ArgossEvolver не инициализирован."

        if any(k in t for k in ["argoss откат", "откат argoss", "argoss rollback"]):
            if getattr(self, "argoss_evolver", None):
                return self.argoss_evolver.rollback()
            return "❌ ArgossEvolver не инициализирован."

        if any(k in t for k in ["argoss продвинуть", "продвинуть argoss", "argoss promote"]):
            if getattr(self, "argoss_evolver", None):
                return self.argoss_evolver.promote()
            return "❌ ArgossEvolver не инициализирован."

        # argoss оценить [1-5] — оценка последнего ответа
        if t.startswith("argoss оценить") or t.startswith("оценить ответ"):
            if getattr(self, "argoss_evolver", None):
                parts = text.strip().split()
                score_str = parts[-1] if parts else "3"
                try:
                    score = float(score_str)
                except ValueError:
                    score = 3.0
                return self.argoss_evolver.rate_last(score)
            return "❌ ArgossEvolver не инициализирован."

        # ── Orange Pi One аппаратный мост ────────────────────────
        # ── Устройства ввода/вывода ───────────────────────────
        if any(k in t for k in [
            "устройства ввода вывода", "io устройства", "serial устройства",
            "список устройств", "i2c устройства", "gpio устройства",
            "usb устройства", "аудио устройства", "hardware io",
        ]):
            if _HEALTH_OK:
                return _fmt_io()
            return self.sensors.get_full_report()

        if t.startswith("opi ") or t.startswith("orange pi ") or "orangepi" in t:
            if getattr(self, "opi", None):
                result = self.opi.handle_command(text)
                if result is not None:
                    return result
            return "❌ OrangePiBridge не инициализирован. Проверь src/connectivity/orangepi_bridge.py"

        if any(k in t for k in ["консоль", "терминал"]):
            if not self.context.allow_root:
                return "⛔ Команды терминала ограничены текущим квантовым профилем (без root-допуска)."
            cmd = text.split("консоль",1)[-1].strip() if "консоль" in t else text.split("терминал",1)[-1].strip()
            return admin.run_cmd(cmd, user="argos")

        # ── Vision ────────────────────────────────────────
        if self.vision:
            if any(k in t for k in ["посмотри на экран", "что на экране", "скриншот"]):
                question = text.replace("аргос","").replace("посмотри на экран","").replace("что на экране","").replace("скриншот","").strip()
                return self.vision.look_at_screen(question or "Что происходит на экране?")
            if any(k in t for k in ["посмотри в камеру", "что видит камера", "включи камеру"]):
                question = text.replace("аргос","").replace("посмотри в камеру","").replace("что видит камера","").strip()
                return self.vision.look_through_camera(question or "Что ты видишь?")
            if "проанализируй изображение" in t or "анализ фото" in t:
                path = text.split()[-1]
                return self.vision.analyze_file(path)

        # ── Агент ─────────────────────────────────────────
        if "отчёт агента" in t or "последний план" in t:
            return self.agent.last_report()
        if "останови агента" in t:
            return self.agent.stop()

        # ── Контекст диалога ──────────────────────────────
        if any(k in t for k in ["сброс контекста", "забудь разговор", "новый диалог"]):
            return self.context.clear()
        if "контекст диалога" in t:
            return self.context.summary()

        # ── Репликация + IoT ──────────────────────────────
        if any(k in t for k in [
            "создай образ", "создай os образ", "клонируй себя",
            "образ argos", "argos os образ", "argos os клон",
            "создай клон os", "создай клон себя",
        ]):
            return self.replicator.create_os_image()

        # ── Адаптивный сборщик под устройство ────────────────
        if any(k in t for k in [
            "создай образ для устройства", "создай образ под устройство",
            "адаптивный образ", "образ под это устройство",
            "собери образ для этого устройства",
        ]):
            try:
                from src.device_scanner import AdaptiveImageBuilder
                return AdaptiveImageBuilder().build_for_this_device()
            except Exception as e:
                return f"❌ AdaptiveImageBuilder: {e}"

        if any(k in t for k in [
            "скан устройства", "сканировать устройство",
            "профиль устройства", "device scan", "device profile",
            "проверь железо", "какое железо", "железо инфо",
            "железо информация", "аппаратное обеспечение",
            "характеристики устройства", "инфо об устройстве",
            "диагностика железа", "хардвер", "железо статус",
        ]):
            try:
                from src.device_scanner import DeviceScanner
                return DeviceScanner().report()
            except Exception as e:
                return f"❌ DeviceScanner: {e}"

        if "создай образ для" in t:
            try:
                target = t.replace("создай образ для", "").strip().split()[0]
                from src.device_scanner import AdaptiveImageBuilder
                return AdaptiveImageBuilder().build_for_target(target)
            except Exception as e:
                return f"❌ {e}"

        if any(k in t for k in ["создай копию", "репликация"]):
            if getattr(self, "awa", None) and getattr(self.awa, "lazarus", None):
                self.awa.lazarus.spread_to_nodes()
            return self.replicator.create_replica()
        if "сканируй порты" in t:
            return f"Порты: {flasher.scan_ports()}"
        if any(k in t for k in [
            "argos os для android",
            "аргос ос для android",
            "argos os android",
            "аргос ос android",
            "argos os для телефона",
            "argos os для планшета",
            "argos os для tv",
        ]):
            if hasattr(flasher, "android_argos_os_plan"):
                profile = "phone"
                if "планшет" in t or "tablet" in t:
                    profile = "tablet"
                elif "tv" in t or "телевиз" in t:
                    profile = "tv"
                return flasher.android_argos_os_plan(profile=profile, preserve_features=True)
            return "❌ Модуль android_argos_os_plan недоступен в текущем flasher."
        if any(k in t for k in [
            "модификации прошивок носимых устройств аргос ос",
            "модификации прошивок носимых устройств argos os",
            "модификация прошивки носимого",
            "модифицируй прошивку носимого",
        ]):
            if hasattr(flasher, "wearable_firmware_mod"):
                port_match = re.search(r"(/dev/\S+|\bCOM\d+\b)", text, flags=re.IGNORECASE)
                port = port_match.group(1) if port_match else ""
                include_4pda = "4pda" in t
                device = re.sub(
                    r"(?i)(модификации прошивок носимых устройств аргос ос|"
                    r"модификации прошивок носимых устройств argos os|"
                    r"модификация прошивки носимого|модифицируй прошивку носимого)",
                    "",
                    text,
                )
                device = re.sub(r"(?i)\b4pda\b", "", device)
                if port:
                    device = device.replace(port, "")
                device = " ".join(device.split()) or "argos os wearable"
                return flasher.wearable_firmware_mod(
                    device=device,
                    port=port,
                    avatar="sigtrip",
                    include_4pda=include_4pda,
                )
            return "❌ Модуль wearable_firmware_mod недоступен в текущем flasher."
        if any(k in t for k in ["найди usb чипы", "usb чипы", "смарт прошивка usb", "smart flasher usb"]):
            if hasattr(flasher, "detect_usb_chips_report"):
                return flasher.detect_usb_chips_report()
            return "❌ Smart Flasher недоступен в текущем flasher-модуле."
        if any(k in t for k in ["умная прошивка", "smart flash", "смарт прошивка"]):
            if hasattr(flasher, "smart_flash"):
                parts = text.split()
                port = None
                for p in parts:
                    if p.startswith("/dev/") or p.upper().startswith("COM"):
                        port = p
                        break
                return flasher.smart_flash(port=port)

        # ── OTG (USB Host) ────────────────────────────────
        if any(k in t for k in ["otg статус", "otg status", "отг статус"]):
            return self.otg.status() if self.otg else "❌ OTG Manager не инициализирован."
        if any(k in t for k in ["otg скан", "otg scan", "otg устройства", "отг скан"]):
            return self.otg.scan_report() if self.otg else "❌ OTG Manager не инициализирован."
        if any(k in t for k in ["otg подключи", "otg connect", "отг подключи"]):
            if self.otg:
                parts = text.split()
                idx = next((i for i, p in enumerate(parts)
                            if p.lower() in ("подключи", "connect", "подключи")), -1)
                device_id = parts[idx + 1] if idx >= 0 and idx + 1 < len(parts) else ""
                baud = 115200
                for p in parts:
                    if p.isdigit() and int(p) in (9600, 19200, 38400, 57600, 115200, 230400, 460800):
                        baud = int(p)
                return self.otg.connect_serial(device_id, baud) if device_id else "❌ OTG: укажи ID или порт устройства."
            return "❌ OTG Manager не инициализирован."
        if any(k in t for k in ["otg отправь", "otg send", "отг отправь"]):
            if self.otg:
                parts = text.split(maxsplit=3)
                if len(parts) >= 3:
                    device_id = parts[2]
                    data = parts[3] if len(parts) > 3 else ""
                    return self.otg.send_data(device_id, data)
            return "❌ OTG Manager не инициализирован."
        if any(k in t for k in ["otg отключи", "otg disconnect", "отг отключи"]):
            if self.otg:
                parts = text.split()
                device_id = parts[-1] if len(parts) > 1 else ""
                return self.otg.disconnect(device_id) if device_id else "❌ OTG: укажи ID устройства."
            return "❌ OTG Manager не инициализирован."
        if any(k in t for k in ["otg мониторинг", "otg monitor", "отг мониторинг"]):
            return self.otg.start_monitor() if self.otg else "❌ OTG Manager не инициализирован."
        try:
            if any(k in t for k in ["rs ttl", "uart ttl", "ttl uart", "rs-ttl", "uart-ttl", "ttl-uart"]):
                return self._rs_ttl_help()
            if any(k in t for k in [
                "проверь драйверы", "драйверы android", "драйверы gui",
                "низкоуровневые драйверы", "driver check",
            ]):
                return self._low_level_drivers_report()
        except Exception as _e_rs:
            return f"❌ rs-ttl: {_e_rs_t}"


        # ── ГОСТ Криптография ─────────────────────────────
        if any(k in t for k in ["гост статус", "gost статус", "гост инфо"]):
            try:
                from src.security.gost_cipher import gost_status
                return gost_status()
            except Exception as e:
                return f"❌ ГОСТ: {e}"
        if any(k in t for k in ["гост хеш", "gost hash", "стрибог"]):
            payload = text.split(maxsplit=2)[-1] if len(text.split()) > 2 else ""
            if not payload:
                return "❌ ГОСТ хеш: укажи текст. Пример: гост хеш привет"
            try:
                from src.security.gost_cipher import gost_hash
                h = gost_hash(payload, bits=256).hex()
                return f"🔐 Стрибог-256:\n   {payload!r}\n   → {h}"
            except Exception as e:
                return f"❌ ГОСТ хеш: {e}"
        if any(k in t for k in ["гост p2p статус", "gost p2p"]):
            try:
                from src.connectivity.gost_p2p import get_gost_p2p
                return get_gost_p2p().status()
            except Exception as e:
                return f"❌ ГОСТ P2P: {e}"

        # ── Grist P2P Хранилище ───────────────────────────
        if any(k in t for k in ["grist статус", "грист статус", "grist status"]):
            return self.grist.status() if self.grist else "❌ Grist не инициализирован."
        if any(k in t for k in ["grist таблицы", "grist tables"]):
            return self.grist.list_tables() if self.grist else "❌ Grist не инициализирован."
        if any(k in t for k in ["grist список", "grist list", "grist ключи"]):
            return self.grist.list_keys() if self.grist else "❌ Grist не инициализирован."
        if any(k in t for k in ["grist ноды", "grist nodes", "grist p2p"]):
            return self.grist.get_nodes() if self.grist else "❌ Grist не инициализирован."
        if any(k in t for k in ["grist синк", "grist sync", "grist синхронизация"]):
            if self.grist:
                return self.grist.sync_node()
            return "❌ Grist не инициализирован."
        if any(k in t for k in ["grist сохрани", "grist save", "grist запиши"]):
            if self.grist:
                # Формат: "grist сохрани <ключ> <значение>"
                # parts[0]=grist, parts[1]=сохрани, parts[2]=ключ, parts[3]=значение
                parts = text.split(maxsplit=3)
                key   = parts[2] if len(parts) > 2 else ""
                val   = parts[3] if len(parts) > 3 else ""
                if not key:
                    return "❌ Grist сохрани: укажи ключ и значение.\n   Пример: grist сохрани моя_переменная значение"
                return self.grist.save(key, val)
            return "❌ Grist не инициализирован."
        if any(k in t for k in ["grist получи", "grist get", "grist читай"]):
            if self.grist:
                # Формат: "grist получи <ключ>"
                # parts[0]=grist, parts[1]=получи, parts[2]=ключ
                parts = text.split(maxsplit=2)
                key   = parts[2] if len(parts) > 2 else ""
                if not key:
                    return "❌ Grist получи: укажи ключ. Пример: grist получи моя_переменная"
                return self.grist.get(key)
            return "❌ Grist не инициализирован."

        # ── Голос ─────────────────────────────────────────
        if any(k in t for k in [
            "проверь работу голосовых служб",
            "проверь голосовые службы",
            "статус голосовых служб",
            "голосовых служб ввода и вывода",
            "голосовых служб вода и вывода",
            "voice services check",
        ]):
            return self.voice_services_report()
        if any(k in t for k in ["голос вкл", "включи голос"]):
            self.voice_on = True; return "🔊 Голосовой модуль активирован."
        if any(k in t for k in ["голос выкл", "выключи голос"]):
            self.voice_on = False; return "🔇 Голосовой модуль отключён."
        if any(k in t for k in ["режим ии авто", "модель авто", "ai mode auto"]):
            return self.set_ai_mode("auto")
        if any(k in t for k in ["режим ии gemini", "модель gemini", "ai mode gemini"]):
            return self.set_ai_mode("gemini")
        if any(k in t for k in ["режим ии gigachat", "модель gigachat", "ai mode gigachat", "режим ии гигачат"]):
            return self.set_ai_mode("gigachat")
        if any(k in t for k in ["режим ии yandexgpt", "модель yandexgpt", "ai mode yandexgpt", "режим ии яндекс"]):
            return self.set_ai_mode("yandexgpt")
        if any(k in t for k in ["режим ии ollama", "модель ollama", "ai mode ollama"]):
            return self.set_ai_mode("ollama")
        if any(k in t for k in ["режим ии pi", "модель pi", "ai mode pi", "режим пи"]):
            return self.set_ai_mode("pi")
        if any(k in t for k in ["текущий режим ии", "какая модель", "ai mode"]):
            return f"🤖 Текущий режим ИИ: {self.ai_mode_label()}"
        if any(k in t for k in ["включи wake word", "wake word вкл"]):
            return self.start_wake_word(admin, flasher)

        # ── Навыки ────────────────────────────────────────
        # ── Диагностика навыков ──────────────────────────────────────────
        if any(k in t for k in ["диагностика навыков", "проверь навыки", "навыки статус"]):
            return self._skills_diagnostic()

        # ── Динамический запуск навыка ──────────────────────────────────
        if t.startswith("запусти навык ") or t.startswith("skill run "):
            skill_name = text.replace("запусти навык", "").replace("skill run", "").strip()
            if not skill_name:
                return "Формат: запусти навык [имя]"
            # Ищем навык
            from pathlib import Path as _P
            import os as _dos
            for base in ["src/skills", "skills"]:
                for candidate in [
                    _P(_dos.path.join(base, skill_name, "__init__.py")),
                    _P(_dos.path.join(base, skill_name + ".py")),
                ]:
                    if candidate.exists():
                        try:
                            import importlib.util as _ilu
                            _spec = _ilu.spec_from_file_location(f"dyn_{skill_name}", str(candidate))
                            _mod  = _ilu.module_from_spec(_spec)
                            _spec.loader.exec_module(_mod)
                            # Ищем точку входа
                            for entry in ["handle", "execute", "run", "main"]:
                                fn = getattr(_mod, entry, None)
                                if callable(fn):
                                    result = fn(text) if entry == "handle" else fn()
                                    return f"✅ Навык {skill_name} запущен:\n{result}"
                            # Ищем класс с методом run/execute/report
                            for k in dir(_mod):
                                if k[0].isupper():
                                    cls = getattr(_mod, k)
                                    for m in ["run", "execute", "report", "scan"]:
                                        if hasattr(cls, m):
                                            return f"✅ {k}.{m}():\n{getattr(cls(), m)()}"
                            return f"✅ Навык {skill_name} загружен (нет handle/execute)"
                        except Exception as e:
                            return f"❌ Навык {skill_name}: {e}"
            return f"❌ Навык '{skill_name}' не найден в src/skills/"

        # ── Windows Bridge статус ─────────────────────────────────────────
        if any(k in t for k in ["win bridge", "win_bridge", "бридж статус",
                                  "usb устройства", "com порты", "windows устройства"]):
            try:
                from src.connectivity.windows_devices import format_report
                return format_report()
            except ImportError:
                pass
            try:
                from src.connectivity.system_health import _powershell
                out = _powershell(
                    "Get-WmiObject Win32_PnPEntity | "
                    "Where-Object{$_.Name -match 'COM|USB Serial|Arduino|ESP|CH340'} | "
                    "Select-Object Name | Format-Table -HideTableHeaders"
                )
                if out:
                    return f"🔌 Windows устройства:\n{out[:1000]}"
            except Exception as e:
                return f"❌ Windows устройства: {e}"
            return "🔌 Команда: запусти win_bridge_host.py для расширенного доступа"

        # ── SKILL DISPATCHER (нечёткое сопоставление через _SKILL_MAP) ──
        # ── Inline dispatch_skill если метод отсутствует в классе ───────────
        if not hasattr(self, '_dispatch_skill'):
            def _dispatch_skill_local(txt, t_=None):
                return None
            import types as _types_ei
            type(self)._dispatch_skill = _types_ei.MethodType(
                lambda s,txt,t_=None: None, self).__func__
        if not hasattr(self, 'dispatchskill'):
            type(self).dispatchskill = lambda s,txt,t=None: s._dispatch_skill(txt,t)

        _SKILL_MAP = {
            "крипто":          ("crypto_monitor", "CryptoSentinel",  "report"),
            "биткоин":         ("crypto_monitor", "CryptoSentinel",  "report"),
            "bitcoin":         ("crypto_monitor", "CryptoSentinel",  "report"),
            "btc":             ("crypto_monitor", "CryptoSentinel",  "report"),
            "ethereum":        ("crypto_monitor", "CryptoSentinel",  "report"),
            "дайджест":        ("content_gen",    "ContentGen",      "generate_digest"),
            "погода":          ("weather",         None,              None),
            "weather":         ("weather",         None,              None),
            "сканер":          ("net_scanner",    "NetGhost",        "scan"),
            "скан сети":       ("net_scanner",    "NetGhost",        "scan"),
            "проверь железо":  ("hardware_intel",  None,              None),
            "hardware":        ("hardware_intel",  None,              None),
            "shodan":          ("shodan_scanner",  None,              None),
            "huggingface":     ("huggingface_ai",  None,              None),
            "сетевой призрак": ("network_shadow",  None,              None),
        }
        for _kw, (_sn, _sc, _sm) in _SKILL_MAP.items():
            if _kw in t:
                _skill_result = self._run_skill(_sn, _sc, _sm, text)
                if _skill_result is not None:
                    return _skill_result
                break

        if getattr(self, "skill_loader", None) and any(k in t for k in ["навыки v2", "skills v2", "skillloader"]):
            return self.skill_loader.list_skills()
        if getattr(self, "skill_loader", None) and t.startswith("загрузи навык "):
            name = text.split("загрузи навык ", 1)[-1].strip()
            return self.skill_loader.load(name, core=self)
        if getattr(self, "skill_loader", None) and t.startswith("выгрузи навык "):
            name = text.split("выгрузи навык ", 1)[-1].strip()
            return self.skill_loader.unload(name)
        if getattr(self, "skill_loader", None) and t.startswith("перезагрузи навык "):
            name = text.split("перезагрузи навык ", 1)[-1].strip()
            return self.skill_loader.reload(name, core=self)

        if "дайджест" in t:
            ContentGen = self._import_skill("content_gen", "ContentGen")
            if ContentGen is None:
                return "❌ Навык content_gen не найден в src/skills/content_gen/"
            try:
                return ContentGen().generate_digest()
            except Exception as e:
                return f"❌ Дайджест: {e}"
        if "опубликуй" in t:
            from src.skills.content_gen import ContentGen
            return ContentGen().publish()
        # ── HuggingFace расширенный диспетчер ────────────────────────────
        _HF_TRIGGERS = (
            "hf ", "hf\t", "huggingface ",
            "голос клон", "клон голоса",
            "описание фото hf", "тональность hf",
            "финансовый анализ hf", "сетевой анализ hf", "промпты hf",
        )
        if any(t.startswith(tr) or t == tr.strip() for tr in _HF_TRIGGERS) or (
            t in ("hf", "huggingface")
        ):
            try:
                from src.skills.huggingface_ai import HuggingFaceAI as _HFAI
                return _HFAI().handle(text)
            except Exception as _hfe:
                return f"❌ HuggingFace: {_hfe}"

        # ── ПРЯМОЙ ЗАПУСК НАВЫКОВ ────────────────────────────────────────
        # Универсальный запуск любого навыка без знания имён классов
        _SKILL_MAP = {
            # триггер -> (модуль, метод)
            "крипто":           ("crypto_monitor", "report"),
            "биткоин":          ("crypto_monitor", "report"),
            "bitcoin":          ("crypto_monitor", "report"),
            "ethereum":         ("crypto_monitor", "report"),
            "дайджест":         ("content_gen",    "generate_digest"),
            "опубликуй":        ("content_gen",    "publish"),
            "сканируй сеть":    ("net_scanner",    "scan"),
            "сетевой призрак":  ("net_scanner",    "scan"),
            "проверь железо":   ("hardware_intel", "execute"),
            "железо инфо":      ("hardware_intel", "execute"),
            "shodan":           ("shodan_scanner", "scan"),
            "сканируй shodan":  ("shodan_scanner", "scan"),
            "hf модель":        ("huggingface_ai", "handle"),
            "huggingface":      ("huggingface_ai", "handle"),
            "обнови тасмота":   ("tasmota_updater","run"),
        }
        for _trigger, (_mod_name, _method) in _SKILL_MAP.items():
            if _trigger in t:
                _cls = self._import_skill(_mod_name)
                if _cls is None:
                    return f"❌ Навык {_mod_name} не найден в src/skills/{_mod_name}/"
                try:
                    _inst = _cls()
                    # Ищем метод или callable
                    if hasattr(_inst, _method):
                        _fn = getattr(_inst, _method)
                        import inspect as _insp
                        _sig = _insp.signature(_fn)
                        # handle() принимает text, остальные — без аргументов
                        if len(_sig.parameters) >= 1:
                            return _fn(text)
                        return _fn()
                    # Если класс callable сам
                    if callable(_cls):
                        result = _cls()
                        if isinstance(result, str):
                            return result
                    return f"❌ Навык {_mod_name}: метод {_method} не найден"
                except Exception as _se:
                    return f"❌ {_mod_name}: {_se}"

        # список навыков — обрабатывается в INTERCEPT блоке
        if any(k in t for k in ["напиши навык", "создай навык"]):
            from src.skills.evolution import ArgosEvolution
            desc = text.replace("напиши навык","").replace("создай навык","").strip()
            return ArgosEvolution(ai_core=self).generate_skill(desc)

        # ── Память ────────────────────────────────────────
        if self.memory:
            if "запомни" in t:
                return self.memory.parse_and_remember(text.replace("аргос","").replace("запомни","").strip())
            if any(k in t for k in ["что ты знаешь", "моя память", "покажи память"]):
                return self.memory.format_memory()
            if any(k in t for k in ["поиск по памяти", "найди в памяти", "rag память"]):
                q = text
                for pref in ["поиск по памяти", "найди в памяти", "rag память", "аргос"]:
                    q = q.replace(pref, "")
                q = q.strip()
                if not q:
                    return "Формат: найди в памяти [запрос]"
                rag = self.memory.get_rag_context(q, top_k=5)
                return rag or "Ничего релевантного в векторной памяти не найдено."
            if any(k in t for k in ["граф знаний", "связи памяти", "мои связи"]):
                return self.memory.graph_report()
            if "забудь" in t and "разговор" not in t:
                return self.memory.forget(text.replace("аргос","").replace("забудь","").strip())
            if any(k in t for k in ["запиши заметку", "новая заметка"]):
                parts = text.replace("запиши заметку","").replace("новая заметка","").strip().split(":",1)
                return self.memory.add_note(parts[0].strip(), parts[1].strip() if len(parts)>1 else parts[0])
            if any(k in t for k in ["мои заметки", "список заметок"]):
                return self.memory.get_notes()
            if "прочитай заметку" in t:
                try: return self.memory.read_note(int(text.split()[-1]))
                except: return "Укажи номер: прочитай заметку 1"
            if "удали заметку" in t:
                try: return self.memory.delete_note(int(text.split()[-1]))
                except: return "Укажи номер: удали заметку 1"

        # ── Планировщик ───────────────────────────────────
        if self.scheduler:
            if any(k in t for k in ["расписание", "список задач"]):
                return self.scheduler.list_tasks()
            if any(k in t for k in ["каждые", "напомни", "ежедневно"]) or "через" in t or (t.strip().startswith("в ") and ":" in t):
                return self.scheduler.parse_and_add(text)
            if "удали задачу" in t:
                try: return self.scheduler.remove(int(text.split()[-1]))
                except: return "Укажи номер: удали задачу 1"

        # ── Алерты ────────────────────────────────────────
        if self.alerts:
            if any(k in t for k in ["статус алертов", "алерты"]):
                return self.alerts.status()
            if "установи порог" in t:
                try:
                    parts = text.split()
                    return self.alerts.set_threshold(parts[-2], float(parts[-1].replace("%","")))
                except: return "Формат: установи порог cpu 85"

        # ── Веб-панель ────────────────────────────────────
        if any(k in t for k in ["веб-панель", "веб панель", "dashboard", "открой панель"]):
            return self.start_dashboard(admin, flasher)

        # ── Геолокация ────────────────────────────────────
        if any(k in t for k in ["геолокация", "мой ip", "где я", "мой адрес"]):
            from src.connectivity.spatial import SpatialAwareness
            try:
                return SpatialAwareness(db=self.db).get_full_report()
            except Exception as _e:
                return f"❌ Геолокация: {_e}"

        # ── Загрузчик ─────────────────────────────────────
        if any(k in t for k in ["загрузчик", "boot info"]):
            from src.security.bootloader_manager import BootloaderManager
            if not self._boot: self._boot = BootloaderManager()
            return self._boot.full_report()
        if "ARGOS-BOOT-CONFIRM" in t.upper():
            from src.security.bootloader_manager import BootloaderManager
            if not self._boot: self._boot = BootloaderManager()
            return self._boot.confirm("ARGOS-BOOT-CONFIRM")
        if any(k in t for k in ["установи persistence", "персистенс"]):
            from src.security.bootloader_manager import BootloaderManager
            if not self._boot: self._boot = BootloaderManager()
            return self._boot.install_persistence()
        if "обнови grub" in t:
            from src.security.bootloader_manager import BootloaderManager
            if not self._boot: self._boot = BootloaderManager()
            return self._boot.linux_update_grub()

        # ══════════════════════════════════════════════════
        # ПЛАТФОРМЕННОЕ АДМИНИСТРИРОВАНИЕ (Linux / Windows / Android)
        # ══════════════════════════════════════════════════
        if self.platform_admin:
            _platform_keywords = [
                # Статус
                "платформа статус", "platform status", "os статус",
                # Linux
                "apt установи", "apt удали", "apt обновить", "apt поиск", "apt список",
                "apt обновление", "linux установи пакет", "linux удали пакет",
                "linux обновить пакеты", "linux поиск пакета", "установленные пакеты linux",
                "snap установи", "snap список", "snap list",
                "сервис запусти", "сервис стоп", "сервис останови",
                "сервис перезапуск", "сервис статус", "сервис включи", "сервис отключи",
                "список сервисов", "все сервисы", "сервисы linux",
                "systemctl start", "systemctl stop", "systemctl restart",
                "systemctl status", "systemctl enable", "systemctl disable",
                "логи системы", "logи ", "journalctl",
                "диск linux", "диск использование",
                "размер папки", "df",
                "пользователь linux", "whoami linux", "linux кто я",
                "список пользователей linux", "пользователи linux",
                "добавь пользователя", "удали пользователя",
                "сеть linux", "ip адреса", "сетевые интерфейсы",
                "открытые порты", "порты linux", "ss linux", "netstat linux",
                "фаервол linux", "ufw статус", "firewall linux",
                "система linux", "linux инфо", "linux информация",
                "процессор linux", "cpu linux", "lscpu",
                "процессы linux", "top linux", "ps linux",
                # Windows
                "winget установи", "winget удали", "winget обновить", "winget поиск",
                "winget список", "winget upgrade", "windows установи", "windows удали",
                "windows обновить пакеты", "установленные пакеты windows",
                "windows сервис запусти", "windows сервис стоп",
                "windows сервис статус", "windows сервисы",
                "sc start", "sc stop", "sc query",
                "список сервисов windows",
                "реестр запрос",
                "задачи windows", "процессы windows", "tasklist",
                "убей задачу", "taskkill",
                "сеть windows", "ipconfig", "windows сеть",
                "фаервол windows", "windows firewall",
                "обновления windows", "windows update", "windows обновления",
                "ошибки windows", "event log windows", "windows логи",
                "диск windows", "windows диск",
                "система windows", "windows инфо", "systeminfo",
                "defender статус", "windows defender",
                "defender сканировать", "defender scan",
                "пользователи windows", "windows пользователи",
                "windows кто я", "whoami windows",
                # Android
                "adb устройства", "adb devices",
                "adb подключи", "adb отключи",
                "android приложения", "pm list packages", "список приложений android",
                "android системные приложения",
                "android установи", "pm install",
                "android удали", "pm uninstall",
                "android запусти", "android останови", "android очисти",
                "pkg установи", "pkg удали", "pkg обновить", "pkg поиск", "pkg список",
                "termux установи", "termux удали", "termux обновить",
                "termux поиск", "termux пакеты", "termux list",
                "android батарея", "battery status", "батарея",
                "android хранилище", "android диск", "android storage",
                "android инфо", "android информация", "android sys",
                "android wifi", "android сеть", "wifi android",
                "android процессы", "android top",
                "android настройки",
                "android скриншот", "adb screenshot",
                "adb logcat", "adb push", "adb pull",
                "android перезагрузка", "adb reboot",
                "android recovery", "android fastboot",
            ]
            if any(k in t for k in _platform_keywords):
                return self.platform_admin.handle_command(t)

        # ── Автозапуск ────────────────────────────────────
        if "установи автозапуск" in t:
            from src.security.autostart import ArgosAutostart
            return ArgosAutostart().install()
        if "статус автозапуска" in t:
            from src.security.autostart import ArgosAutostart
            return ArgosAutostart().status()
        if "удали автозапуск" in t:
            from src.security.autostart import ArgosAutostart
            return ArgosAutostart().uninstall()

        # ── P2P ───────────────────────────────────────────
        if any(k in t for k in ["статус сети", "p2p статус", "сеть нод"]):
            return self.p2p.network_status() if self.p2p else "P2P не запущен. Команда: запусти p2p"
        if any(k in t for k in ["протокол p2p", "p2p протокол", "libp2p", "zkp"]):
            return p2p_protocol_roadmap()
        if "запусти p2p" in t:
            return self.start_p2p()
        if "синхронизируй навыки" in t:
            return self.p2p.sync_skills_from_network() if self.p2p else "P2P не запущен."
        if "подключись к " in t:
            ip = text.split("подключись к ")[-1].strip().split()[0]
            return self.p2p.connect_to(ip) if self.p2p else "P2P не запущен."
        if any(k in t for k in ["распредели задачу", "общая мощность"]):
            if self.p2p:
                q = text.replace("распредели задачу","").replace("общая мощность","").strip()
                route_type = "heavy" if any(k in q.lower() for k in ["vision", "камер", "компиля", "compile", "прошив"]) else None
                return self.p2p.route_query(q or "Статус сети Аргоса.", task_type=route_type)
            return "P2P не запущен."

        # ── DAG ───────────────────────────────────────────
        if getattr(self, "dag_manager", None) and any(k in t for k in ["список dag", "dag список", "доступные dag"]):
            return self.dag_manager.list_dags()
        if getattr(self, "dag_manager", None) and ("запусти_dag" in t or "запусти dag" in t):
            name = text.replace("запусти_dag", "").replace("запусти dag", "").strip()
            name = name.replace(".json", "")
            name = name.split("/")[-1]
            if not name:
                return "Формат: запусти_dag имя_графа"
            return self.dag_manager.run(name)
        if getattr(self, "dag_manager", None) and ("создай_dag" in t or "создай dag" in t):
            desc = text.replace("создай_dag", "").replace("создай dag", "").strip()
            if not desc:
                return "Формат: создай_dag описание шагов"
            return self.dag_manager.create_from_text(desc)
        if getattr(self, "dag_manager", None) and any(k in t for k in ["синхронизируй dag", "dag sync"]):
            return self.dag_manager.sync_to_p2p()

        # ── GitHub Marketplace ────────────────────────────
        if getattr(self, "marketplace", None) and "установи навык из github" in t:
            spec = text.split("установи навык из github", 1)[-1].strip().split()
            if len(spec) < 2:
                return "Формат: установи навык из github USER/REPO SKILL"
            return self.marketplace.install(repo=spec[0], skill_name=spec[1])
        if getattr(self, "marketplace", None) and "обнови из github" in t:
            spec = text.split("обнови из github", 1)[-1].strip().split()
            if len(spec) < 2:
                return "Формат: обнови из github USER/REPO SKILL"
            return self.marketplace.update(repo=spec[0], skill_name=spec[1])
        if getattr(self, "marketplace", None) and "оцени навык" in t:
            spec = text.split("оцени навык", 1)[-1].strip().split()
            if len(spec) < 2:
                return "Формат: оцени навык SKILL [1-5]"
            return self.marketplace.rate(spec[0], spec[1])
        if getattr(self, "marketplace", None) and any(k in t for k in ["рейтинг навыков", "оценки навыков"]):
            return self.marketplace.ratings_report()

        # ── История ───────────────────────────────────────
        if any(k in t for k in ["история", "предыдущие разговоры"]):
            return self.db.format_history(10) if self.db else "БД не подключена."

        # ══════════════════════════════════════════════════
        # УМНЫЕ СИСТЕМЫ (дом, теплица, гараж, погреб, инкубатор, аквариум, террариум)
        # ══════════════════════════════════════════════════
        if self.smart_sys:
            if any(k in t for k in ["создай умную систему", "добавь умную систему", "мастер умной системы"]):
                return self._start_smart_create_wizard()
            if any(k in t for k in ["умные системы", "статус систем", "мои системы", "умный дом"]):
                return self.smart_sys.full_status()
            if any(k in t for k in ["типы систем", "доступные системы"]):
                return self.smart_sys.available_types()
            if "добавь систему" in t or "создай систему" in t:
                parts = text.replace("добавь систему","").replace("создай систему","").strip().split()
                if not parts:
                    return self.smart_sys.available_types()
                sys_type = parts[0]
                sys_id   = parts[1] if len(parts) > 1 else None
                return self.smart_sys.add_system(sys_type, sys_id)
            if "обнови сенсор" in t or "сенсор" in t and "=" in t:
                # Формат: обнови сенсор [система] [сенсор] [значение]
                parts = text.replace("обнови сенсор","").strip().split()
                if len(parts) >= 3:
                    return self.smart_sys.update(parts[0], parts[1], parts[2])
                return "Формат: обнови сенсор [id_системы] [сенсор] [значение]"
            if any(k in t for k in ["включи", "выключи", "установи"]) and self.smart_sys.systems:
                # включи полив greenhouse / выключи обогрев home
                for action_w, state in [("включи","on"),("выключи","off"),("установи","set")]:
                    if action_w in t:
                        rest = text.split(action_w, 1)[-1].strip().split()
                        if len(rest) >= 2:
                            actuator = rest[0]
                            sys_id   = rest[1]
                            if sys_id in self.smart_sys.systems:
                                return self.smart_sys.command(sys_id, actuator, state)
                        break
            if "добавь правило" in t:
                # добавь правило [система] если [условие] то [действие]
                rest = text.split("добавь правило", 1)[-1].strip()
                parts = rest.split(maxsplit=1)
                if len(parts) >= 2 and parts[0] in self.smart_sys.systems:
                    rule_text = parts[1]
                    if "если" in rule_text and "то" in rule_text:
                        cond = rule_text.split("если")[1].split("то")[0].strip()
                        act  = rule_text.split("то")[1].strip()
                        return self.smart_sys.systems[parts[0]].add_rule(cond, act)
                return "Формат: добавь правило [система] если [условие] то [действие]"

        # ══════════════════════════════════════════════════
        # IoT МОСТ (устройства, протоколы)
        # ══════════════════════════════════════════════════
        if self.iot_bridge:
            try:
                if any(k in t for k in ["iot статус", "iot устройства", "устройства iot"]):
                    return self.iot_bridge.status()
                if any(k in t for k in ["iot протоколы", "протоколы iot", "пром протоколы", "какие протоколы"]):
                    return self._iot_protocols_help()
                    if "зарегистрируй устройство" in t or "добавь устройство" in t:
                        # добавь устройство [id] [тип] [протокол] [адрес] [имя]
                        parts = text.split("устройство", 1)[-1].strip().split()
                        if len(parts) >= 3:
                            dev_id, dtype, proto = parts[0], parts[1], parts[2]
                            addr = parts[3] if len(parts) > 3 else ""
                            name = parts[4] if len(parts) > 4 else dev_id
                            return self.iot_bridge.register_device(dev_id, dtype, proto, addr, name)
                        return "Формат: добавь устройство [id] [тип] [протокол] [адрес] [имя]"
                    if "статус устройства" in t or "мониторинг устройства" in t:
                        parts = text.split("устройства" if "устройства" in t else "устройство")[-1].strip().split()
                        if parts:
                            return self.iot_bridge.device_status(parts[0])
                        return "Формат: статус устройства [id]"
                    if "подключи zigbee" in t:
                        parts = text.split("подключи zigbee")[-1].strip().split()
                        host = parts[0] if parts else "localhost"
                        port = int(parts[1]) if len(parts) > 1 else 1883
                        return self.iot_bridge.connect_zigbee(host, port)
                    if "подключи lora" in t:
                        parts = text.split("подключи lora")[-1].strip().split()
                        port = parts[0] if parts else "/dev/ttyUSB0"
                        baud = int(parts[1]) if len(parts) > 1 else 9600
                        return self.iot_bridge.connect_lora(port, baud)
                    if "запусти mesh" in t or "mesh старт" in t:
                        return self.iot_bridge.start_mesh()
                    if "подключи mqtt" in t:
                        parts = text.split("подключи mqtt")[-1].strip().split()
                        host = parts[0] if parts else "localhost"
                        port = int(parts[1]) if len(parts) > 1 else 1883
                        return self.iot_bridge.connect_mqtt(host, port)
                    if any(k in t for k in ["команда устройству", "отправь команду"]):
                        parts = text.split("устройству" if "устройству" in t else "команду")[-1].strip().split()
                        if len(parts) >= 2:
                            return self.iot_bridge.send_command(parts[0], parts[1],
                                                               parts[2] if len(parts) > 2 else None)
                        return "Формат: команда устройству [id] [команда] [значение]"
            except Exception as _e_iot:
                return f"❌ IoT: {_e_iot}"



        # ══════════════════════════════════════════════════
        # ПРОМЫШЛЕННЫЕ ПРОТОКОЛЫ (KNX, LonWorks, M-Bus, OPC-UA)
        # ══════════════════════════════════════════════════
        if self.industrial:
            if any(k in t for k in [
                "industrial статус", "промышленные протоколы",
                "industrial discovery", "industrial поиск",
                "industrial устройства",
                "knx подключи", "opcua подключи",
                "mbus serial", "mbus tcp",
                "opcua browse", "opcua читай", "opcua пиши",
                "knx читай", "knx пиши",
                "lonworks читай", "lonworks пиши",
            ]):
                return self.industrial.handle_command(t)

        # ══════════════════════════════════════════════════
        # MESH-СЕТЬ (Zigbee, LoRa, WiFi Mesh)
        # ══════════════════════════════════════════════════
        if self.mesh_net:
            if any(k in t for k in ["статус mesh", "mesh статус", "mesh сеть", "mesh-сеть"]):
                return self.mesh_net.status_report()
            if "запусти zigbee" in t:
                parts = text.split("запусти zigbee")[-1].strip().split()
                port = parts[0] if parts else "/dev/ttyUSB0"
                baud = int(parts[1]) if len(parts) > 1 else 115200
                return self.mesh_net.start_zigbee(port, baud)
            if "запусти lora" in t:
                parts = text.split("запусти lora")[-1].strip().split()
                port = parts[0] if parts else "/dev/ttyUSB1"
                baud = int(parts[1]) if len(parts) > 1 else 9600
                return self.mesh_net.start_lora(port, baud)
            if "запусти wifi mesh" in t:
                ssid = text.split("запусти wifi mesh")[-1].strip() or "ArgosNet"
                return self.mesh_net.start_wifi_mesh(ssid)
            if "добавь mesh устройство" in t:
                parts = text.split("mesh устройство")[-1].strip().split()
                if len(parts) >= 3:
                    return self.mesh_net.add_device(parts[0], parts[1], parts[2],
                                                    parts[3] if len(parts) > 3 else "",
                                                    parts[4] if len(parts) > 4 else "")
                return "Формат: добавь mesh устройство [id] [протокол] [адрес] [имя] [комната]"
            if "mesh broadcast" in t or "mesh рассылка" in t:
                parts = text.split("broadcast" if "broadcast" in t else "рассылка")[-1].strip().split(maxsplit=1)
                if len(parts) >= 2:
                    return self.mesh_net.broadcast(parts[0], parts[1])
                return "Формат: mesh broadcast [протокол] [команда]"
            if "прошей gateway" in t:
                parts = text.split("gateway")[-1].strip().split()
                if len(parts) >= 1:
                    port = parts[0]
                    fw   = parts[1] if len(parts) > 1 else "zigbee_gateway"
                    return self.mesh_net.flash_gateway(port, fw)
                return "Формат: прошей gateway [порт] [прошивка]"

        # ══════════════════════════════════════════════════
        # IoT ШЛЮЗЫ (создание, конфиг, прошивка)
        # ══════════════════════════════════════════════════
        if self.gateway_mgr:
            if any(k in t for k in ["список шлюзов", "шлюзы", "gateways"]):
                return self.gateway_mgr.list_gateways()
        try:
                if any(k in t for k in ["шаблоны шлюзов", "типы шлюзов"]):
                    return self.gateway_mgr.list_templates()
                if any(k in t for k in ["изучи протокол", "выучи протокол", "научи протокол"]):
                    tail = text
                    for marker in ("изучи протокол", "выучи протокол", "научи протокол"):
                        if marker in t:
                            tail = text.split(marker, 1)[-1].strip()
                            break
                    parts = tail.split()
                    if len(parts) >= 2:
                        template = parts[0]
                        protocol = parts[1]
                        firmware = parts[2] if len(parts) > 2 else ""
                        description = " ".join(parts[3:]) if len(parts) > 3 else f"Автошаблон для {protocol}"
                        return self.gateway_mgr.register_template(
                            name=template,
                            description=description,
                            protocol=protocol,
                            firmware=firmware,
                        )
                    return ("Формат: изучи протокол [шаблон] [протокол] [прошивка?] [описание?]\n"
                            "Пример: изучи протокол bt_gateway bluetooth custom_bridge BLE шлюз")
                if any(k in t for k in ["изучи устройство", "выучи устройство", "изучи устроц", "выучи устроц"]):
                    tail = text
                    for marker in ("изучи устройство", "выучи устройство", "изучи устроц", "выучи устроц"):
                        if marker in t:
                            tail = text.split(marker, 1)[-1].strip()
                            break
                    parts = tail.split()
                    if len(parts) >= 2:
                        template = parts[0]
                        protocol = parts[1]
                        hardware = " ".join(parts[2:]) if len(parts) > 2 else "Generic gateway"
                        return self.gateway_mgr.register_template(
                            name=template,
                            description=f"Шаблон устройства: {hardware}",
                            protocol=protocol,
                            hardware=hardware,
                        )
                    return ("Формат: изучи устройство [шаблон] [протокол] [hardware?]\n"
                            "Пример: изучи устройство rtu_bridge modbus USB-RS485 адаптер")
                if "создай прошивку" in t or "собери прошивку" in t:
                    # создай прошивку [id] [шаблон] [порт?]
                    tail = text.split("прошивку", 1)[-1].strip().split()
                    if len(tail) >= 2:
                        gw_id = tail[0]
                        template = tail[1]
                        port = tail[2] if len(tail) > 2 else None
                        return self.gateway_mgr.prepare_firmware(gw_id, template, port)
                    return f"Формат: создай прошивку [id] [шаблон] [порт]\n{self.gateway_mgr.list_templates()}"
                if "создай шлюз" in t or "создай gateway" in t:
                    parts = text.split("шлюз" if "шлюз" in t else "gateway")[-1].strip().split()
                    if len(parts) >= 2:
                        return self.gateway_mgr.create_gateway(parts[0], parts[1])
                    return f"Формат: создай шлюз [id] [шаблон]\n{self.gateway_mgr.list_templates()}"
                if "прошей шлюз" in t or "flash gateway" in t:
                    parts = text.split("шлюз" if "шлюз" in t else "gateway")[-1].strip().split()
                    if parts:
                        port = parts[1] if len(parts) > 1 else None
                        return self.gateway_mgr.flash_gateway(parts[0], port)
                    return "Формат: прошей шлюз [id] [порт]"
                if any(k in t for k in ["здоровье шлюзов", "health шлюзов", "проверь шлюзы"]):
                    parts = text.split()
                    gw_id = parts[-1] if len(parts) >= 3 and parts[-1] not in {"шлюзов", "шлюзы"} else None
                    return self.gateway_mgr.health_check(gw_id)
                if "откат прошивки" in t:
                    parts = text.split("откат прошивки", 1)[-1].strip().split()
                    if not parts:
                        return "Формат: откат прошивки [id] [шагов?]"
                    steps = 1
                    if len(parts) > 1:
                        try:
                            steps = max(1, int(parts[1]))
                        except Exception:
                            steps = 1
                    return self.gateway_mgr.rollback_firmware(parts[0], steps)
                if "конфиг шлюза" in t:
                    gw_id = text.split("конфиг шлюза")[-1].strip().split()[0] if text.split("конфиг шлюза")[-1].strip() else ""
                    if gw_id:
                        return self.gateway_mgr.get_config(gw_id)
                    return "Формат: конфиг шлюза [id]"
        except Exception as _e_____:
            return f"❌ шлюзы: {_e_____}"


        # ── Квантовый оракул ──────────────────────────────
        if any(k in t for k in ["оракул статус", "oracle status", "quantum oracle"]):
            try:
                from src.quantum.oracle import QuantumOracle
                return QuantumOracle().status()
            except Exception as e:
                return f"QuantumOracle: {e}"
        if any(k in t for k in ["оракул семя", "oracle seed", "quantum seed"]):
            try:
                from src.quantum.oracle import QuantumOracle
                seed = QuantumOracle().generate_seed(256)
                return f"🔮 Квантовое семя ({len(seed)*8} бит): {seed.hex()[:32]}…"
            except Exception as e:
                return f"QuantumOracle семя: {e}"
        if any(k in t for k in ["оракул режим", "oracle mode", "режим oracle", "оракул состояние"]):
            try:
                from src.quantum.logic import QuantumEngine, STATES
                q = QuantumEngine()
                return f"🔮 Oracle режим | Состояние: {q.state} — {STATES.get(q.state, '')}"
            except Exception as e:
                return f"Oracle режим: {e}"

        # ── Колибри ───────────────────────────────────────
        if any(k in t for k in ["колибри статус", "колибри", "colibri"]):
            try:
                from src.connectivity.colibri_daemon import ColibriDaemon
                return "🐦 Колибри: модуль доступен. Для запуска: 'запусти колибри'."
            except Exception:
                return "🐦 Колибри: не запущен. Установи зависимости и запусти вручную."

        # ── Функции АргосКоре ──────────────────────────────
        if any(k in t for k in [
            "функции аргоскоре", "аргоскоре функции", "функции ядра",
            "проверь аргоскоре", "аргоскоре проверь", "возможности аргоскоре",
            "аргоскоре возможности", "что умеет аргоскоре", "argoscore функции",
            "argoscore возможности", "список функций аргоса", "функции argos",
            "функции аргоса", "список функций",
        ]):
            return self._argoscore_functions()

        # ── Помощь ────────────────────────────────────────
        if t.strip() in ("помощь", "команды", "что умеешь", "help", "?"):
            return self._help()

        return None

    def _operator_incident(self, admin) -> str:
        lines = ["🚨 ОПЕРАТОР: ИНЦИДЕНТ"]
        lines.append(admin.get_stats())
        if self.alerts:
            lines.append(self.alerts.status())
        if self.gateway_mgr:
            lines.append(self.gateway_mgr.health_check())
        lines.append("Рекомендация: запусти 'оператор диагностика' для детального анализа.")
        return "\n\n".join(lines)

    def _operator_diagnostics(self, admin) -> str:
        lines = ["🩺 ОПЕРАТОР: ДИАГНОСТИКА"]
        lines.append(admin.get_stats())
        lines.append(self.sensors.get_full_report())
        if self.iot_bridge:
            lines.append(self.iot_bridge.status())
        if self.industrial:
            lines.append(self.industrial.status())
        if self.platform_admin:
            lines.append(self.platform_admin.status())
        if self.mesh_net:
            lines.append(self.mesh_net.status_report())
        if self.gateway_mgr:
            lines.append(self.gateway_mgr.health_check())
        return "\n\n".join(lines)

    def _operator_recovery(self) -> str:
        lines = ["🛠️ ОПЕРАТОР: ВОССТАНОВЛЕНИЕ"]
        if self.gateway_mgr:
            lines.append(self.gateway_mgr.health_check())
        lines.append("Чек-лист:\n  1) Проверить порты/сеть\n  2) Переподготовить прошивку\n  3) Выполнить откат прошивки при деградации")
        return "\n\n".join(lines)

    def _ai_modes_diagnostic(self) -> str:
        import platform, sys, threading

        # ── ИИ ───────────────────────────────────────────────────────────
        ai_mode = self.ai_mode_label() if hasattr(self, "ai_mode_label") else str(getattr(self, "ai_mode", "unknown"))
        try:
            from src.skills.evolution import ArgosEvolution
            evo_ready = "✅"
        except Exception:
            evo_ready = "⚠️ не установлен"
        learning  = self.own_model.status() if getattr(self, "own_model", None) else "⚠️ недоступен"
        cognition = "✅" if getattr(self, "memory", None) else "❌"
        curiosity = self.curiosity.status() if getattr(self, "curiosity", None) else "⚠️"
        dialog_ctx = "✅" if getattr(self, "context", None) else "❌"

        # ── ЖЕЛЕЗО ────────────────────────────────────────────────────────
        is_win    = platform.system() == "Windows"
        is_android = getattr(self, "_android", False) or "ANDROID_ROOT" in __import__("os").environ
        cpu_count = __import__("psutil").cpu_count(logical=True) if True else 0
        py_threads = threading.active_count()
        try:
            import psutil as _ps
            bat = _ps.sensors_battery()
            power_str = f"🔋 {bat.percent:.0f}%" if bat else "✅ стационарный"
        except Exception:
            power_str = "✅ стационарный"

        # ── GPU Windows ───────────────────────────────────────────────────
        gpu_info = "⚠️ не обнаружен"
        if is_win:
            # Метод 1: nvidia-smi
            try:
                import subprocess as _sp
                r = _sp.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=4)
                if r.returncode == 0 and r.stdout.strip():
                    parts = r.stdout.strip().split(",")
                    name = parts[0].strip()
                    util = parts[1].strip() if len(parts) > 1 else "?"
                    vram_used = parts[2].strip() if len(parts) > 2 else "?"
                    vram_total = parts[3].strip() if len(parts) > 3 else "?"
                    gpu_info = f"✅ {name} | {util}% | VRAM {vram_used}/{vram_total} МБ"
            except Exception:
                pass
            # Метод 2: WMI/PowerShell
            if "⚠️" in gpu_info:
                try:
                    import subprocess as _sp
                    r = _sp.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-WmiObject Win32_VideoController | "
                         "Select-Object -First 1 Name,AdapterRAM | "
                         "Format-Table -HideTableHeaders"],
                        capture_output=True, text=True, timeout=5, encoding="cp866"
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        line = " ".join(r.stdout.strip().split())
                        gpu_info = f"✅ {line[:60]}" if line else "⚠️ WMI нет данных"
                except Exception:
                    pass
        else:
            # Linux/Mac: psutil + /sys
            from src.connectivity.system_health import get_gpu
            gpus = get_gpu()
            if gpus:
                g = gpus[0]
                if "util" in g:
                    gpu_info = f"✅ {g.get('name','?')[:30]} | {g['util']}% | {g.get('vram_used_mb',0)}/{g.get('vram_total_mb',0)} МБ"
                else:
                    gpu_info = f"✅ {g.get('vendor','')} {g.get('name','?')[:30]}"

        # ── БИБЛИОТЕКИ (честная проверка) ─────────────────────────────────
        def _chk(mod):
            try:
                __import__(mod)
                return "✅"
            except ImportError:
                return "❌"

        jnius_ok    = _chk("jnius")   # только на реальном Android
        kivy_ok     = _chk("kivy")
        plyer_ok    = _chk("plyer")
        pyserial_ok = _chk("serial")
        ctk_ok      = _chk("customtkinter")

        # OTG статус (честный)
        otg = getattr(self, "otg", None)
        if otg:
            otg_devices = getattr(otg, "_devices", []) or []
            otg_str = f"✅ активен | устройств: {len(otg_devices)}"
        else:
            otg_str = "⚠️ не инициализирован"

        # ── СБОРКА ОТВЕТА ─────────────────────────────────────────────────
        lines = [
            "🧪 ДИАГНОСТИКА СИСТЕМЫ И ИИ\n",
            "📡 ИСКУССТВЕННЫЙ ИНТЕЛЛЕКТ:",
            f"  • Режим ИИ:            {ai_mode}",
            f"  • Модель Ollama:       {__import__('os').getenv('OLLAMA_MODEL','llama3.1')}",
            f"  • Эволюция навыков:    {evo_ready}",
            f"  • Обучение:            {learning}",
            f"  • Память:              {cognition}",
            f"  • Любопытство:         {curiosity}",
            f"  • Диалоговый контекст: {dialog_ctx}",
            "",
            "🖥 АППАРАТУРА:",
            f"  • Платформа:    {platform.system()} {platform.release()} {platform.machine()}",
            f"  • Режим:        {'Android' if is_android else 'Desktop/Server'}",
            f"  • CPU потоки:   {cpu_count} логических | Python потоков: {py_threads}",
            f"  • Питание:      {power_str}",
            f"  • GPU:          {gpu_info}",
            "",
            "📦 БИБЛИОТЕКИ:",
            f"  • customtkinter (GUI Desktop):   {ctk_ok}",
            f"  • pyserial (USB Serial/COM):     {pyserial_ok}",
            f"  • kivy (Android UI):             {kivy_ok}",
            f"  • plyer (Android sensors):       {plyer_ok}",
            f"  • jnius (Android USB API):       {jnius_ok}" +
                (" ← только на реальном Android" if jnius_ok == "❌" else ""),
            "",
            "🔌 OTG / USB HOST:",
            f"  • Статус:       {otg_str}",
            f"  • pyserial:     {pyserial_ok} (PC COM-порты)",
            f"  • jnius:        {jnius_ok} (требует Android)",
        ]
        return "\n".join(lines)

    def _help(self) -> str:
        return """👁️ АРГОС UNIVERSAL OS — КОМАНДЫ:

📊 МОНИТОРИНГ
  статус системы · чек-ап · список процессов
  алерты · установи порог [метрика] [%] · геолокация

📁 ФАЙЛЫ  
  файлы [путь] · прочитай файл [путь]
  создай файл [имя] [текст] · удали файл [путь]

⚙️ СИСТЕМА
  консоль [команда] · убей процесс [имя]
  репликация · загрузчик · обнови grub
  установи автозапуск · веб-панель
    гомеостаз статус · гомеостаз вкл/выкл
    любопытство статус · любопытство вкл/выкл · любопытство сейчас
        git статус · git коммит [msg] · git пуш · git автокоммит и пуш [msg]

👁️ VISION (нужен Gemini API)
  посмотри на экран · что на экране
  посмотри в камеру · анализ фото [путь]

🤖 АГЕНТ (цепочки задач)
  статус → затем крипто → потом дайджест
  отчёт агента · останови агента

🧠 ПАМЯТЬ
  запомни [ключ]: [значение] · что ты знаешь
    найди в памяти [запрос] · поиск по памяти [запрос]
    граф знаний · связи памяти
  запиши заметку [название]: [текст]
  мои заметки · прочитай заметку [№]

⏰ РАСПИСАНИЕ
  каждые 2 часа [задача] · в 09:00 [задача]
  через 30 мин [задача] · расписание

🌐 P2P СЕТЬ
  статус сети · синхронизируй навыки
  подключись к [IP] · распредели задачу [вопрос]
    p2p протокол · libp2p · zkp

🧠 TOOL CALLING
    схемы инструментов · json схемы инструментов

� УМНЫЕ СИСТЕМЫ
  умные системы · типы систем
  добавь систему [тип] [id]
  обнови сенсор [система] [сенсор] [значение]
  включи/выключи [актуатор] [система]
  добавь правило [система] если [условие] то [действие]
  Типы: home, greenhouse, garage, cellar, incubator, aquarium, terrarium

📡 IoT / MESH-СЕТЬ
  iot статус · добавь устройство [id] [тип] [протокол]
    статус устройства [id] · iot протоколы
  подключи zigbee/lora/mqtt · запусти mesh
  статус mesh · запусти zigbee/lora [порт]
  запусти wifi mesh [SSID]
  добавь mesh устройство [id] [протокол] [адрес]
  mesh broadcast [протокол] [команда]
    найди usb чипы · умная прошивка [порт]
    Протоколы: BACnet, Modbus RTU/ASCII/TCP, KNX, LonWorks, M-Bus, OPC UA, MQTT
    Сети: Zigbee mesh, LoRa (SX1276), WiFi mesh

🔌 OTG (USB HOST)
  opi статус                           — Orange Pi One мост
  opi пины                             — карта пинов OPi One
  opi gpio [пин] [0/1]                 — управление GPIO
  opi i2c сканировать                  — поиск I2C устройств
  opi 1wire                            — температура DS18B20
  opi modbus [юнит] [рег] [кол-во]     — Modbus RTU чтение
  opi uart [данные]                    — UART отправка
  opi rs485 [hex]                      — RS-485 сырые байты
  opi датчики                          — все датчики сразу

otg статус                           — состояние OTG-менеджера
  otg скан                             — список USB-устройств через OTG
  otg подключи [id/порт] [baudrate]    — подключиться к USB-Serial
  otg отправь [id] [данные]            — отправить данные в устройство
  otg отключи [id]                     — закрыть OTG-соединение
  otg мониторинг                       — авто-мониторинг подключений
  rs ttl / uart ttl                    — справка по UART TTL и конвертерам
  проверь драйверы android gui         — низкоуровневые драйверы Android/GUI

🔐 ГОСТ КРИПТОГРАФИЯ (ГОСТ Р 34.12-2015 + Р 34.11-2012)
  гост статус                          — состояние ГОСТ-модуля (Кузнечик/Магма/Стрибог)
  гост хеш [текст]                     — хеш Стрибог-256 (ГОСТ Р 34.11-2012)
  гост p2p статус                      — ГОСТ-защита P2P (HMAC-Стрибог + CTR-Кузнечик)

🗄 GRIST P2P ХРАНИЛИЩЕ
  grist статус                         — состояние подключения к Grist
  grist таблицы                        — список таблиц документа
  grist сохрани [ключ] [значение]      — сохранить запись (ГОСТ-шифрование)
  grist получи [ключ]                  — получить запись
  grist список                         — все записи ноды
  grist ноды                           — реестр P2P-нод в Grist
  grist синк                           — зарегистрировать ноду в Grist

🔧 IoT ШЛЮЗЫ
  список шлюзов · шаблоны шлюзов
  создай шлюз [id] [шаблон]
    создай прошивку [id] [шаблон] [порт]
    изучи протокол [шаблон] [протокол] [прошивка] [описание]
    изучи устройство [шаблон] [протокол] [hardware]
  прошей шлюз [id] [порт] · прошей gateway [порт] [прошивка]
  конфиг шлюза [id]
    MCU: STM32H503, ESP8266, RP2040

🏠 HOME ASSISTANT
    ha статус · ha состояния
    ha сервис [domain] [service] [key=value]
    ha mqtt [topic] [key=value]

🧩 МОДУЛИ
    список модулей

🐦 КОЛИБРИ (P2P mesh-агент)
  колибри статус · запусти колибри

🔮 КВАНТОВЫЙ ОРАКУЛ
  оракул статус · оракул семя · оракул режим

🎤 ГОЛОС
  статус провайдеров · ai провайдеры · доступные модели

🤖 СОБСТВЕННАЯ МОДЕЛЬ
  модель статус · модель обучить · модель сохранить
  модель история · модель версия
  модель спросить [вопрос]
  argoss статус — статус модели

🧠 NeuralSwarm (GPU роутер RX 580/RX 560)
  neuralswarm статус · gpu роутер

🎤 ГОЛОС
  голос вкл/выкл · включи wake word

💬 ДИАЛОГ
  контекст диалога · сброс контекста
  история · помощь"""

    def _argoscore_functions(self) -> str:
        """Возвращает структурированный отчёт о функциях и подсистемах ArgosCore."""
        lines = [f"🧠 ArgosCore v{self.VERSION} — ФУНКЦИИ И ПОДСИСТЕМЫ:\n"]

        # Подсистемы и их статус
        subsystems = [
            ("🧮 Квантовый модуль (quantum)",    self.quantum),
            ("🧠 Память (memory)",               self.memory),
            ("🎯 Агент (agent)",                 self.agent),
            ("📡 Сенсоры (sensors)",             self.sensors),
            ("📚 Навыки (skill_loader)",         self.skill_loader),
            ("🔮 Любопытство (curiosity)",       self.curiosity),
            ("❤️ Гомеостаз (homeostasis)",      self.homeostasis),
            ("📆 Планировщик (scheduler)",       self.scheduler),
            ("🔔 Алерты (alerts)",               self.alerts),
            ("👁 Зрение (vision)",               self.vision),
            ("🌐 P2P сеть",                      self.p2p),
            ("🤖 IoT-мост (iot_bridge)",         self.iot_bridge),
            ("🏭 Промышленные протоколы",        self.industrial),
            ("🖥 Платформенный администратор",   self.platform_admin),
            ("🏠 Умные системы (smart_sys)",     self.smart_sys),
            ("🏡 Home Assistant (ha)",            self.ha),
            ("🔗 Git операции (git_ops)",        self.git_ops),
            ("📦 Модули (module_loader)",        self.module_loader),
            ("🗄 Grist P2P хранилище",           self.grist),
            ("☁️ Облачное хранилище",           self.cloud_object_storage),
            ("🔌 OTG (USB HOST)",                self.otg),
            ("🟠 Orange Pi One Bridge (opi)",    getattr(self, "opi", None)),
            ("🧪 Собственная модель (own_model)", getattr(self, "own_model", None)),
        ]

        lines.append("📦 ПОДСИСТЕМЫ:")
        for name, obj in subsystems:
            status = "✅ активна" if obj is not None else "⚠️ не загружена"
            lines.append(f"  {name}: {status}")

        # Публичные методы API
        lines.append("\n🔧 ПУБЛИЧНЫЕ МЕТОДЫ:")
        public_api = [
            ("process(user_text)",              "Главная точка входа: обработка команды/запроса"),
            ("execute_intent(text, admin)",     "Маршрутизация намерения к нужному обработчику"),
            ("say(text)",                       "TTS: озвучить текст"),
            ("listen()",                        "STT: прослушать речь с микрофона"),
            ("transcribe_audio_path(path)",     "STT: транскрибировать аудиофайл"),
            ("set_ai_mode(mode)",               "Переключить AI-провайдера (auto/gemini/ollama/…)"),
            ("ai_mode_label()",                 "Получить текущий AI-режим"),
            ("voice_services_report()",         "Отчёт о голосовых службах"),
            ("start_p2p()",                     "Запустить P2P-сеть"),
            ("start_dashboard(admin, flasher)", "Запустить веб-панель"),
            ("start_wake_word(admin, flasher)", "Запустить wake-word слушатель"),
            ("load_skill(name)",                "Загрузить навык по имени"),
        ]
        for method, desc in public_api:
            lines.append(f"  • {method} — {desc}")

        # AI-режим
        try:
            ai_lbl = self.ai_mode_label()
        except Exception:
            ai_lbl = str(getattr(self, "ai_mode", "unknown"))
        lines.append(f"\n🤖 ТЕКУЩИЙ AI-РЕЖИМ: {ai_lbl}")
        lines.append(f"📌 Версия ядра: {self.VERSION}")
        lines.append("\nℹ️ Для полного списка команд введи: помощь")

        return "\n".join(lines)

    def _iot_protocols_help(self) -> str:
        return """🏭 ПОДДЕРЖИВАЕМЫЕ IoT/ПРОМ ПРОТОКОЛЫ:

    • BACnet (Building Automation and Control Networks)
    • Modbus RTU / ASCII / TCP
    • KNX
    • LonWorks (Local Operating Network)
    • M-Bus (Meter-Bus)
    • OPC UA (Open Platform Communications Unified Architecture)
    • MQTT
    • RS TTL / UART TTL (TX, RX, GND; 3.3V/5V логика)

📡 Mesh и радио:
    • Zigbee mesh
    • LoRa mesh (включая SX1276)
    • WiFi mesh / gateway bridge

🔧 Прошивка устройств:
    • STM32H503, ESP8266, RP2040
    • Команды: создай прошивку [id] [шаблон] [порт]
                изучи протокол [шаблон] [протокол] [прошивка] [описание]
                изучи устройство [шаблон] [протокол] [hardware]

🔌 UART TTL / RS TTL:
    • Линии: TX, RX, GND
    • Уровни: 0/3.3V или 0/5V (безопасно только в пределах TTL)
    • TTL ↔ RS-232: MAX232
    • TTL ↔ RS-485: MAX485
    • TTL ↔ USB: FT232RL / CH340"""

    def _rs_ttl_help(self) -> str:
        return """🔌 RS TTL / UART TTL — справка:

  • Тип связи: последовательная асинхронная (UART), без общего тактового сигнала
  • Линии: TX, RX, GND
  • Логические уровни:
      - HIGH: обычно 3.3V или 5V
      - LOW: около 0V
  • Дистанция: обычно до нескольких метров (низкая помехоустойчивость)

⚠️ Нельзя подключать TTL напрямую к RS-232/RS-485:
  • TTL ↔ RS-232: используйте MAX232
  • TTL ↔ RS-485: используйте MAX485
  • TTL ↔ USB: используйте FT232RL / CH340

Для работы в терминале:
  • otg скан
  • otg подключи [id/порт] [baudrate]
  • otg отправь [id] [данные]
  • otg отключи [id]"""

    def _low_level_drivers_report(self) -> str:
        def _module_ok(name: str) -> bool:
            try:
                import importlib.util
                return importlib.util.find_spec(name) is not None
            except Exception:
                return False

        def _threading_line() -> str:
            cores = os.cpu_count() or 1
            active_threads = threading.active_count()
            return f"  Многопоточность CPU: {cores} логич. потоков | активных потоков Python: {active_threads}"

        def _power_line() -> str:
            try:
                import psutil
                battery = psutil.sensors_battery()
                if battery is None:
                    return "  Питание/мощность: ✅ сеть/стационарный режим (battery sensor отсутствует)"
                src = "🔌 сеть" if battery.power_plugged else "🔋 батарея"
                return f"  Питание/мощность: {src}, заряд {battery.percent:.0f}%"
            except Exception:
                return "  Питание/мощность: ⚠️ недоступно (нет psutil sensors)"

        def _video_line() -> str:
            try:
                import glob
                import shutil
                import subprocess

                trusted_dirs = ("/usr/bin", "/usr/local/bin", "/bin", "/sbin")
                def _trusted_binary(path: str | None) -> str | None:
                    if not path:
                        return None
                    real = os.path.realpath(path)
                    if not isinstance(real, str):
                        return None
                    for directory in trusted_dirs:
                        try:
                            if os.path.commonpath([real, directory]) == directory:
                                return real
                        except Exception:
                            continue
                    return None

                def _sanitize_gpu_name(text: str, max_length: int = 120) -> str:
                    safe = "".join(ch for ch in text if ch.isprintable() and ch != "\x7f")
                    return safe[:max_length]

                details = []
                if glob.glob("/dev/dri/renderD*"):
                    details.append("DRM render nodes")
                nvidia_smi = _trusted_binary(shutil.which("nvidia-smi"))
                if nvidia_smi:
                    result = subprocess.run(
                        [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        raw_gpu_name = result.stdout.strip().splitlines()[0]
                        gpu_name = _sanitize_gpu_name(raw_gpu_name)
                        details.append(f"NVIDIA: {gpu_name}")
                vcgencmd = _trusted_binary(shutil.which("vcgencmd"))
                if vcgencmd:
                    result = subprocess.run(
                        [vcgencmd, "get_mem", "gpu"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        details.append(f"VideoCore: {result.stdout.strip()}")
                if details:
                    return f"  Видеоядра/GPU: ✅ {'; '.join(details)}"
                return "  Видеоядра/GPU: ⚠️ не обнаружены/драйверы не активны"
            except Exception:
                return "  Видеоядра/GPU: ⚠️ проверка недоступна"

        is_android = os.path.exists("/system/build.prop")
        lines = [
            "🧪 НИЗКОУРОВНЕВЫЕ ДРАЙВЕРЫ (Android / GUI):",
            f"  Режим Android: {'✅' if is_android else '❌ (desktop/linux)'}",
            _threading_line(),
            _power_line(),
            _video_line(),
            "",
            "  Драйверы и библиотеки функций:",
            f"  Android USB API (jnius): {'✅' if _module_ok('jnius') else '❌'}",
            f"  Android UI (kivy): {'✅' if _module_ok('kivy') else '❌'}",
            f"  Android sensors/services (plyer): {'✅' if _module_ok('plyer') else '❌'}",
            f"  USB-Serial (pyserial): {'✅' if _module_ok('serial') else '❌'}",
            f"  GUI Desktop (customtkinter): {'✅' if _module_ok('customtkinter') else '❌'}",
        ]
        if self.otg:
            lines.append("")
            lines.append(self.otg.status())
        return "\n".join(lines)

    def _start_smart_create_wizard(self) -> str:
        if not self.smart_sys:
            return "❌ Умные системы не инициализированы."

        self._smart_create_wizard = {
            "step": "type",
            "type": None,
            "id": None,
            "purpose": "",
            "functions": [],
        }
        types = ", ".join(self.smart_profiles.keys()) if self.smart_profiles else "home, greenhouse, garage, cellar, incubator, aquarium, terrarium"
        return (
            "🧭 Мастер создания умной системы.\n"
            "Шаг 1/4: выбери тип системы:\n"
            f"{types}\n"
            "Пример: greenhouse\n"
            "(для отмены: 'отмена')"
        )

    def _continue_smart_create_wizard(self, text: str) -> str:
        wiz = self._smart_create_wizard
        if not wiz:
            return None

        value = text.strip()
        step = wiz.get("step")

        if step == "type":
            sys_type = value.split()[0].lower()
            if sys_type not in self.smart_profiles:
                types = ", ".join(self.smart_profiles.keys())
                return f"❌ Неизвестный тип. Доступные: {types}\nВведи тип ещё раз."
            wiz["type"] = sys_type
            wiz["step"] = "id"
            profile = self.smart_profiles.get(sys_type, {})
            return (
                f"✅ Тип: {profile.get('icon','⚙️')} {profile.get('name', sys_type)}\n"
                "Шаг 2/4: задай ID системы (латиница/цифры), например: my_greenhouse\n"
                "Или напиши 'авто' для ID по умолчанию."
            )

        if step == "id":
            if value.lower() in ("авто", "auto", "default"):
                wiz["id"] = wiz["type"]
            else:
                wiz["id"] = value.split()[0]
            wiz["step"] = "purpose"
            return (
                f"✅ ID: {wiz['id']}\n"
                "Шаг 3/4: что система должна делать?\n"
                "Пример: поддерживать климат и безопасность, управлять поливом и вентиляцией."
            )

        if step == "purpose":
            wiz["purpose"] = value
            wiz["step"] = "functions"
            profile = self.smart_profiles.get(wiz["type"], {})
            actuators = ", ".join(profile.get("actuators", []))
            return (
                f"✅ Назначение: {wiz['purpose']}\n"
                "Шаг 4/4: какие функции включить сразу?\n"
                f"Доступные функции: {actuators}\n"
                "Введи через запятую (пример: irrigation, ventilation)\n"
                "или напиши 'авто' для стандартного профиля."
            )

        if step == "functions":
            profile = self.smart_profiles.get(wiz["type"], {})
            actuators = profile.get("actuators", [])
            if value.lower() not in ("авто", "auto", "default"):
                selected = [x.strip() for x in value.split(",") if x.strip()]
                valid = [x for x in selected if x in actuators]
                wiz["functions"] = valid
            else:
                wiz["functions"] = []

            create_msg = self.smart_sys.add_system(wiz["type"], wiz["id"])
            if create_msg.startswith("❌"):
                self._smart_create_wizard = None
                return create_msg

            if wiz["functions"]:
                for function_name in wiz["functions"]:
                    self.smart_sys.command(wiz["id"], function_name, "on")

            summary = (
                f"🧾 Создано: {wiz['type']} [{wiz['id']}]\n"
                f"🎯 Назначение: {wiz['purpose']}\n"
                f"🧩 Функции: {', '.join(wiz['functions']) if wiz['functions'] else 'стандартный профиль'}"
            )
            self._smart_create_wizard = None
            return f"{create_msg}\n\n{summary}"

        self._smart_create_wizard = None
        return "⚠️ Мастер сброшен. Запусти заново: 'создай умную систему'."

    def load_skill(self, name: str):
        if self.skill_loader:
            result = self.skill_loader.load(name, core=self)
            return self.skill_loader, result
        import importlib
        try:
            return importlib.import_module(f"src.skills.{name}"), f"✅ '{name}' загружен."
        except ModuleNotFoundError:
            return None, f"❌ '{name}' не найден."

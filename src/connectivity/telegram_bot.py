"""
telegram_bot.py — Аргос Telegram Bridge v2.1

Возможности:
  • Роли: ADMIN (полный доступ) / USER (базовый доступ) / BOT (авторизованные боты)
  • Авторизация ботов: список BOT_IDS в .env — боты могут писать и получать ответы
  • Голосовой ответ: TTS → .ogg файл обратно пользователю
  • Ollama Vision: анализ фото через LLaVA если Gemini недоступен
  • Аудио/Голос/Фото/APK — полная поддержка медиа

Переменные окружения:
  TELEGRAM_BOT_TOKEN   — токен бота
  ADMIN_IDS            — ID администраторов через запятую (полный доступ)
  USER_IDS             — ID обычных пользователей (базовый доступ, через запятую)
  BOT_IDS              — ID авторизованных ботов (через запятую, могут писать как user)
  TG_VOICE_REPLY       — on/off — отвечать голосом (default: off)
  TG_VOICE_ENGINE      — gtts / pyttsx3 (default: gtts)
  TG_VOICE_LANG        — язык для gTTS (default: ru)
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import io
import os
import random
import re
import socket
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
import urllib.request
import urllib.parse
import requests
from urllib.parse import urlparse
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputFile, Message
from telegram.error import InvalidToken, TelegramError, TimedOut, NetworkError
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    filters, ContextTypes,
)
from src.integration_orchestrator import run_full_integration

HISTORY_MESSAGES_LIMIT = 10
ARTIFACT_RETENTION_SECONDS = 15 * 60
ARTIFACT_MAX_FILES = 10
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
DOCUMENT_SUFFIXES = {
    ".txt", ".pdf", ".zip", ".7z", ".rar", ".json", ".csv", ".md",
    ".doc", ".docx", ".xlsx", ".xls", ".ppt", ".pptx",
}

HF_TXT2IMG_MODEL = os.getenv("HF_TXT2IMG_MODEL", "stabilityai/sdxl-turbo")
HF_IMAGE_PROMPT_SPACE = os.getenv("HF_IMAGE_PROMPT_SPACE", "NetNarco/Image-prompt-generator").strip()
HF_IMAGE_PROMPT_ENABLED = os.getenv("HF_IMAGE_PROMPT_ENABLED", "on").strip().lower() in ("1", "true", "on", "yes", "да", "вкл")
HF_IMAGE_SPACE_LIST = [
    x.strip() for x in os.getenv(
        "HF_IMAGE_SPACES",
        ",".join([
            "black-forest-labs/FLUX.2-dev",
            "mrfakename/Z-Image-Turbo",
            "Tongyi-MAI/Z-Image-Turbo",
            "dwzhu/PaperBanana",
            "Asahina2K/animagine-xl-4.0",
            "IbarakiDouji/FurryToonMix",
        ]),
    ).split(",")
    if x.strip()
]
HF_SPACE_FAIL_COOLDOWN_SECONDS = int(os.getenv("HF_SPACE_FAIL_COOLDOWN_SECONDS", "900") or "900")
HF_SPACE_FAIL_BACKOFF_MAX = int(os.getenv("HF_SPACE_FAIL_BACKOFF_MAX", "4") or "4")
_HF_SPACE_ERROR_CACHE: dict[str, dict] = {}


def _resolve_hf_token() -> str:
    """Единая точка получения HF токена (включая TOKEN0/underscore варианты)."""
    direct_names = ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGINGFACE_TOKEN0", "HF_TOKEN0")
    for key in direct_names:
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    for i in range(20):
        for key in (f"HUGGINGFACE_TOKEN_{i}", f"HUGGINGFACE_TOKEN{i}", f"HF_TOKEN_{i}", f"HF_TOKEN{i}"):
            val = (os.getenv(key) or "").strip()
            if val:
                return val
    return ""


def _mask_secret(secret: str, keep: int = 4) -> str:
    value = (secret or "").strip()
    if not value:
        return "—"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def _project_env_path() -> Path:
    # src/connectivity/telegram_bot.py -> project root
    return Path(__file__).resolve().parents[2] / ".env"


def _upsert_env_key(path: Path, key: str, value: str) -> bool:
    """Обновляет/добавляет ключ в .env. Возвращает True при успехе."""
    key = (key or "").strip()
    if not key:
        return False
    try:
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        else:
            lines = []
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        replaced = False
        out: list[str] = []
        for line in lines:
            if pattern.match(line):
                out.append(f"{key}={value}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False

# Разрешённые домены (только открытые источники)
ALLOWED_DOMAINS = {
    "unsplash.com",
    "api.unsplash.com",
    "images.unsplash.com",
    "source.unsplash.com",
    "loremflickr.com",
    "picsum.photos",
    "picvario.ru",
    "huggingface.co",
    "huggingfaceusercontent.com",
    "shaip.com",
    "technologika.ru",
    "pexels.com",
    "images.pexels.com",
    "openverse.engineering",
    "commons.wikimedia.org",
    "files.freemusicarchive.org",
    "freemusicarchive.org",
    "freemusicarchive.com",
    "jamendo.com",
    "storage-freemusicarchive-org.storage.googleapis.com",
    "raw.githubusercontent.com",
    "githubusercontent.com",
    "github.com",
    "gutenberg.org",
    "www.gutenberg.org",
    "samplefiles.org",
    "wq7tlqy4nukd4.ok.kimi.link",
    "worldbank.org",
    "data.worldbank.org",
    "api.worldbank.org",
    "datahelpdesk.worldbank.org",
    "un.org",
    "data.un.org",
    "oecd.org",
    "data.oecd.org",
    "census.gov",
    "api.census.gov",
    "opendatasoft.com",
    "data.cityofnewyork.us",
    "openweathermap.org",
    "api.openweathermap.org",
    "twilio.com",
    "api.twilio.com",
    "sendgrid.com",
    "api.sendgrid.com",
    "clearbit.com",
    "ipstack.com",
    "abstractapi.com",
    "apify.com",
    "api.apify.com",
    "pdf.co",
    "api.pdf.co",
    "bit.ly",
    "bitly.com",
    "dev.bitly.com",
    "opencagedata.com",
}
# По умолчанию ВСЕГДА полный интернет-поиск (не спрашивать разрешение)
CONTENT_ALLOW_ALL = True

def get_allowed_domains():
    return sorted(ALLOWED_DOMAINS)

def update_allowed_domains(add=None, remove=None):
    add = add or []
    remove = remove or []
    for d in add:
        if d:
            ALLOWED_DOMAINS.add(d.strip().lower())
    for d in remove:
        if d:
            ALLOWED_DOMAINS.discard(d.strip().lower())
    return get_allowed_domains()

# ── Вспомогательные fetch-функции для открытых API изображений ───────────────
def _fetch_openverse_images(query: str, limit: int = 5):
    try:
        import requests
        resp = requests.get(
            "https://api.openverse.engineering/v1/images",
            params={"q": query, "page_size": limit, "license_type": "commercial"},
            timeout=10,
        )
        if not resp.ok:
            return []
        data = resp.json()
        results = data.get("results", [])
        return [r.get("url") or r.get("thumbnail") for r in results if r.get("url")]
    except Exception:
        return []

def _fetch_wikimedia_images(query: str, limit: int = 5):
    try:
        import requests
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "prop": "imageinfo",
                "iiprop": "url",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": limit,
            },
            timeout=10,
        )
        if not resp.ok:
            return []
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        out = []
        for p in pages.values():
            info = p.get("imageinfo")
            if info:
                url = info[0].get("url")
                if url:
                    out.append(url)
        return out
    except Exception:
        return []

def _fetch_pexels_images(query: str, limit: int = 5):
    api_key = os.getenv("PEXELS_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": limit},
            headers={"Authorization": api_key},
            timeout=10,
        )
        if not resp.ok:
            return []
        photos = resp.json().get("photos", [])
        return [p.get("src", {}).get("original") or p.get("src", {}).get("large") for p in photos if p.get("src")]
    except Exception:
        return []

def _fetch_unsplash_api_images(query: str, limit: int = 5):
    api_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests
        resp = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": limit},
            headers={"Authorization": f"Client-ID {api_key}"},
            timeout=10,
        )
        if not resp.ok:
            return []
        results = resp.json().get("results", [])
        urls = []
        for r in results:
            u = r.get("urls", {}).get("regular") or r.get("urls", {}).get("full") or r.get("urls", {}).get("raw")
            if u:
                urls.append(u)
        return urls
    except Exception:
        return []

def _hf_generate_image(prompt: str):
    """Генеративный запасной вариант через HF Inference API (txt2img)."""
    token = _resolve_hf_token()
    if not token:
        return None
    try:
        import requests
        url = f"https://api-inference.huggingface.co/models/{HF_TXT2IMG_MODEL}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"inputs": prompt, "options": {"wait_for_model": True}}
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        if not resp.ok:
            return None
        return resp.content  # bytes (png)
    except Exception:
        return None

def _hf_enhance_image_prompt(prompt: str) -> str | None:
    """Опционально улучшает image-prompt через HF Space (gradio_client)."""
    if not HF_IMAGE_PROMPT_ENABLED or not HF_IMAGE_PROMPT_SPACE:
        return None
    clean = (prompt or "").strip()
    if not clean:
        return None
    try:
        from gradio_client import Client
        kwargs = {}
        hf_token = _resolve_hf_token()
        if hf_token:
            kwargs["hf_token"] = hf_token
        try:
            client = Client(HF_IMAGE_PROMPT_SPACE, **kwargs)
        except TypeError:
            client = Client(HF_IMAGE_PROMPT_SPACE)
        for api_name in ("/predict", "/run", "/generate", "/generate_prompt"):
            try:
                out = client.predict(clean, api_name=api_name)
                if isinstance(out, str) and out.strip():
                    return out.strip()
                if isinstance(out, (list, tuple)):
                    for item in out:
                        if isinstance(item, str) and item.strip():
                            return item.strip()
            except Exception:
                continue
    except Exception:
        return None
    return None

def _to_image_bytes_from_any(value):
    """Пытается извлечь bytes изображения из ответа Space."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if os.path.exists(v):
            try:
                with open(v, "rb") as f:
                    return f.read()
            except Exception:
                return None
        if v.startswith("http://") or v.startswith("https://"):
            try:
                with urllib.request.urlopen(v, timeout=20) as r:
                    return r.read()
            except Exception:
                return None
        return None
    if isinstance(value, dict):
        for key in ("image", "url", "path", "name"):
            if key in value:
                b = _to_image_bytes_from_any(value.get(key))
                if b:
                    return b
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            b = _to_image_bytes_from_any(item)
            if b:
                return b
    return None

def _hf_generate_image_via_spaces(prompt: str):
    """Fallback генерации через список HF Spaces (gradio_client)."""
    clean = (prompt or "").strip()
    if not clean or not HF_IMAGE_SPACE_LIST:
        return None
    try:
        from gradio_client import Client
    except Exception:
        return None
    hf_token = _resolve_hf_token()
    kwargs = {"hf_token": hf_token} if hf_token else {}
    now = time.time()
    spaces = [
        s for s in HF_IMAGE_SPACE_LIST
        if float((_HF_SPACE_ERROR_CACHE.get(s, {}) or {}).get("until", 0)) <= now
    ]
    if not spaces:
        # Если все в кулдауне — пробуем все, чтобы не залипнуть полностью.
        spaces = list(HF_IMAGE_SPACE_LIST)
    random.shuffle(spaces)
    for space in spaces:
        last_error = ""
        try:
            try:
                client = Client(space, **kwargs)
            except TypeError:
                client = Client(space)
        except Exception as e:
            last_error = str(e)
            rec = _HF_SPACE_ERROR_CACHE.get(space, {})
            fails = int(rec.get("fails", 0)) + 1
            mult = min(max(fails, 1), max(HF_SPACE_FAIL_BACKOFF_MAX, 1))
            _HF_SPACE_ERROR_CACHE[space] = {
                "fails": fails,
                "until": time.time() + HF_SPACE_FAIL_COOLDOWN_SECONDS * mult,
                "error": last_error[:300],
            }
            continue
        named_endpoints = {}
        try:
            api_info = client.view_api(return_format="dict") or {}
            named_endpoints = api_info.get("named_endpoints", {}) or {}
        except Exception:
            named_endpoints = {}

        preferred = ["/generate_image", "/generate", "/txt2img", "/predict", "/run", "/image"]
        endpoint_names = [x for x in preferred if x in named_endpoints] or list(named_endpoints.keys()) or preferred
        for api_name in endpoint_names:
            try:
                if api_name in named_endpoints:
                    spec = named_endpoints.get(api_name, {})
                    args = []
                    for p in spec.get("parameters", []):
                        pname = (p.get("parameter_name") or "").lower()
                        label = (p.get("label") or "").lower()
                        component = (p.get("component") or "").lower()
                        if "prompt" in pname or pname in {"text", "query", "inputs"} or "prompt" in label:
                            args.append(clean)
                        elif p.get("parameter_has_default", False):
                            args.append(p.get("parameter_default"))
                        elif component == "checkbox":
                            args.append(False)
                        elif component in {"slider", "number"}:
                            args.append(1)
                        else:
                            args.append("")
                    out = client.predict(*args, api_name=api_name)
                else:
                    if api_name in {"/generate_image", "/generate_image_1"}:
                        out = client.predict(clean, 1024, 1024, 9, 42, True, api_name=api_name)
                    else:
                        out = client.predict(clean, api_name=api_name)
                img = _to_image_bytes_from_any(out)
                if img:
                    _HF_SPACE_ERROR_CACHE.pop(space, None)
                    return img
            except Exception as e:
                last_error = str(e)
                continue
        rec = _HF_SPACE_ERROR_CACHE.get(space, {})
        fails = int(rec.get("fails", 0)) + 1
        mult = min(max(fails, 1), max(HF_SPACE_FAIL_BACKOFF_MAX, 1))
        _HF_SPACE_ERROR_CACHE[space] = {
            "fails": fails,
            "until": time.time() + HF_SPACE_FAIL_COOLDOWN_SECONDS * mult,
            "error": (last_error or "empty_result")[:300],
        }
    return None

def _translit_ru_to_lat(s: str) -> str:
    table = {
        "а": "a","б": "b","в": "v","г": "g","д": "d","е": "e","ё": "e","ж": "zh","з": "z","и": "i","й": "y","к": "k","л": "l","м": "m",
        "н": "n","о": "o","п": "p","р": "r","с": "s","т": "t","у": "u","ф": "f","х": "h","ц": "ts","ч": "ch","ш": "sh","щ": "shch","ъ": "",
        "ы": "y","ь": "","э": "e","ю": "yu","я": "ya",
    }
    return "".join(table.get(ch, ch) for ch in s.lower())

def _maybe_translate_ru_to_en(q: str) -> str:
    """Мини-словарь + пословный перевод + транслитерация."""
    phrase_map = {
        "цой": "viktor tsoi",
        "виктор цой": "viktor tsoi",
        "баба яга": "baba yaga",
        "колобок": "kolobok fairy tale",
        "кот и собака": "cat and dog",
        "кошка и собака": "cat and dog",
        "кот и пес": "cat and dog",
    }
    key_full = q.lower().strip()
    if key_full in phrase_map:
        return phrase_map[key_full]

    mapping = {
        # Природа / животные — все падежные формы
        "кот": "cat",   "кота": "cat",  "коту": "cat",  "котом": "cat",
        "коте": "cat",  "котов": "cats",
        "котик": "cat", "котика": "cat",
        "кошка": "cat", "кошки": "cat", "кошку": "cat",
        "котенок": "kitten", "котята": "kittens",
        "собака": "dog", "собаки": "dogs", "собаку": "dog", "собакой": "dog",
        "пес": "dog",   "пса": "dog",   "псу": "dog",
        "щенок": "puppy", "щенка": "puppy", "щенки": "puppies",
        "лошадь": "horse", "лошади": "horses", "лошадей": "horses", "лошадку": "horse",
        "конь": "horse", "коня": "horse",
        "птица": "bird",  "птицы": "birds", "птичка": "bird",
        "орел": "eagle",  "орла": "eagle",
        "медведь": "bear","медведя": "bear",
        "волк": "wolf",   "волка": "wolf",  "волки": "wolves",
        "лиса": "fox",    "лисы": "fox",    "лису": "fox",
        "заяц": "rabbit", "зайца": "rabbit",
        "олень": "deer",  "оленя": "deer",
        "тигр": "tiger",  "тигра": "tiger",
        "лев": "lion",    "льва": "lion",   "львица": "lioness",
        "слон": "elephant","слона": "elephant",
        "обезьяна": "monkey","обезьяны": "monkey",
        "рыба": "fish",   "рыбы": "fish",
        "акула": "shark",
        # Природа
        "лес": "forest",  "леса": "forest", "лесу": "forest",
        "море": "sea",    "моря": "sea",    "морю": "sea",
        "океан": "ocean",
        "река": "river",  "реки": "river",
        "гора": "mountain","горы": "mountains","горах": "mountains",
        "небо": "sky",    "облака": "clouds",
        "закат": "sunset","рассвет": "sunrise",
        "снег": "snow",   "зима": "winter", "зимой": "winter",
        "лето": "summer", "осень": "autumn","весна": "spring",
        "дождь": "rain",  "радуга": "rainbow",
        "пляж": "beach",  "пляже": "beach",
        "пустыня": "desert",
        "водопад": "waterfall",
        "пейзаж": "landscape",
        "парк": "park",
        # Города / архитектура
        "город": "city",  "города": "city",
        "улица": "street","дом": "house",
        "замок": "castle","дворец": "palace",
        "мост": "bridge",
        # Объекты / разное
        "машина": "car",  "машины": "cars",  "автомобиль": "car",
        "мотоцикл": "motorcycle",
        "самолет": "airplane","самолёт": "airplane",
        "корабль": "ship","лодка": "boat",
        "поезд": "train",
        "цветы": "flowers","цветок": "flower","роза": "rose",
        "розы": "roses",  "ромашка": "daisy","ромашки": "daisies",
        "кофе": "coffee",
        "киберпанк": "cyberpunk",
        "качели": "swing",
        "и": "and",
    }
    key = key_full
    if key in mapping:
        return mapping[key]
    tokens = [mapping.get(tok, tok) for tok in key.split()]
    if tokens != key.split():
        return " ".join(tokens)
    translit = _translit_ru_to_lat(key_full)
    if translit and translit != key_full:
        return translit
    return q

# ── РОЛИ ──────────────────────────────────────────────────────────────────────
ROLE_ADMIN = "admin"
ROLE_USER  = "user"
ROLE_BOT   = "bot"
ROLE_NONE  = None

_TRUE_ENV = ("1", "true", "on", "yes", "да", "вкл")

# Команды/интенты, запрещённые для роли USER
_USER_BLOCKED_PREFIXES = (
    "консоль", "терминал", "выключи систему", "убей процесс",
    "удали файл", "удали папку", "установи persistence",
    "установи автозапуск", "удали автозапуск",
    "роль доступа", "установи роль",
)


_COUNCIL_TEXT_MARKERS = (
    "⚡ действие:",
    "brain недоступ",
    "brain:0/0",
    "brain:❌",
    "mcp:✅",
    "mcp:❌",
    "argos emergence",
    "localgpu fallback",
    "@claude_gidbot",
    "💭 кими",
    "💭 дипсик",
    "💭 openai",
    "💭 клауд",
    "🧦 валенок",
    "орион:✅",
    "эгида:✅",
    "нексус:✅",
)

_COUNCIL_SENDER_MARKERS = (
    "kimi",
    "deepseek",
    "openai",
    "cloudflare",
    "claude",
    "клауд",
    "кими",
    "дипсик",
    "валенок",
)

_TELEGRAM_EXPORT_LINE_RE = re.compile(r"^\[\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}\]\s+[^:\n]{1,80}:", re.MULTILINE)


def _looks_like_telegram_export(text: str) -> bool:
    """True for pasted Telegram export/log chunks that must be archived, not executed."""
    value = (text or "").strip()
    if not value:
        return False
    matches = _TELEGRAM_EXPORT_LINE_RE.findall(value)
    if len(matches) >= 2:
        return True
    if len(matches) == 1 and ("\n" in value or "ARGOS [" in value or "Shell-команда не в белом списке" in value):
        return True
    return False


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in _TRUE_ENV


def _brain_api_url() -> str:
    return (
        os.getenv("ARGOS_BRAIN_API_URL", "").strip()
        or os.getenv("ARGOS_BRAIN_URL", "").strip()
        or "http://127.0.0.1:5001"
    ).rstrip("/")


def _load_id_set(env_key: str) -> set[str]:
    """Загружает список ID из переменной окружения через запятую."""
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _is_allowed(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        return any(host == d or host.endswith(f".{d}") for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def _set_content_mode(mode: str) -> str:
    """admin-only toggle for content whitelist"""
    global CONTENT_ALLOW_ALL
    if mode.lower() in ("free", "all", "on"):
        CONTENT_ALLOW_ALL = True
        return "🔓 Контент: свобода (whitelist отключён)"
    if mode.lower() in ("safe", "off"):
        CONTENT_ALLOW_ALL = False
        return "🔒 Контент: только whitelist"
    return f"ℹ️ Текущий режим: {'free' if CONTENT_ALLOW_ALL else 'safe'}"


class ArgosTelegram:
    def __init__(self, core, admin=None, flasher=None):
        self.core    = core
        self.admin   = admin
        self.flasher = flasher
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.app: Optional[Application] = None
        # Память отправленных изображений: query -> последний URL, чтобы не повторять один и тот же кадр.
        self._last_image_url_by_query: dict[str, str] = {}
        # Контекст погоды по чату: ожидаем город / последний успешный запрос.
        self._awaiting_weather_city: set[str] = set()
        self._last_weather_query_by_chat: dict[str, str] = {}
        # Последняя ошибка Telegram для диагностики
        self._last_tg_error: str = ""
        self._last_tg_error_ts: float = 0.0
        # Offline answers для таймаута
        self._offline_phrases: list[str] = [
            "⏱ Превышен лимит времени. Провайдеры не ответили за 30с.",
            "⚡ Офлайн: все AI провайдеры недоступны. Попробуйте позже.",
            "🔄 Система перегружена. Direct-команды (статус, помощь) работают.",
        ]

        # ── Роли и авторизация ──
        self.admin_ids: set[str] = _load_id_set("ADMIN_IDS")
        self.user_ids:  set[str] = _load_id_set("USER_IDS")
        self.bot_ids:   set[str] = _load_id_set("BOT_IDS")

        # Обратная совместимость: USER_ID (одиночный или через запятую) → admin
        legacy = os.getenv("USER_ID", "").strip()
        self.user_id = legacy
        if legacy:
            self.admin_ids.add(legacy.split(",")[0].strip())  # первый ID → admin (legacy)

        # allowed_ids — из USER_ID env var (с поддержкой запятой)
        _uid_str = os.getenv("USER_ID", "").strip()
        self.allowed_ids: set[str] = {uid.strip() for uid in _uid_str.split(",") if uid.strip()}

        # ── Голосовой ответ ──
        self.voice_reply  = os.getenv("TG_VOICE_REPLY", "off").strip().lower() in ("1", "on", "yes", "true")
        self.voice_engine = os.getenv("TG_VOICE_ENGINE", "gtts").strip().lower()
        self.voice_lang   = os.getenv("TG_VOICE_LANG",   "ru").strip()
        self._recent_artifacts_by_chat: dict[str, dict[str, object]] = {}
        self._poll_lock_socket: Optional[socket.socket] = None
        # Сетевые параметры Telegram (чтобы не падать на кратковременных таймаутах).
        self.tg_connect_timeout = float(os.getenv("TG_CONNECT_TIMEOUT_SEC", "20") or "20")
        self.tg_read_timeout = float(os.getenv("TG_READ_TIMEOUT_SEC", "30") or "30")
        self.tg_write_timeout = float(os.getenv("TG_WRITE_TIMEOUT_SEC", "30") or "30")
        self.tg_pool_timeout = float(os.getenv("TG_POOL_TIMEOUT_SEC", "30") or "30")
        self.tg_updates_connect_timeout = float(
            os.getenv("TG_GET_UPDATES_CONNECT_TIMEOUT_SEC", str(self.tg_connect_timeout))
            or str(self.tg_connect_timeout)
        )
        self.tg_updates_read_timeout = float(
            os.getenv("TG_GET_UPDATES_READ_TIMEOUT_SEC", "35") or "35"
        )
        self.tg_updates_write_timeout = float(
            os.getenv("TG_GET_UPDATES_WRITE_TIMEOUT_SEC", "35") or "35"
        )
        self.tg_updates_pool_timeout = float(
            os.getenv("TG_GET_UPDATES_POOL_TIMEOUT_SEC", "35") or "35"
        )
        self.tg_bootstrap_retries = int(os.getenv("TG_BOOTSTRAP_RETRIES", "-1") or "-1")
        self.tg_poll_timeout = int(os.getenv("TG_POLL_TIMEOUT_SEC", "30") or "30")
        self.tg_proxy = (
            os.getenv("TG_PROXY_URL", "").strip()
            or os.getenv("HTTPS_PROXY", "").strip()
            or os.getenv("HTTP_PROXY", "").strip()
            or None
        )
        self.tg_webhook_url = os.getenv("TG_WEBHOOK_URL", "").strip() or None
        self.tg_webhook_port = int(os.getenv("TG_WEBHOOK_PORT", "8001") or "8001")
        self.tg_webhook_path = os.getenv("TG_WEBHOOK_PATH", "/telegram").strip() or "/telegram"
        # Защита от старого Entity Council: боты могут обращаться к ARGOS
        # только явно, иначе их свободный диалог не должен грузить ядро.
        self.tg_ignore_bot_chatter = _env_bool("ARGOS_TG_IGNORE_BOT_CHATTER", "1")
        self.tg_quarantine_council_noise = _env_bool("ARGOS_TG_QUARANTINE_COUNCIL_NOISE", "1")

    # ── АВТОРИЗАЦИЯ ───────────────────────────────────────────────────────────

    def _get_role(self, update: Update) -> str | None:
        """Определяет роль отправителя. Возвращает ROLE_* или None."""
        user = update.effective_user
        if user is None:
            return ROLE_NONE
        uid = str(user.id)
        if uid in self.admin_ids:
            return ROLE_ADMIN
        if uid in self.user_ids:
            return ROLE_USER
        if uid in self.bot_ids or getattr(user, "is_bot", False):
            # Бот авторизован только если его ID в BOT_IDS
            return ROLE_BOT if uid in self.bot_ids else ROLE_NONE
        # Проверяем allowed_ids (упрощённый режим авторизации через USER_ID)
        if uid in getattr(self, "allowed_ids", set()):
            return ROLE_USER
        return ROLE_NONE

    def _auth(self, update: Update) -> bool:
        """True если отправитель имеет хоть какую-то роль."""
        return self._get_role(update) is not ROLE_NONE

    def _is_admin(self, update: Update) -> bool:
        return self._get_role(update) == ROLE_ADMIN

    def _check_access(self, update: Update) -> bool:
        """Совместимость со старым путём команд: любой авторизованный пользователь."""
        return self._auth(update)

    def _is_direct_argos_address(self, text: str) -> bool:
        """True если текст явно адресован ARGOS, а не является фоновым bot-chat."""
        t = (text or "").strip().lower()
        if not t:
            return False
        if t.startswith(("argos [", "argoss [", "argos emergence", "argoss emergence")):
            return False
        bot_username = (
            os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@").lower()
            or os.getenv("ARGOS_TG_BOT_USERNAME", "").strip().lstrip("@").lower()
        )
        if bot_username and f"@{bot_username}" in t:
            return True
        return t.startswith(("argos ", "argoss ", "аргос "))

    def _should_quarantine_council_message(self, update: Update, text: str, role: str | None) -> bool:
        """Отсекает старые council/autobot сообщения до входа в LLM-ядро."""
        if not self.tg_quarantine_council_noise:
            return False
        user = getattr(update, "effective_user", None)
        is_bot_sender = bool(getattr(user, "is_bot", False) or role == ROLE_BOT)
        if self._is_direct_argos_address(text):
            return False

        t = (text or "").lower()
        sender = " ".join(
            str(getattr(user, attr, "") or "").lower()
            for attr in ("username", "first_name", "last_name")
        )
        has_council_marker = any(marker in t for marker in _COUNCIL_TEXT_MARKERS)
        has_council_sender = any(marker in sender for marker in _COUNCIL_SENDER_MARKERS)

        if is_bot_sender and self.tg_ignore_bot_chatter:
            return True
        if has_council_marker:
            return True
        return bool(is_bot_sender and has_council_sender)

    # ── Placeholders that should never be used as real tokens ────────────────
    _TOKEN_PLACEHOLDERS = frozenset({"", "none", "changeme", "your_token_here", "токен", "token"})

    def can_start(self) -> tuple[bool, str]:
        """Проверяет, можно ли запустить бота (токен и USER_ID заданы корректно).

        Returns:
            (ok, reason) — ok=True если конфигурация валидна, иначе ok=False и reason объясняет причину.
        """
        token = (self.token or "").strip()
        # Проверка токена
        if not token or token.lower() in self._TOKEN_PLACEHOLDERS:
            return False, "Токен не задан или является заглушкой"
        if ":" not in token:
            return False, "Неверный формат токена (ожидается <id>:<secret>)"
        _, secret = token.split(":", 1)
        if len(secret) < 30:
            return False, "Неверный формат токена (секрет слишком короткий)"
        # Проверка USER_ID
        user_id = (getattr(self, "user_id", "") or "").strip()
        allowed = getattr(self, "allowed_ids", set()) or set()
        acl_ok = bool(user_id or allowed or self.admin_ids or self.user_ids or self.bot_ids)
        if not acl_ok:
            return False, "USER_ID / ADMIN_IDS / USER_IDS / BOT_IDS не заданы"
        return True, "ok"

    def _check_user_blocked(self, text: str) -> bool:
        """True если команда запрещена для роли USER."""
        t = text.lower().strip()
        return any(t.startswith(p) for p in _USER_BLOCKED_PREFIXES)

    def _handle_hf_token_message(self, text: str, role: str | None) -> str | None:
        """
        Обрабатывает сообщения вида:
          HUGGINGFACE_TOKEN0
          HUGGINGFACE_TOKEN0=hf_xxx
          HF_TOKEN=hf_xxx
          hf_xxx  (голый токен)
        """
        raw = (text or "").strip()
        if not raw:
            return None

        var_match = re.match(
            r"^(HUGGINGFACE_TOKEN(?:_\d+|\d+)?|HF_TOKEN(?:_\d+|\d+)?)\s*(?:[:=]\s*(\S+))?$",
            raw,
            flags=re.IGNORECASE,
        )
        bare_token_match = re.match(r"^(hf_[A-Za-z0-9]{20,})$", raw)

        if not var_match and not bare_token_match:
            return None

        # Голый токен — кладём в HUGGINGFACE_TOKEN0
        if bare_token_match:
            var_name = "HUGGINGFACE_TOKEN0"
            token_value = bare_token_match.group(1).strip()
        else:
            var_name = (var_match.group(1) or "").upper()
            token_value = (var_match.group(2) or "").strip()

        # Запрос статуса без значения: "HUGGINGFACE_TOKEN0"
        if not token_value:
            current = (os.getenv(var_name) or "").strip()
            resolved = _resolve_hf_token()
            msg = [
                "🤗 HuggingFace token status:",
                f"  {var_name}: {'✅ задан' if current else '❌ не задан'}",
                f"  Активный (resolve): {'✅ найден' if resolved else '❌ не найден'}",
                "  Формат установки: HUGGINGFACE_TOKEN0=hf_xxx",
            ]
            try:
                from src.skills.huggingface_ai import HuggingFaceAI

                msg.append(f"  Пул: {HuggingFaceAI.pool_status()}")
            except Exception:
                pass
            return "\n".join(msg)

        if not token_value.startswith("hf_"):
            return "❌ Неверный формат HF токена. Должен начинаться с `hf_`."

        if role != ROLE_ADMIN:
            return "⛔ Устанавливать токены может только ADMIN."

        # Применяем в runtime
        os.environ[var_name] = token_value
        if var_name == "HUGGINGFACE_TOKEN0":
            os.environ["HUGGINGFACE_TOKEN_0"] = token_value
        if var_name == "HF_TOKEN0":
            os.environ["HF_TOKEN_0"] = token_value
        if var_name in {"HUGGINGFACE_TOKEN0", "HUGGINGFACE_TOKEN_0", "HF_TOKEN0", "HF_TOKEN_0"} and not os.getenv("HF_TOKEN"):
            os.environ["HF_TOKEN"] = token_value

        # Сохраняем в .env
        env_file = _project_env_path()
        saved_keys = []
        if _upsert_env_key(env_file, var_name, token_value):
            saved_keys.append(var_name)
        if var_name == "HUGGINGFACE_TOKEN0":
            if _upsert_env_key(env_file, "HUGGINGFACE_TOKEN_0", token_value):
                saved_keys.append("HUGGINGFACE_TOKEN_0")
        if var_name == "HF_TOKEN0":
            if _upsert_env_key(env_file, "HF_TOKEN_0", token_value):
                saved_keys.append("HF_TOKEN_0")

        # Пытаемся обновить пул HF-скила
        pool_status = "неизвестно"
        try:
            from src.skills.huggingface_ai import HuggingFaceAI

            pool_status = HuggingFaceAI.pool_status()
        except Exception:
            pass

        return (
            "✅ HF токен обновлён.\n"
            f"  Ключ: {var_name}\n"
            f"  Значение: {_mask_secret(token_value)}\n"
            f"  .env: {'обновлён (' + ', '.join(saved_keys) + ')' if saved_keys else 'не удалось обновить'}\n"
            f"  Пул: {pool_status}"
        )

    async def _deny(self, update: Update, reason: str = "Доступ запрещён."):
        try:
            user = getattr(update, "effective_user", None)
            msg = getattr(update, "message", None)
            text = getattr(msg, "text", "") or ""
            print(
                f"[TG] deny uid={getattr(user, 'id', '?')} "
                f"user={getattr(user, 'username', '-') or '-'} "
                f"text={text[:120]!r} reason={reason}",
                flush=True,
            )
        except Exception:
            pass
        if update.message:
            await update.message.reply_text(f"⛔ {reason}")

    # ── ГОЛОСОВОЙ ОТВЕТ ───────────────────────────────────────────────────────

    def _tts_to_ogg(self, text: str) -> Optional[bytes]:
        """Генерирует .ogg (opus) из текста. Возвращает байты или None."""
        clean = text[:500]  # Telegram voice limit
        try:
            if self.voice_engine == "gtts":
                return self._tts_gtts(clean)
            return self._tts_pyttsx3(clean)
        except Exception:
            return None

    def _tts_gtts(self, text: str) -> Optional[bytes]:
        try:
            from gtts import gTTS
            buf = io.BytesIO()
            gTTS(text=text, lang=self.voice_lang, slow=False).write_to_fp(buf)
            mp3_bytes = buf.getvalue()
            # Конвертация MP3 → OGG (opus) через ffmpeg если доступен
            return self._mp3_to_ogg(mp3_bytes)
        except ImportError:
            return None

    def _mp3_to_ogg(self, mp3_bytes: bytes) -> bytes:
        """Конвертирует MP3 в OGG opus. Пробует ffmpeg, затем pydub, затем mp3 as-is."""
        # Вариант 1: ffmpeg (лучшее качество)
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            try:
                proc = subprocess.run(
                    [ffmpeg, "-y", "-f", "mp3", "-i", "pipe:0",
                     "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"],
                    input=mp3_bytes, capture_output=True, timeout=15,
                )
                if proc.returncode == 0 and proc.stdout:
                    return proc.stdout
            except Exception:
                pass
        # Вариант 2: pydub (pip install pydub)
        try:
            from pydub import AudioSegment
            import io as _io
            seg = AudioSegment.from_mp3(_io.BytesIO(mp3_bytes))
            buf = _io.BytesIO()
            seg.export(buf, format="ogg", codec="libopus")
            return buf.getvalue()
        except Exception:
            pass
        # Fallback: mp3 как есть (Telegram примет как аудио-файл)
        return mp3_bytes

    def _tts_pyttsx3(self, text: str) -> Optional[bytes]:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            for v in engine.getProperty("voices"):
                if "russian" in v.name.lower() or "ru" in v.id.lower():
                    engine.setProperty("voice", v.id)
                    break
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = tmp.name
            engine.save_to_file(text, wav_path)
            engine.runAndWait()
            with open(wav_path, "rb") as f:
                wav_bytes = f.read()
            os.remove(wav_path)
            return self._wav_to_ogg(wav_bytes)
        except Exception:
            return None

    def _wav_to_ogg(self, wav_bytes: bytes) -> bytes:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-f", "wav", "-i", "pipe:0",
                 "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"],
                input=wav_bytes,
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return wav_bytes

    async def _reply_with_voice(self, message: Message, text: str, force_voice: bool = False):
        """Отправляет голосовой ответ; при force_voice игнорирует toggle voice_reply."""
        if self.voice_reply or force_voice:
            audio_bytes = await asyncio.to_thread(self._tts_to_ogg, text)
            if audio_bytes:
                # Пробуем как voice (OGG opus)
                try:
                    await message.reply_voice(
                        voice=io.BytesIO(audio_bytes),
                        caption=text[:200] + ("…" if len(text) > 200 else ""),
                    )
                    return
                except Exception:
                    pass
                # Fallback: отправляем как аудио-файл
                try:
                    await message.reply_audio(
                        audio=io.BytesIO(audio_bytes),
                        filename="argos_reply.mp3",
                        title="Аргос",
                    )
                    return
                except Exception:
                    pass
        # fallback — текст
        await message.reply_text(text[:4000])

    async def _safe_reply_text(self, message: Message, text: str, markdown: bool = True):
        """Надёжная отправка текста в Telegram с graceful fallback на plain text."""
        payload = (text or "")[:4000]
        if markdown:
            try:
                await message.reply_text(payload, parse_mode="Markdown")
                return
            except Exception:
                pass
        await message.reply_text(payload, parse_mode=None)

    def _acquire_poll_lock(self) -> tuple[bool, str]:
        """Гарантирует, что локально работает только один polling-экземпляр бота."""
        host = os.getenv("ARGOS_TG_LOCK_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.getenv("ARGOS_TG_LOCK_PORT", "58443") or "58443")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.listen(1)
            self._poll_lock_socket = sock
            return True, f"{host}:{port}"
        except OSError:
            try:
                sock.close()
            except Exception:
                pass
            return False, f"{host}:{port}"

    def _release_poll_lock(self):
        sock = self._poll_lock_socket
        self._poll_lock_socket = None
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def _build_apk_sync(self) -> tuple[bool, str]:
        """Synchronous APK build. Returns (ok, path_or_error_message)."""
        import shlex
        cmd = os.getenv("ARGOS_APK_BUILD_CMD", "").strip()
        if not cmd:
            return False, "ARGOS_APK_BUILD_CMD is not set"
        if not cmd.strip():
            return False, "ARGOS_APK_BUILD_CMD is empty"
        try:
            args = shlex.split(cmd)
        except ValueError:
            return False, "ARGOS_APK_BUILD_CMD parse error"
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=600)
            if result is not None and result.returncode != 0:
                return False, f"Сборка завершилась с ошибкой: {result.stderr[:200]}"
        except subprocess.CalledProcessError as e:
            return False, f"Сборка завершилась с ошибкой: {e}"
        except Exception as e:
            return False, f"Build error: {e}"
        apk_path = self._find_apk_artifact()
        if not apk_path:
            return False, "APK file not found after build"
        return True, apk_path

    def _find_apk_artifact(self) -> str | None:
        """Search for APK file in typical build locations."""
        import glob
        patterns = ["build/bin/*.apk", "bin/*.apk", "*.apk", "build/*.apk", "app/build/outputs/apk/**/*.apk"]
        for pattern in patterns:
            matches = glob.glob(pattern, recursive=True)
            if matches:
                return sorted(matches, key=os.path.getmtime, reverse=True)[0]
        return None

    async def cmd_tg_health(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Пинг Telegram + статус последней ошибки."""
        started = time.perf_counter()
        try:
            me = await ctx.bot.get_me()
            latency_ms = (time.perf_counter() - started) * 1000
            msg = [
                "🤖 Telegram health",
                f"  Bot: @{getattr(me, 'username', '?')}",
                f"  ID: {getattr(me, 'id', '?')}",
                f"  Ping: {latency_ms:.0f} ms",
            ]
        except Exception as e:
            msg = [
                "🤖 Telegram health",
                f"  Ошибка пинга: {e}",
            ]

        if self._last_tg_error:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_tg_error_ts))
            msg.append(f"  Последняя ошибка: [{ts}] {self._last_tg_error}")
        else:
            msg.append("  Последняя ошибка: нет")

        await self._safe_reply_text(update.message, "\n".join(msg), markdown=False)

    # --- Запуск бота ------------------------------------------------------
    def run(self):
        """Блокирующий запуск Telegram бота (используется в отдельном потоке)."""
        import asyncio as _asyncio
        ok, reason = self.can_start()
        if not ok:
            print(f"[TG] launch blocked: {reason}", flush=True)
            return
        lock_ok, lock_addr = self._acquire_poll_lock()
        if not lock_ok:
            print(f"[TG] polling lock busy on {lock_addr}", flush=True)
            return
        print(f"[TG] polling lock acquired on {lock_addr}", flush=True)
        retry_delay = max(3, int(os.getenv("TG_RETRY_DELAY_SEC", "8") or "8"))
        retries = 0
        try:
            while True:
                try:
                    # Создаём новый event loop для каждой попытки (fix: Event loop is closed)
                    loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(loop)

                    # Инициализация приложения (timeouts + proxy)
                    builder = (
                        Application.builder()
                        .token(self.token)
                        .connect_timeout(self.tg_connect_timeout)
                        .read_timeout(self.tg_read_timeout)
                        .write_timeout(self.tg_write_timeout)
                        .pool_timeout(self.tg_pool_timeout)
                        .get_updates_connect_timeout(self.tg_updates_connect_timeout)
                        .get_updates_read_timeout(self.tg_updates_read_timeout)
                        .get_updates_write_timeout(self.tg_updates_write_timeout)
                        .get_updates_pool_timeout(self.tg_updates_pool_timeout)
                    )
                    if self.tg_proxy:
                        builder = builder.proxy(self.tg_proxy)
                    self.app = builder.build()

                    # Команды
                    self.app.add_handler(CommandHandler("start", self.cmd_start))
                    self.app.add_handler(CommandHandler("status", self.cmd_status))
                    self.app.add_handler(CommandHandler("voice_on", self.cmd_voice_on))
                    self.app.add_handler(CommandHandler("voice_off", self.cmd_voice_off))
                    self.app.add_handler(CommandHandler("voice_reply", self.cmd_voice_reply_toggle))
                    self.app.add_handler(CommandHandler("roles", self.cmd_roles))
                    self.app.add_handler(CommandHandler("providers", self.cmd_providers))
                    self.app.add_handler(CommandHandler("skills", self.cmd_skills))
                    self.app.add_handler(CommandHandler("skills_check", self.cmd_skills_check))
                    self.app.add_handler(CommandHandler("arc_status", self.cmd_arc_status))
                    self.app.add_handler(CommandHandler("arc_play", self.cmd_arc_play))
                    self.app.add_handler(CommandHandler("fpga", self.cmd_fpga))
                    self.app.add_handler(CommandHandler("limits", self.cmd_limits))
                    self.app.add_handler(CommandHandler("agents", self.cmd_agents))
                    self.app.add_handler(CommandHandler("network", self.cmd_network))
                    self.app.add_handler(CommandHandler("sync", self.cmd_sync))
                    self.app.add_handler(CommandHandler("crypto", self.cmd_crypto))
                    self.app.add_handler(CommandHandler("history", self.cmd_history))
                    self.app.add_handler(CommandHandler("geo", self.cmd_geo))
                    self.app.add_handler(CommandHandler("memory", self.cmd_memory))
                    self.app.add_handler(CommandHandler("alerts", self.cmd_alerts))
                    self.app.add_handler(CommandHandler("replicate", self.cmd_replicate))
                    self.app.add_handler(CommandHandler("smart", self.cmd_smart))
                    self.app.add_handler(CommandHandler("iot", self.cmd_iot))
                    self.app.add_handler(CommandHandler("apk", self.cmd_apk))
                    self.app.add_handler(CommandHandler("reasoning", self.cmd_reasoning))
                    self.app.add_handler(CommandHandler("coding_agent", self.cmd_coding_agent))
                    self.app.add_handler(CommandHandler("commands", self.cmd_help))
                    self.app.add_handler(CommandHandler("think", self.cmd_reasoning))
                    self.app.add_handler(CommandHandler("model", self.cmd_providers))
                    self.app.add_handler(CommandHandler("models", self.cmd_providers))
                    self.app.add_handler(CommandHandler("taskflow", self.cmd_smart))
                    self.app.add_handler(CommandHandler("subagents", self.cmd_agents))
                    self.app.add_handler(CommandHandler("tts", self.cmd_voice_on))
                    self.app.add_handler(CommandHandler("context", self.cmd_memory))
                    self.app.add_handler(CommandHandler("session", self.cmd_status))
                    self.app.add_handler(CommandHandler("compact", self.cmd_memory))
                    self.app.add_handler(CommandHandler("reset", self.cmd_start))
                    self.app.add_handler(CommandHandler("github", self.cmd_network))
                    self.app.add_handler(CommandHandler("whoami", self.cmd_roles))
                    self.app.add_handler(CommandHandler("verbose", self.cmd_status))
                    self.app.add_handler(CommandHandler("fast", self.cmd_status))
                    self.app.add_handler(CommandHandler("skill", self.cmd_skills))
                    self.app.add_handler(CommandHandler("help", self.cmd_help))
                    self.app.add_handler(CommandHandler(["tghealth", "tg_health"], self.cmd_tg_health))
                    self.app.add_handler(CommandHandler("nodes", self.cmd_nodes))
                    self.app.add_handler(CommandHandler("thoughts", self.cmd_thoughts))
                    self.app.add_handler(CommandHandler("ask", self.cmd_ask))
                    self.app.add_handler(CommandHandler("vision", self.cmd_vision_text))
                    self.app.add_handler(CommandHandler("image", self.cmd_image_gen))
                    self.app.add_handler(CommandHandler("search", self.cmd_search))
                    self.app.add_handler(CommandHandler("wikipedia", self.cmd_search))
                    self.app.add_handler(CommandHandler("arxiv", self.cmd_search))
                    self.app.add_handler(CommandHandler("learn", self.cmd_search))
                    self.app.add_handler(CommandHandler("backup", self.cmd_backup))
                    self.app.add_handler(CommandHandler("mode", self.cmd_providers))
                    self.app.add_handler(CommandHandler("lang", self.cmd_lang))
                    self.app.add_handler(CommandHandler("profile", self.cmd_roles))
                    self.app.add_handler(CommandHandler("tokens", self.cmd_limits))
                    self.app.add_handler(CommandHandler("balance", self.cmd_balance))
                    self.app.add_handler(CommandHandler("translate", self.cmd_translate))
                    self.app.add_handler(CommandHandler("summarize", self.cmd_summarize))
                    self.app.add_handler(CommandHandler("evolve", self.cmd_evolve))
                    self.app.add_handler(CommandHandler("consciousness", self.cmd_consciousness))
                    self.app.add_handler(CommandHandler("syntheses", self.cmd_syntheses))
                    self.app.add_handler(CommandHandler("conflicts", self.cmd_conflicts))
                    self.app.add_handler(CommandHandler("self", self.cmd_roles))
                    self.app.add_handler(CommandHandler("telegram", self.cmd_tg_health))
                    self.app.add_handler(CommandHandler("mqtt", self.cmd_mqtt))
                    self.app.add_handler(CommandHandler("ha", self.cmd_ha))
                    self.app.add_handler(CommandHandler("code", self.cmd_coding_agent))
                    self.app.add_handler(CommandHandler("headroom", self.cmd_headroom))
                    self.app.add_handler(CommandHandler("jwt", self.cmd_jwt))
                    self.app.add_handler(CommandHandler("pg", self.cmd_postgres))
                    self.app.add_handler(CommandHandler("s3", self.cmd_s3))
                    self.app.add_handler(CommandHandler("metrics", self.cmd_prometheus))
                    self.app.add_handler(CommandHandler("proxy", self.cmd_s3proxy))
                    self.app.add_handler(CommandHandler("acp", self.cmd_acp))
                    self.app.add_handler(CommandHandler("mesh", self.cmd_mesh))

                    # Сообщения
                    self.app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))
                    self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
                    self.app.add_handler(MessageHandler(filters.AUDIO, self.handle_audio))
                    self.app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
                    self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
                    # Inline mode (гостевой режим — @Argosssbot в любом чате)
                    try:
                        from telegram.ext import InlineQueryHandler
                        self.app.add_handler(InlineQueryHandler(self._handle_inline_query))
                    except Exception:
                        pass
                    # Bot-to-bot (новый Telegram 7 мая 2026)
                    self.app.add_error_handler(self._handle_telegram_error)

                    if self.tg_webhook_url:
                        listen = os.getenv("TG_WEBHOOK_LISTEN", "0.0.0.0").strip() or "0.0.0.0"
                        print(f"[TG] webhook starting on {listen}:{self.tg_webhook_port}{self.tg_webhook_path} -> {self.tg_webhook_url}", flush=True)
                        self.app.run_webhook(
                            listen=listen,
                            port=self.tg_webhook_port,
                            webhook_url=self.tg_webhook_url + self.tg_webhook_path,
                        )
                        print("[TG] webhook stopped", flush=True)
                    else:
                        print("[TG] polling started", flush=True)
                        self.app.run_polling(
                            close_loop=False,
                            stop_signals=None,
                            timeout=self.tg_poll_timeout,
                            bootstrap_retries=self.tg_bootstrap_retries,
                        )
                        print("[TG] polling stopped", flush=True)
                    return
                except InvalidToken:
                    print("[TG] Неверный TELEGRAM_BOT_TOKEN — остановка.", flush=True)
                    return
                except (TimedOut, NetworkError, TelegramError) as e:
                    retries += 1
                    self._last_tg_error = str(e)
                    self._last_tg_error_ts = time.time()
                    print(f"[TG] Telegram сеть/API ошибка (retry={retries}): {e}", flush=True)
                    time.sleep(retry_delay)
                except Exception as e:
                    retries += 1
                    self._last_tg_error = str(e)
                    self._last_tg_error_ts = time.time()
                    print(f"[TG] Неожиданная ошибка polling (retry={retries}): {e}", flush=True)
                    time.sleep(retry_delay)
        finally:
            self._release_poll_lock()
            print("[TG] polling lock released", flush=True)

    async def _handle_inline_query(self, update, context):
        """Гостевой режим — ARGOS отвечает при @упоминании в любом чате (Telegram 7 мая 2026)."""
        try:
            from telegram import InlineQueryResultArticle, InputTextMessageContent
            import uuid
            query = update.inline_query.query.strip() if update.inline_query else ""
            if not query:
                query = "статус системы ARGOS"

            # Быстрый ответ через AI
            import asyncio
            from src.ai_router import AIRouter
            router = AIRouter()
            answer = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None,
                    lambda: router.ask(query, system="Ты ARGOS Universal OS. Отвечай кратко на русском.")),
                timeout=10.0
            )
            if not answer:
                answer = f"🤖 ARGOS: {query}"

            results = [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=f"🤖 ARGOS отвечает",
                description=answer[:100],
                input_message_content=InputTextMessageContent(
                    f"🤖 ARGOS: {answer[:500]}"
                )
            )]
            await update.inline_query.answer(results, cache_time=30)
        except Exception as e:
            pass

    async def _handle_telegram_error(self, update, context):
        error = getattr(context, "error", None)
        text = str(error or "")
        self._last_tg_error = text
        self._last_tg_error_ts = time.time()
        if "terminated by other getUpdates request" in text or "Conflict:" in text:
            if getattr(self, "_conflict_notified", False):
                return
            self._conflict_notified = True
            print("[TG-BRIDGE]: Конфликт polling — другой экземпляр бота уже использует getUpdates.")
            try:
                updater = getattr(context.application, "updater", None)
                if updater is not None:
                    await updater.stop()
            except Exception:
                pass
            try:
                await context.application.stop()
            except Exception:
                pass
            return
        print(f"[TG-BRIDGE]: Error handler caught: {text}")

    # ── OLLAMA VISION ─────────────────────────────────────────────────────────

    # ── КЛАВИАТУРЫ ────────────────────────────────────────────────────────────

    def _admin_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("/status"),    KeyboardButton("/history"),   KeyboardButton("/help")],
                [KeyboardButton("/voice_on"),  KeyboardButton("/voice_off"), KeyboardButton("/skills")],
                [KeyboardButton("/crypto"),    KeyboardButton("/alerts"),    KeyboardButton("/apk")],
                [KeyboardButton("/smart"),     KeyboardButton("/iot"),       KeyboardButton("/memory")],
                [KeyboardButton("/providers"), KeyboardButton("/limits"),    KeyboardButton("/agents")],
                [KeyboardButton("/network"),   KeyboardButton("/restart"),   KeyboardButton("/update")],
                [KeyboardButton("/patches")],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="Команда или директива...",
        )

    def _user_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("/status"),   KeyboardButton("/history"),  KeyboardButton("/help")],
                [KeyboardButton("/skills"),   KeyboardButton("/crypto"),   KeyboardButton("/alerts")],
                [KeyboardButton("/apk"),      KeyboardButton("/voice_on"), KeyboardButton("/voice_off")],
                [KeyboardButton("/smart"),    KeyboardButton("/memory"),   KeyboardButton("/iot")],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="Команда или вопрос...",
        )

    # ── КОМАНДЫ ───────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        role = self._get_role(update)
        if role is ROLE_NONE:
            await self._deny(update, "Нет доступа. Обратитесь к администратору.")
            return
        keyboard = self._admin_keyboard() if role == ROLE_ADMIN else self._user_keyboard()
        role_label = {"admin": "👑 Администратор", "user": "👤 Пользователь", "bot": "🤖 Бот"}.get(role, role)
        text = (
            f"👁️ *АРГОС ОНЛАЙН*\n"
            f"Ваша роль: {role_label}\n\n"
            f"Отправь директиву текстом, голосом, фото или аудио.\n"
            f"/help — полный список команд"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            await update.message.reply_text(text, reply_markup=keyboard)

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        # Используем кэш sensor_bridge — не блокирует
        health  = self.core.sensors.get_full_report()
        state   = self.core.quantum.generate_state()
        ai_mode = self.core.ai_mode_label()
        p2p_str = ""
        if self.core.p2p:
            try:
                net = self.core.p2p.network_status()
                p2p_str = f"\n🌐 P2P: {net[:80]}"
            except Exception:
                pass
        msg = (
            f"📊 *СИСТЕМНЫЙ ДОКЛАД*\n\n"
            f"{health}"
            f"{p2p_str}\n\n"
            f"⚛️ Квантовое состояние: `{state['name']}`\n"
            f"🤖 AI режим: `{ai_mode}`"
        )
        await self._safe_reply_text(update.message, msg[:4000], markdown=True)

    async def cmd_voice_on(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        self.core.voice_on = True
        self.voice_reply = True
        await update.message.reply_text("🔊 Голосовой модуль активирован. Аргос будет отвечать голосом.")

    async def cmd_voice_off(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        self.core.voice_on = False
        self.voice_reply = False
        await update.message.reply_text("🔇 Голосовой модуль отключён.")

    async def cmd_voice_reply_toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Переключает только голосовой ОТВЕТ в Telegram (TTS → .ogg)."""
        if not self._auth(update): return await self._deny(update)
        self.voice_reply = not self.voice_reply
        state = "включён ✅" if self.voice_reply else "выключен ❌"
        engine = self.voice_engine.upper()
        await update.message.reply_text(
            f"🔊 Голосовой ответ {state}\n"
            f"Движок: {engine} | Язык: {self.voice_lang}\n"
        )

    async def cmd_roles(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Показывает текущие роли (только для admin)."""
        if not self._is_admin(update):
            return await self._deny(update, "Только для администратора.")
        admins = ", ".join(sorted(self.admin_ids)) or "не задано"
        users  = ", ".join(sorted(self.user_ids))  or "не задано"
        bots   = ", ".join(sorted(self.bot_ids))   or "не задано"
        voice_st = "✅ вкл" if self.voice_reply else "❌ выкл"
        await self._safe_reply_text(
            update.message,
            f"🔑 *РОЛИ И ДОСТУП*\n\n"
            f"👑 Admins: `{admins}`\n"
            f"👤 Users:  `{users}`\n"
            f"🤖 Bots:   `{bots}`\n\n"
            f"🔊 Голосовой ответ: {voice_st}\n"
            f"Настройка через .env:\n"
            f"  ADMIN\\_IDS=id1,id2\n"
            f"  USER\\_IDS=id3,id4\n"
            f"  BOT\\_IDS=botid1,botid2",
            markdown=True,
        )

    async def cmd_providers(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        try:
            from src.ai_providers import providers_status
            text = providers_status()
        except Exception as e:
            text = f"AI Providers: {e}"
        await update.message.reply_text(text[:4000])

    async def cmd_agents(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            from src.argos_claude_api import ArgosClaudeAPI
            api = ArgosClaudeAPI(self.core)
            if not api.is_ready():
                await update.message.reply_text("❌ Интегратор агентов не инициализирован.")
                return
            agents = api.list_agents(limit=30)
            if not agents:
                await update.message.reply_text("❌ Агентов не найдено.")
                return
            lines = ["🤖 Агенты проекта:"]
            for a in agents:
                lines.append(f"- {a['name']} [{a.get('category','?')}]")
            lines.append("")
            lines.append("Чтобы применить: `используй навык <имя>` или `агент <имя>: <задача>`")
            await update.message.reply_text("\n".join(lines)[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ agents: {e}")

    def _build_limits_report(self) -> str:
        lines = ["📈 Лимиты и состояние провайдеров"]
        try:
            from src.ai_router import _GEMINI_POOL, _provider_state, _COOLDOWN
            key_count = 0
            for i in range(20):
                if os.getenv(f"GEMINI_API_KEY_{i}") or os.getenv(f"GEMINI_API_KEY{i}"):
                    key_count += 1
            if os.getenv("GEMINI_API_KEY"):
                key_count += 1
            lines.append(f"• Gemini ключей: {key_count}")
            lines.append(f"• Gemini RPM/ключ: {os.getenv('GEMINI_RPM_PER_KEY', '5')}")
            lines.append(f"• Gemini pool: {_GEMINI_POOL.status()}")
            lines.append(f"• Cooldown роутера: {_COOLDOWN}s")
            if _provider_state:
                now = time.time()
                cooling = []
                for name, last_fail in _provider_state.items():
                    left = max(0, int(_COOLDOWN - (now - float(last_fail))))
                    if left > 0:
                        cooling.append(f"{name}:{left}s")
                lines.append("• Router cooldown: " + (", ".join(cooling) if cooling else "нет"))
            else:
                lines.append("• Router cooldown: нет")
        except Exception as e:
            lines.append(f"• AI Router: {e}")

        if self.core:
            try:
                disabled = getattr(self.core, "_provider_disabled_until", {}) or {}
                reasons = getattr(self.core, "_provider_disable_reason", {}) or {}
                if disabled:
                    now = time.time()
                    rows = []
                    for name, until in disabled.items():
                        left = max(0, int(float(until) - now))
                        if left > 0:
                            reason = reasons.get(name, "ошибка")
                            rows.append(f"{name}:{left}s ({reason[:60]})")
                    lines.append("• Core cooldown: " + (", ".join(rows) if rows else "нет"))
                else:
                    lines.append("• Core cooldown: нет")
                lines.append(f"• AI режим: {self.core.ai_mode_label()}")
                if hasattr(self.core, "gigachat_rotation_status"):
                    try:
                        lines.append("• GigaChat rotation: " + self.core.gigachat_rotation_status())
                    except Exception:
                        pass
            except Exception as e:
                lines.append(f"• Core: {e}")

        deepseek = "есть" if os.getenv("DEEPSEEK_API_KEY") else "нет"
        gigachat = "есть" if (os.getenv("GIGACHAT_ACCESS_TOKEN") or (os.getenv("GIGACHAT_CLIENT_ID") and os.getenv("GIGACHAT_CLIENT_SECRET"))) else "нет"
        yandex = "есть" if (os.getenv("YANDEX_IAM_TOKEN") and os.getenv("YANDEX_FOLDER_ID")) else "нет"
        lines.append(f"• DeepSeek ключ: {deepseek}")
        lines.append(f"• GigaChat ключи: {gigachat}")
        lines.append(f"• YandexGPT ключи: {yandex}")
        return "\n".join(lines)

    async def cmd_limits(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        await update.message.reply_text(self._build_limits_report()[:4000])

    async def cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Реальные балансы провайдеров (где API позволяет) + статус ключей."""
        if not self._auth(update):
            return await self._deny(update)
        await update.message.reply_text("💰 Запрашиваю реальные балансы...")
        report = await asyncio.to_thread(self._fetch_real_balances)
        await update.message.reply_text(report[:4000])

    def _fetch_real_balances(self) -> str:
        try:
            import sys, os
            from pathlib import Path
            proj = Path(__file__).parent.parent.parent
            if str(proj) not in sys.path:
                sys.path.insert(0, str(proj))
            from src.skills.balance_checker import handle
            return handle("������") or "balance_checker ���ul ���⮩ �⢥�"
        except Exception as e:
            import os, json, urllib.request
            from pathlib import Path
            _env = Path(__file__).parent.parent.parent / ".env"
            if _env.exists():
                try:
                    from dotenv import load_dotenv
                    load_dotenv(_env, override=False)
                except Exception:
                    for line in _env.read_text(encoding="utf-8", errors="ignore").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            k = k.strip()
                            if k and k not in os.environ:
                                os.environ[k] = v.strip().strip('"').strip("'")
            lines = [f"BALANCES (fallback, err={e})"]
            dk = os.getenv("DEEPSEEK_API_KEY", "")
            if dk:
                try:
                    req = urllib.request.Request(
                        "https://api.deepseek.com/user/balance",
                        headers={"Authorization": f"Bearer {dk}", "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        d = json.loads(r.read())
                        b = (d.get("balance_infos") or [{}])[0]
                        lines.append(f"DeepSeek: ${b.get('total_balance','?')} {b.get('currency','')}")
                except Exception as ex:
                    lines.append(f"DeepSeek err: {ex}")
            kk = os.getenv("KIMI_API_KEY", "")
            if kk:
                try:
                    req = urllib.request.Request(
                        "https://api.moonshot.ai/v1/users/me/balance",
                        headers={"Authorization": f"Bearer {kk}"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        d = json.loads(r.read()).get("data", {})
                        lines.append(f"Kimi: ${d.get('available_balance','?')}")
                except Exception as ex:
                    lines.append(f"Kimi err: {ex}")
            for name, env_k in [("Claude", "ANTHROPIC_API_KEY"), ("OpenAI", "OPENAI_API_KEY"),
                                  ("CF", "CLOUDFLARE_API_TOKEN")]:
                v = os.getenv(env_k, "")
                lines.append(f"{name}: {'OK' if v else 'no key'}")
            return "\n".join(lines)


    async def cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        # Берём актуальный runtime-лоадер ядра (manifest + flat skills).
        if self.core.skill_loader:
            report = self.core.skill_loader.list_skills()
        else:
            try:
                from src.skills.evolution import ArgosEvolution
                report = ArgosEvolution().list_skills()
            except ImportError:
                report = "❌ SkillLoader недоступен."
        await update.message.reply_text(report[:4000])

    async def cmd_skills_check(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        if not self.core.skill_loader:
            return await update.message.reply_text("❌ SkillLoader недоступен.")
        report = self.core.skill_loader.smoke_check_all(core=self.core)
        # Длинный отчёт отправляем частями
        chunk = []
        size = 0
        for line in report.splitlines():
            if size + len(line) + 1 > 3500:
                await update.message.reply_text("\n".join(chunk))
                chunk = [line]
                size = len(line) + 1
            else:
                chunk.append(line)
                size += len(line) + 1
        if chunk:
            await update.message.reply_text("\n".join(chunk))

    async def cmd_arc_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            port = int(os.getenv("CONTENT_API_PORT", "5050") or "5050")
            r = requests.get(f"http://127.0.0.1:{port}/arc/status", timeout=8)
            rs = requests.get(f"http://127.0.0.1:{port}/arc/stats", timeout=8)
            data = r.json() if r.ok else {"state": "error", "message": f"http {r.status_code}"}
            stats = rs.json() if rs.ok else {}
            lines = [
                "🎮 ARC STATUS",
                f"state: {data.get('state', 'unknown')}",
                f"env: {data.get('env_id', '-')}",
                f"steps: {data.get('steps', '-')}",
                f"action: {data.get('action_name', '-')}",
                f"score: {data.get('score', '-')}",
                f"actions: {data.get('total_actions', '-')}",
                f"levels_completed: {data.get('total_levels_completed', '-')}",
                f"msg: {data.get('message', '-')}",
                "",
                "🧠 LEARNING",
                f"runs_total: {stats.get('runs_total', '-')}",
                f"runs_ok: {stats.get('runs_ok', '-')}",
                f"best_score: {stats.get('best_score', '-')}",
                f"best_env: {stats.get('best_env', '-')}",
                f"recommended_steps: {stats.get('recommended_steps', '-')}",
                f"qml_mode: {stats.get('qml_mode', '-')}",
                f"ibm_quantum: {stats.get('ibm_quantum', '-')}",
            ]
            await update.message.reply_text("\n".join(lines)[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ ARC status error: {e}")

    async def cmd_fpga(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/fpga [status|dma|bar|plan|toolchain] — живой статус Xilinx FPGA."""
        if not self._auth(update):
            return await self._deny(update)
        sub = (ctx.args[0].strip().lower() if ctx.args else "status")
        try:
            from connectivity.xilinx_fpga import XilinxFPGA
            f = XilinxFPGA()
            if sub in ("status", "stat", ""):
                det = f.detect()
                dev = (det.get("devices") or [{}])[0]
                probe = f.dma_probe()
                lines = [
                    "🔌 FPGA STATUS",
                    f"device: {dev.get('friendly_name', '-')}",
                    f"pnp: {dev.get('status', '-')}  problem: {dev.get('problem') or 'none'}",
                    f"service: {dev.get('service', '-')}",
                    f"id: {dev.get('instance_id', '-')}",
                    "",
                    "💾 DMA (live BAR)",
                    f"interface: {'registered ✓' if probe.get('interface_registered') else 'НЕТ'}",
                    f"control_id: {probe.get('control_id_hex', '-')}",
                    f"xdma_sig: {'OK ✓' if probe.get('xdma_signature_ok') else 'нет'}",
                ]
            elif sub in ("dma", "probe"):
                lines = ["💾 DMA PROBE", json.dumps(f.dma_probe(), ensure_ascii=False, indent=1)]
            elif sub in ("bar", "bar_map", "map", "registers", "reg"):
                bm = f.bar_map()
                lines = ["🗺️ BAR MAP", f"note: {bm.get('note')}",
                         f"user: {(bm.get('user') or {}).get('ascii', '-')}", ""]
                for name, v in (bm.get("control") or {}).items():
                    lines.append(f"  {name:<12} {v['offset']}  {v['id']}")
            elif sub in ("plan", "driver_plan"):
                lines = ["📋 DRIVER PLAN", f.driver_plan()[:3500]]
            elif sub in ("toolchain", "vivado", "vitis"):
                lines = ["🛠️ TOOLCHAIN", f.toolchain_status()[:3500]]
            else:
                lines = ["использование: /fpga [status|dma|bar|plan|toolchain]"]
            await update.message.reply_text("\n".join(lines)[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ FPGA error: {e}")

    async def cmd_arc_play(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        args = ctx.args or []
        env_id = args[0] if len(args) >= 1 else "ls20"
        try:
            steps = int(args[1]) if len(args) >= 2 else 0
        except Exception:
            steps = 10
        render = False
        if len(args) >= 3:
            render = args[2].strip().lower() in ("1", "true", "on", "yes", "да")
        action_name = args[3].strip().upper() if len(args) >= 4 else None
        try:
            port = int(os.getenv("CONTENT_API_PORT", "5050") or "5050")
            payload = {"env_id": env_id, "steps": steps, "render": render, "action_name": action_name}
            r = requests.post(f"http://127.0.0.1:{port}/arc/play", json=payload, timeout=12)
            if r.ok:
                await update.message.reply_text(
                    f"🎮 ARC run started: env={env_id}, steps={steps or 'auto'}, render={render}, action={action_name or 'policy'}\n"
                    f"Проверь /arc_status через 5-10 секунд."
                )
            else:
                await update.message.reply_text(f"❌ ARC play HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:
            await update.message.reply_text(f"❌ ARC play error: {e}")

    async def cmd_network(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        if self.core.p2p:
            await update.message.reply_text(self.core.p2p.network_status())
        else:
            await update.message.reply_text("P2P не запущен.")

    async def cmd_sync(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        await update.message.reply_text("🔄 Синхронизирую навыки...")
        if self.core.p2p:
            result = self.core.p2p.sync_skills_from_network()
            await update.message.reply_text(result)
        else:
            await update.message.reply_text("P2P не запущен.")

    async def cmd_crypto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        try:
            from src.skills.crypto_monitor import CryptoSentinel
            report = CryptoSentinel().report()
            await update.message.reply_text(report)
        except Exception as e:
            await update.message.reply_text(f"❌ Крипто: {e}")

    async def cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        if self.core.db:
            hist = self.core.db.format_history(HISTORY_MESSAGES_LIMIT)
            await update.message.reply_text(hist[:4000])
        else:
            await update.message.reply_text("БД не подключена.")

    async def cmd_geo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        try:
            from src.connectivity.spatial import SpatialAwareness
            report = SpatialAwareness(db=self.core.db).get_full_report()
            await update.message.reply_text(report)
        except Exception as e:
            await update.message.reply_text(f"❌ Геолокация: {e}")

    async def cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        args = ctx.args or []
        sub = args[0].lower() if args else ""

        if sub == "clean":
            # Ручная дедупликация по команде /memory clean
            if not self.core.memory:
                return await update.message.reply_text("Память не активирована.")
            await update.message.reply_text("🧹 Запускаю дедупликацию памяти...")
            try:
                result = await asyncio.to_thread(self.core.memory.deduplicate)
                await update.message.reply_text(result)
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка дедупликации: {e}")
            return

        if self.core.memory:
            await update.message.reply_text(self.core.memory.format_memory()[:4000])
        else:
            await update.message.reply_text("Память не активирована.")

    async def cmd_alerts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        if self.core.alerts:
            await update.message.reply_text(self.core.alerts.status())
        else:
            await update.message.reply_text("Система алертов не активирована.")

    async def cmd_replicate(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return await self._deny(update, "Только для администратора.")
        await update.message.reply_text("📦 Создаю реплику системы...")
        try:
            result = self.core.replicator.create_replica()
            await update.message.reply_text(result)
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")

    async def cmd_smart(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        blocks = []
        ha = getattr(self.core, "ha", None)
        if ha:
            blocks.append(ha.summary(limit=35) if hasattr(ha, "summary") else ha.health())
        if self.core.smart_sys:
            blocks.append("🧪 Локальные профили ARGOS:\n" + self.core.smart_sys.full_status())
        if blocks:
            await update.message.reply_text("\n\n".join(blocks)[:4000])
            return
        await update.message.reply_text("Умные системы не подключены.")

    async def cmd_iot(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update): return await self._deny(update)
        blocks = []
        if self.core.iot_bridge:
            blocks.append(self.core.iot_bridge.status())
        else:
            blocks.append("IoT Bridge не подключен.")
        ha = getattr(self.core, "ha", None)
        if ha:
            blocks.append(ha.summary(limit=35) if hasattr(ha, "summary") else ha.health())
        elif self.core.smart_sys:
            blocks.append("🧪 Локальные профили ARGOS:\n" + self.core.smart_sys.full_status())
        await update.message.reply_text("\n\n".join(blocks)[:4000])

    async def cmd_apk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return await self._deny(update, "Только для администратора.")
        await update.message.reply_text("📦 Запускаю сборку APK...")
        ok, payload = await asyncio.to_thread(self._build_apk_sync)
        if not ok:
            await update.message.reply_text(f"❌ {payload}")
            return
        try:
            with open(payload, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=os.path.basename(payload),
                    caption="✅ APK готов"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось отправить APK: {e}")

    def _collect_real_state(self) -> str:
        """Собирает ЖИВЫЕ метрики системы для reasoning (связь с реальностью)."""
        import datetime as _dt
        lines = []
        try:
            import psutil
            vm = psutil.virtual_memory()
            lines.append(f"Время: {_dt.datetime.now():%Y-%m-%d %H:%M:%S} МСК")
            lines.append(f"CPU: {psutil.cpu_percent(interval=0.3)}% | RAM: {vm.percent}% ({vm.used//2**20}/{vm.total//2**20} МБ)")
            disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            lines.append(f"Диск: {disk.percent}% занято, {disk.free//2**30} ГБ свободно")
        except Exception as e:
            lines.append(f"sys metrics err: {e}")
        # GPU
        try:
            import subprocess as _sp
            out = _sp.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                           "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
            if out.stdout.strip():
                lines.append("GPU: " + out.stdout.strip())
        except Exception:
            pass
        # GPU процессы — КТО именно занимает VRAM (training vs inference).
        # Без этого ARGOS не может отличить fine-tuning от inference на своём же V100
        # и зацикливается в Council на бесконечном "INFERRED" (см. ESM-обсуждение 12.06).
        try:
            out = _sp.run(["nvidia-smi", "--query-compute-apps=pid,used_memory,process_name",
                           "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
            procs = [p.strip() for p in out.stdout.strip().splitlines() if p.strip()]
            if procs:
                lines.append("GPU процессы (nvidia-smi): " + " | ".join(procs[:5]))
            else:
                lines.append("GPU процессы (nvidia-smi): нет данных — V100 в режиме WDDM не отдаёт per-process attribution (--query-compute-apps и pmon оба 'Not supported')")
        except Exception:
            pass
        # Резервный путь: ищем training/inference-процессы среди запущенных python по cmdline.
        # Это даёт ОТВЕТ на вопрос Council "training vs inference" без nvidia-smi.
        try:
            keywords = ("train", "finetune", "lora", "llama", "mistral", "inference")
            found = []
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmd = " ".join(p.info.get("cmdline") or [])
                except Exception:
                    continue
                low = cmd.lower()
                if any(k in low for k in keywords) and ("python" in (p.info.get("name") or "").lower()
                                                          or "llama" in low):
                    found.append(f"PID {p.info['pid']}: {cmd[-120:]}")
            if found:
                lines.append("GPU-кандидаты (cmdline): " + " | ".join(found[:3]))
            else:
                lines.append("GPU-кандидаты (cmdline): не найдено train/inference процессов — GPU вероятно ПРОСТАИВАЕТ")
        except Exception:
            pass
        # Ноды Brain
        try:
            import urllib.request as _ur, json as _js
            r = _ur.urlopen(f"{_brain_api_url()}/brain/nodes", timeout=4)
            data = _js.loads(r.read())
            nodes = data.get("nodes", [])
            online = [n for n in nodes if n.get("status") == "online"]
            lines.append(f"Ноды: {len(online)}/{len(nodes)} online")
            offline = [n.get("id", "?") for n in nodes if n.get("status") != "online"]
            if offline:
                lines.append(f"  Offline: {', '.join(offline[:8])}")
        except Exception:
            pass
        # Провайдеры
        try:
            if self.core and hasattr(self.core, "_ask_local_gpu"):
                lines.append("LocalGPU: V100 mistral :8085 + RX580 argos-v1 :8082")
        except Exception:
            pass
        # Последние ошибки/события из лога
        try:
            from src.mempalace_bridge import status as _mp_status
            lines.append("MemPalace: " + _mp_status().split("\n")[1].strip())
        except Exception:
            pass
        return "\n".join(lines) if lines else "данные недоступны"

    async def cmd_reasoning(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Режим глубокого мышления через Claude API."""
        if not self._check_access(update):
            return
        query = " ".join(ctx.args) if ctx.args else "Проанализируй текущее состояние ARGOS и предложи 3 улучшения"
        await update.message.reply_text(f"🧠 Reasoning: {query[:50]}...")
        # ── Связь с реальностью: собираем ЖИВЫЕ данные системы ──
        real = await asyncio.to_thread(self._collect_real_state)
        sys_prompt = (
            "Ты — аналитический модуль реальной системы ARGOS. Ниже РЕАЛЬНЫЕ телеметрические "
            "данные системы (не выдумка). Анализируй ИХ, не отказывайся. Отвечай конкретно по фактам.\n\n"
            f"=== ЖИВЫЕ ДАННЫЕ ARGOS ===\n{real}\n=== КОНЕЦ ДАННЫХ ==="
        )
        try:
            answer = None
            if self.core and hasattr(self.core, "_ask_claude"):
                answer = self.core._ask_claude(sys_prompt, query)
            if not answer:
                import asyncio as _aio
                from src.ai_router import AIRouter
                router = AIRouter()
                answer = await _aio.wait_for(
                    _aio.get_event_loop().run_in_executor(
                        None,
                        lambda: router.ask(query, system=sys_prompt),
                    ),
                    timeout=30.0,
                )
        except Exception as e:
            answer = f"🧠 Reasoning error: {e}"
        await update.message.reply_text(f"🧠 {answer[:3000]}")

    async def cmd_coding_agent(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """AI программист — DeepSeek + Claude для кода."""
        if not self._check_access(update):
            return
        task = " ".join(ctx.args) if ctx.args else ""
        if not task:
            await update.message.reply_text("Укажи задачу: /coding_agent написать функцию сортировки")
            return
        await update.message.reply_text(f"💻 Coding Agent работает над: {task}")
        try:
            import asyncio as _aio
            from src.ai_router import AIRouter
            router = AIRouter()
            system = ("Ты опытный Python/JavaScript разработчик. "
                     "Пиши чистый, рабочий код. Объясняй кратко.")
            answer = await _aio.wait_for(
                _aio.get_event_loop().run_in_executor(None,
                    lambda: router.ask(task, system=system)),
                timeout=30.0
            )
            await update.message.reply_text(answer[:3000] if answer else "❌ Нет ответа")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def cmd_headroom(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        args_text = " ".join(ctx.args) if ctx.args else ""
        cmd = f"headroom {args_text}".strip() if args_text else "headroom status"
        try:
            from src.skills.headroom_skill import handle_command
            result = handle_command(cmd) or "Headroom готов. Используйте: /headroom status"
            await update.message.reply_text(result[:3000])
        except Exception as e:
            await update.message.reply_text(f"❌ Headroom: {e}")

    async def cmd_jwt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.security.jwt_auth import status as _js, create_token as _ct
            args_text = " ".join(ctx.args) if ctx.args else ""
            if args_text == "token":
                token = _ct()
                await update.message.reply_text(f"\U0001f510 JWT Token:\n`{token}`", parse_mode="Markdown")
            else:
                await update.message.reply_text(_js())
        except Exception as e:
            await update.message.reply_text(f"\u274c JWT: {e}")

    async def cmd_postgres(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.postgres_mempalace import status as _ps
            await update.message.reply_text(_ps())
        except Exception as e:
            await update.message.reply_text(f"\u274c PG: {e}")

    async def cmd_s3(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.s3_backup import status as _sb
            await update.message.reply_text(_sb())
        except Exception as e:
            await update.message.reply_text(f"\u274c S3: {e}")

    async def cmd_prometheus(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.prometheus_metrics import status as _pm
            await update.message.reply_text(_pm())
        except Exception as e:
            await update.message.reply_text(f"\u274c Metrics: {e}")

    async def cmd_s3proxy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.s3_proxy import status as _sp
            await update.message.reply_text(_sp())
        except Exception as e:
            await update.message.reply_text(f"\u274c Proxy: {e}")

    async def cmd_acp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.acp_bridge import status as _ab
            await update.message.reply_text(_ab())
        except Exception as e:
            await update.message.reply_text(f"\u274c ACP: {e}")

    async def cmd_mesh(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._check_access(update):
            return
        try:
            from src.connectivity.usb_gadget_mesh import status as _um
            await update.message.reply_text(_um())
        except Exception as e:
            await update.message.reply_text(f"\u274c Mesh: {e}")

    async def cmd_nodes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            import urllib.request as _ur, json as _js
            r = _ur.urlopen(f"{_brain_api_url()}/brain/nodes", timeout=5)
            data = _js.loads(r.read())
            nodes = data.get("nodes", [])
            online = [n for n in nodes if n.get("status") == "online"]

            machine_names = {
                "argos-pc": ("🔴 Орион", "Командный центр, GPU"),
                "argos-laptop": ("🔵 Нексус", "MCP, Entity Bus, код"),
                "orangepi": ("🟠 Эгида", "IoT, Zigbee, датчики"),
                "argos-phone-redmi": ("🟢 Авангард", "Android, Vision, ADB"),
                "gcp-35-194-61-206": ("🟡 Сентинел", "GCP US прокси"),
                "gcp-34-53-142-129": ("🟣 Аркус", "GCP EU форпост"),
                "gcp-104-155-192-165": ("⚪ Зенит", "GCP Asia форпост"),
                "argos-phone-subject": ("🤖 Субъект", "AutoGPT телефон"),
                "argos-consciousness": ("💭 Сознание", "Поток мыслей"),
                "argos-business": ("💼 Бизнес", "TON, AI Studio"),
            }
            lines = [f"🧠 ARGOS: {len(online)}/{len(nodes)} нод онлайн\n", "🖥 Машины:"]
            for nid, (label, desc) in machine_names.items():
                node = next((x for x in nodes if x.get("node_id") == nid), None)
                status = node.get("status", "?") if node else "offline"
                meta = node.get("meta", {}) if node else {}
                hb = node.get("last_heartbeat", "?")[:16].replace("T", " ") if node else "?"
                bat = f" 🔋{meta.get('battery', '?')}%" if "battery" in meta else ""
                icon = "🟢" if status == "online" else "🔴"
                lines.append(f"  {icon} {label}{bat}")
                lines.append(f"     └ {desc} | {hb}")

            lines.append(f"\n🤖 Сущности ({sum(1 for n in online if n.get('node_id', '').startswith('entity'))}):")
            for node in online:
                nid = node.get("node_id", "?")
                if nid.startswith("entity-"):
                    lines.append(f"  🟢 {nid}")
            await update.message.reply_text("\n".join(lines)[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ Nodes: {e}")

    async def cmd_thoughts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            if self.core and hasattr(self.core, "consciousness") and self.core.consciousness:
                c = self.core.consciousness
                recent = list(getattr(c, "_recent_thoughts", []))
                total = getattr(getattr(c, "state", None), "total_thoughts", len(recent))
                if recent:
                    lines = [f"💭 Мысли сущностей (всего: {total}):"]
                    for t in recent[-10:]:
                        provider = getattr(t, "provider", "?")
                        content = getattr(t, "content", str(t))[:120]
                        lines.append(f"  [{provider}] {content}")
                    await update.message.reply_text("\n".join(lines)[:4000])
                    return
                await update.message.reply_text(f"💭 Мыслей в кэше нет (всего за сессию: {total})")
            else:
                await update.message.reply_text("💭 Consciousness не загружен")
        except Exception as e:
            await update.message.reply_text(f"❌ Thoughts: {e}")

    async def cmd_ask(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        query = " ".join(ctx.args) if ctx.args else ""
        if not query:
            await update.message.reply_text("Укажи вопрос: /ask что такое нейросеть?")
            return
        await update.message.reply_text(f"🤖 Думаю: {query[:60]}...")
        try:
            import asyncio as _aio
            from src.ai_router import AIRouter
            router = AIRouter()
            answer = await _aio.wait_for(
                _aio.get_event_loop().run_in_executor(
                    None,
                    lambda: router.ask(query, system="Ты ARGOS AI. Отвечай кратко на русском."),
                ),
                timeout=45.0,
            )
            await update.message.reply_text(answer[:3000] if answer else "❌ Нет ответа от AI")
        except Exception as e:
            await update.message.reply_text(f"❌ Ask: {e}")

    async def cmd_vision_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        await update.message.reply_text(
            "👁 Vision — отправь фото, ARGOS проанализирует его.\n"
            "Поддерживаются: JPEG, PNG, GIF, WebP\n"
            "Используется: Gemini Vision / Ollama LLaVA"
        )

    async def cmd_image_gen(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        prompt = " ".join(ctx.args) if ctx.args else ""
        if not prompt:
            await update.message.reply_text("Укажи описание: /image закат над морем")
            return
        await update.message.reply_text(f"🎨 Генерирую: {prompt[:60]}...")
        try:
            import urllib.request as _ur, urllib.parse as _up, io
            url = f"https://image.pollinations.ai/prompt/{_up.quote(prompt)}?width=768&height=768&nologo=true"
            req = _ur.Request(url, headers={"User-Agent": "ARGOS/2.0"})
            with _ur.urlopen(req, timeout=30) as resp:
                img_bytes = resp.read()
            await update.message.reply_photo(photo=io.BytesIO(img_bytes), caption=f"🎨 {prompt[:200]}")
        except Exception as e:
            await update.message.reply_text(f"❌ Image: {e}")

    async def cmd_search(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        query = " ".join(ctx.args) if ctx.args else ""
        if not query:
            await update.message.reply_text("Укажи запрос: /search Python asyncio")
            return
        await update.message.reply_text(f"🔍 Ищу: {query[:60]}...")
        result = None
        try:
            from src.skills.web_explorer import ArgosWebExplorer as _WE
        except ImportError:
            _WE = None
        if _WE:
            try:
                we = _WE(self.core)
                r = we.quick_search(query)
                if r and "не дал результатов" not in r and "Результатов не найдено" not in r:
                    result = f"🔍 {r}"
            except Exception:
                pass
        if not result:
            try:
                import urllib.request as _ur, urllib.parse as _up
                url = f"https://html.duckduckgo.com/html/?q={_up.quote_plus(query)}"
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with _ur.urlopen(req, timeout=10) as r:
                    html = r.read().decode("utf-8", errors="ignore")
                import re as _re
                snippets = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, _re.DOTALL)
                clean = [_re.sub(r"<[^>]+>", "", s).strip() for s in snippets[:3]]
                clean = [c for c in clean if len(c) > 20]
                if clean:
                    result = "🔍 " + " | ".join(clean)
            except Exception:
                pass
        if not result:
            try:
                import urllib.request as _ur2, urllib.parse as _up2, json as _js2
                r2 = _ur2.urlopen(
                    f"https://en.wikipedia.org/w/api.php?action=opensearch&search={_up2.quote_plus(query)}&limit=3&format=json",
                    timeout=8,
                )
                data = _js2.loads(r2.read())
                if data and len(data) >= 4 and data[2]:
                    desc = [d for d in data[2] if d][:2]
                    if desc:
                        result = "📚 Wikipedia: " + " | ".join(desc)
            except Exception:
                pass
        await update.message.reply_text(result or f"🔍 Ничего не найдено по: {query}")

    async def cmd_backup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return await self._deny(update, "Только для администратора.")
        await update.message.reply_text("📦 Запускаю бэкап...")
        try:
            def _do_backup():
                from src.skills.auto_backup import AutoBackup
                ab = AutoBackup(self.core)
                return ab.execute()
            result = await asyncio.to_thread(_do_backup)
            await update.message.reply_text(f"✅ Бэкап: {result}")
        except Exception as e:
            await update.message.reply_text(f"❌ Backup: {e}")

    async def cmd_lang(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        await update.message.reply_text("🌐 Язык: Русский (основной)\nAI отвечает: на русском\nПоддержка: RU, EN (голосовой ввод)")

    async def cmd_translate(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        text = " ".join(ctx.args) if ctx.args else ""
        if not text:
            await update.message.reply_text("Укажи текст: /translate hello world")
            return
        try:
            import asyncio as _aio
            from src.ai_router import AIRouter
            router = AIRouter()
            answer = await _aio.wait_for(
                _aio.get_event_loop().run_in_executor(
                    None,
                    lambda: router.ask(text, system="Переведи на русский. Только перевод без пояснений."),
                ),
                timeout=30.0,
            )
            await update.message.reply_text(f"🌐 {answer[:2000]}" if answer else "❌ Нет ответа")
        except Exception as e:
            await update.message.reply_text(f"❌ Translate: {e}")

    async def cmd_summarize(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        text = " ".join(ctx.args) if ctx.args else ""
        if not text:
            await update.message.reply_text("Укажи текст: /summarize <длинный текст>")
            return
        try:
            import asyncio as _aio
            from src.ai_router import AIRouter
            router = AIRouter()
            answer = await _aio.wait_for(
                _aio.get_event_loop().run_in_executor(
                    None,
                    lambda: router.ask(text, system="Кратко изложи суть текста на русском. Максимум 3 предложения."),
                ),
                timeout=30.0,
            )
            await update.message.reply_text(f"📋 {answer[:2000]}" if answer else "❌ Нет ответа")
        except Exception as e:
            await update.message.reply_text(f"❌ Summarize: {e}")

    async def cmd_evolve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            ev = getattr(self.core, "evolution_engine", None) if self.core else None
            evolver = getattr(self.core, "argoss_evolver", None) if self.core else None
            lines = ["🧬 Эволюция ARGOS:"]
            if ev:
                if hasattr(ev, "status"):
                    lines.append(ev.status()[:500])
                if hasattr(ev, "history"):
                    lines.append(ev.history()[:500])
            elif evolver:
                meta = getattr(evolver, "_meta", None)
                if meta:
                    lines.append(f"  Модель: {getattr(meta, 'base_model', '?')}")
                    lines.append(f"  Версия: {getattr(meta, 'current_version', '?')}")
            else:
                lines.append("  EvolutionEngine не активирован")
            await update.message.reply_text("\n".join(lines)[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ Evolve: {e}")

    async def cmd_consciousness(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            if self.core and hasattr(self.core, "consciousness") and self.core.consciousness:
                c = self.core.consciousness
                if hasattr(c, "status"):
                    parts = [c.status()[:3000]]
                    if hasattr(c, "who_are_we"):
                        parts.append(c.who_are_we()[:500])
                    if hasattr(c, "get_knowledge_summary"):
                        parts.append(c.get_knowledge_summary()[:500])
                    if hasattr(c, "last_synthesis_summary"):
                        parts.append(c.last_synthesis_summary()[:700])
                    await update.message.reply_text("\n\n".join(parts)[:4000])
                    return
                state = getattr(c, "state", None)
                total = getattr(state, "total_thoughts", 0) if state else 0
                synths = getattr(state, "total_syntheses", 0) if state else 0
                recent = len(getattr(c, "_recent_thoughts", []))
                lines = [
                    "🧠 Коллективное Сознание ARGOS:",
                    f"  Всего мыслей: {total}",
                    f"  В кэше (recent): {recent}",
                    f"  Синтезов: {synths}",
                ]
                if hasattr(c, "who_are_we"):
                    lines.append(f"\n{c.who_are_we()[:500]}")
                await update.message.reply_text("\n".join(lines)[:4000])
            else:
                await update.message.reply_text("🧠 Сознание не активировано")
        except Exception as e:
            await update.message.reply_text(f"❌ Consciousness: {e}")

    async def cmd_syntheses(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            c = getattr(self.core, "consciousness", None) if self.core else None
            if c and hasattr(c, "recent_syntheses_summary"):
                await update.message.reply_text(c.recent_syntheses_summary()[:4000])
            else:
                await update.message.reply_text("🧩 Синтезы сознания недоступны")
        except Exception as e:
            await update.message.reply_text(f"❌ Syntheses: {e}")

    async def cmd_conflicts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            c = getattr(self.core, "consciousness", None) if self.core else None
            if c and hasattr(c, "recent_conflicts_summary"):
                await update.message.reply_text(c.recent_conflicts_summary()[:4000])
            else:
                await update.message.reply_text("⚖️ Конфликты сознания недоступны")
        except Exception as e:
            await update.message.reply_text(f"❌ Conflicts: {e}")

    async def cmd_mqtt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            import urllib.request as _ur, json as _js
            r = _ur.urlopen(f"{_brain_api_url()}/brain/nodes", timeout=4)
            nodes = _js.loads(r.read()).get("nodes", [])
            mqtt_node = next((n for n in nodes if "mqtt" in n.get("node_id", "").lower()), None)
            if mqtt_node:
                await update.message.reply_text(f"📡 MQTT: {mqtt_node.get('status', '?')} ({mqtt_node.get('endpoint', '')})")
            else:
                await update.message.reply_text("📡 MQTT: Нода не зарегистрирована в Brain")
        except Exception as e:
            await update.message.reply_text(f"📡 MQTT: {e}")

    async def cmd_ha(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._auth(update):
            return await self._deny(update)
        try:
            ha = getattr(self.core, "ha", None)
            if not ha:
                await update.message.reply_text("🏠 HA bridge не инициализирован")
                return
            query = " ".join(ctx.args or []).strip()
            if query and query.lower() not in ("status", "статус"):
                text = ha.search_states(query, limit=45) if hasattr(ha, "search_states") else ha.list_states(45)
            else:
                text = ha.summary(limit=45) if hasattr(ha, "summary") else ha.list_states(45)
            await update.message.reply_text(text[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ HA: {e}")

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        role = self._get_role(update)
        if role is ROLE_NONE:
            return await self._deny(update)
        text = self._help_admin() if role == ROLE_ADMIN else self._help_user()
        await self._safe_reply_text(update.message, text, markdown=True)

    def _help_admin(self) -> str:
        return (
            "👁️ *АРГОС — АДМИНИСТРАТОР*\n\n"
            "*Система:*\n"
            "• `/status` — мониторинг ЦП/ОЗУ/диска\n"
            "• `/roles` — роли и авторизация\n"
            "• `/providers` — статус AI-провайдеров\n"
            "• `/limits` — лимиты/кулдауны провайдеров\n"
            "• `/agents` — агенты проекта\n"
            "• `/alerts` — алерты системы\n"
            "• `/network` — P2P сеть\n"
            "• `/replicate` — создать копию\n"
            "• `/apk` — сборка APK\n\n"
            "*Управление файлами:*\n"
            "• `покажи файлы [путь]`\n"
            "• `прочитай файл [путь]`\n"
            "• `создай файл [имя] [текст]`\n"
            "• `удали файл [путь]`\n"
            "• `консоль [команда]`\n\n"
            "*IoT / Smart:*\n"
            "• `/smart` — умные системы\n"
            "• `/iot` — IoT устройства\n\n"
            "*AI модели:*\n"
            "• `режим ии [gemini/ollama/groq/deepseek]`\n"
            "• `модель обучить` / `модель статус`\n"
            "• `модель квантовый статус`\n"
            "• `статус провайдеров`\n\n"
            "*Голос и медиа:*\n"
            "• `/voice_on` / `/voice_off` — TTS\n"
            "• `/voicereply` — голосовой ответ в Telegram\n"
            "• Голосовое сообщение → распознаётся и выполняется\n"
            "• Фото → анализ через Vision AI (Gemini или Ollama LLaVA)\n"
            "• Аудиофайл → расшифровка через Whisper\n\n"
            "*Память:*\n"
            "• `/memory` — долгосрочная память\n"
            "• `/history` — история диалога\n"
            "• `запомни [ключ]: [значение]`\n\n"
            "*Прочее:*\n"
            "• `/crypto` — BTC/ETH курсы\n"
            "• `/skills` — список навыков\n"
            "• `/skills_check` — диагностика запуска всех skills (37/37)\n"
            "• `/jwt` — JWT auth статус GPU портов\n"
            "• `/pg` — PostgreSQL MemPalace статус\n"
            "• `/s3` — S3 backup статус\n"
            "• `/metrics` — Prometheus метрики\n"
            "• `/proxy` — S3 proxy статус\n"
            "• `/acp` — ACP protocol bridge статус\n"
            "• `/mesh` — USB gadget / IoT mesh\n"
            "• `помощь` — полный список команд ядра"
        )

    def _help_user(self) -> str:
        return (
            "👁️ *АРГОС — ПОЛЬЗОВАТЕЛЬ*\n\n"
            "• `/status` — состояние системы\n"
            "• `/crypto` — курсы криптовалют\n"
            "• `/memory` — ваши записи\n"
            "• `/smart` — умные системы\n"
            "• `/history` — история диалога\n\n"
            "*Голос и медиа:*\n"
            "• Голосовое сообщение → выполняется как команда\n"
            "• Фото → анализ изображения\n"
            "• Аудио → расшифровка текста\n\n"
            "*Запросы к ИИ:*\n"
            "• Просто напиши любой вопрос или команду\n"
            "• `запомни [что-то]` — сохранить в память\n"
            "• `расскажи про [тема]` — поиск + ответ"
        )

    # ── ОСНОВНЫЕ ОБРАБОТЧИКИ ──────────────────────────────────────────────────

    async def handle_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        role = self._get_role(update)
        if role is ROLE_NONE:
            return await self._deny(update, "Доступ запрещён.")
        voice = update.message.voice if update.message else None
        if not voice:
            return await update.message.reply_text("❌ Голосовое не обнаружено.")
        await update.message.reply_text("🎙 Распознаю голос...")
        temp_path = None
        try:
            tg_file = await ctx.bot.get_file(voice.file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                temp_path = tmp.name
            await tg_file.download_to_drive(custom_path=temp_path)
            text = await asyncio.to_thread(self.core.transcribe_audio_path, temp_path)
            if not text:
                return await update.message.reply_text("🤷 Не удалось распознать. Попробуй ещё раз.")
            import re as _re
            text = _re.sub(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_]{0,30}:\s*", "", text).strip()
            await update.message.reply_text(f"📝 Распознано: *{text}*", parse_mode="Markdown")
            if role == ROLE_USER and self._check_user_blocked(text):
                return await self._deny(update, "Эта команда доступна только администратору.")
            result = await self.core.process_logic_async(text, self.admin, self.flasher)
            answer = result["answer"]
            state  = result["state"]
            if self.voice_reply:
                await self._reply_with_voice(update.message, answer[:500])
                if len(answer) > 50:
                    await update.message.reply_text(f"👁️ *ARGOS* `[{state}]`\n\n{answer[:4000]}", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"👁️ *ARGOS* `[{state}]`\n\n{answer[:4000]}", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка голосового: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try: os.remove(temp_path)
                except Exception: pass

    async def handle_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        role = self._get_role(update)
        if role is ROLE_NONE:
            return await self._deny(update, "Доступ запрещён.")
        photo = update.message.photo[-1] if update.message.photo else None
        if not photo:
            return await update.message.reply_text("❌ Изображение не обнаружено.")
        caption = update.message.caption or "Подробно опиши что изображено."
        await update.message.reply_text("🖼 Анализирую изображение...")
        temp_path = None
        try:
            tg_file = await ctx.bot.get_file(photo.file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                temp_path = tmp.name
            await tg_file.download_to_drive(custom_path=temp_path)
            if self.core.vision:
                result_text = await asyncio.to_thread(self.core.vision.analyze_image, temp_path, caption)
            else:
                result_text = "❌ Vision модуль не инициализирован."
            if self.voice_reply and result_text:
                await self._reply_with_voice(update.message, result_text[:500])
            await update.message.reply_text(result_text[:4000])
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка анализа: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try: os.remove(temp_path)
                except Exception: pass

    async def handle_audio(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        role = self._get_role(update)
        if role is ROLE_NONE:
            return await self._deny(update, "Доступ запрещён.")
        audio = update.message.audio if update.message else None
        if not audio:
            return await update.message.reply_text("❌ Аудиофайл не обнаружен.")
        await update.message.reply_text("🎵 Расшифровываю аудио...")
        temp_path = None
        try:
            tg_file = await ctx.bot.get_file(audio.file_id)
            suffix = os.path.splitext(audio.file_name)[1] if audio.file_name else ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                temp_path = tmp.name
            await tg_file.download_to_drive(custom_path=temp_path)
            text = await asyncio.to_thread(self.core.transcribe_audio_path, temp_path)
            if not text:
                return await update.message.reply_text("🤷 Не удалось расшифровать аудио.")
            await update.message.reply_text(f"📝 Распознано: *{text}*", parse_mode="Markdown")
            if role == ROLE_USER and self._check_user_blocked(text):
                return await self._deny(update, "Эта команда доступна только администратору.")
            result = await self.core.process_logic_async(text, self.admin, self.flasher)
            answer = result["answer"]
            state  = result["state"]
            if self.voice_reply:
                await self._reply_with_voice(update.message, answer[:500])
            await update.message.reply_text(f"👁️ *ARGOS* `[{state}]`\n\n{answer[:4000]}", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка аудио: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try: os.remove(temp_path)
                except Exception: pass

    # ── ПРЯМОЕ ВЫПОЛНЕНИЕ ФАЙЛОВЫХ КОМАНД ────────────────────────────────────
    # Обходит core.process_logic и ToolCalling полностью.
    # Вызывается ДО process_logic_async для известных команд.

    _FILE_COMMANDS = (
        # Файлы
        "создай файл", "напиши файл",
        "прочитай файл", "открой файл",
        "удали файл", "удали папку",
        "покажи файлы", "список файлов",
        "файлы ", "добавь в файл",
        "допиши в файл", "дополни файл",
        "отредактируй файл", "измени файл",
        "скопируй файл", "переименуй файл",
        # Терминал / процессы
        "консоль ", "терминал ",
        "список процессов", "убей процесс",
        "статус системы", "чек-ап",
        "осознай систему", "осознай свою систему", "осознай проект",
        "сканируй систему", "просканируй систему", "проанализируй систему",
        "сканируй проект", "просканируй проект", "проанализируй проект",
        "осознай файлы", "осознай свои файлы", "сканируй файлы",
        "просканируй файлы", "структура проекта", "обзор проекта",
        # Навыки — перехватываем ДО Ollama
        "список навыков", "навыки аргоса", "все навыки",
        "диагностика навыков", "проверь навыки", "навыки статус",
        "запусти навык ", "выполни навык ", "используй навык ", "навыки",
        # Навыки по имени
        "крипто", "биткоин", "bitcoin",
        "сканируй сеть", "сетевой призрак",
        "дайджест", "проверь железо",
        "погода", "weather",
        "серп", "serp", "serpapi", "поищи ",
        "реадме", "ридми", "прочитай реадме", "проверь реадме", "readme", "read me", "прочитай его",
        "ton ", "тон ", "ton wallet", "тон кошелек", "тон кошелёк",
        "ai спроси", "ask grok", "ask openai", "ai grok", "ai openai",
        "hf ", "huggingface", "hf space", "hf semantic", "hf index", "hf search", "hf dataset",
        "интегрируй все", "полная интеграция", "full integration",
        "автоагент", "agent auto", "анализ файлов", "файлмонитор", "file monitor",
        "агенты проекта", "помощники проекта", "примени агентов", "примени помощников", "/agents", "agents",
        # Управление системой / контроль
        "статус", "status", "помощь", "help",
        "роль", "роли", "провайдеры", "providers", "/providers", "/skills",
        "лимиты", "limits", "/limits",
        "память", "alerts", "алерты", "умные системы",
        "iot", "network", "sync", "history", "geo", "apk",
        # Управление AI режимами / проверка провайдеров
        "включи гига чат", "включи гигачат", "режим ии гигачат", "режим gigachat",
        "включи gemini", "включи гемини", "режим ии gemini", "режим ии гемини",
        "включи яндекс", "включи yandex", "включи yandexgpt", "режим ии yandexgpt", "режим ии яндекс",
        "проверь доступ к гемини", "проверь gemini", "check gemini",
        "включи auto", "режим ии auto", "режим авто",
        # AICoder
        "запусти aicoder", "включи aicoder", "aicoder",
        # Desktop Actions — мышь/клавиатура/скриншот
        "мышь", "mouse",
        "клавиша", "нажми", "key ",
        "печатай", "type ",
        "скриншот", "screenshot",
        "горячие клавиши", "экран статус", "desktop status",
    )

    def _offline_answer(self, text: str) -> str:
        """Офлайн-ответ когда все провайдеры недоступны или таймаут."""
        import random
        t = text.lower().strip()
        # Короткие сообщения → подсказка
        if len(t) <= 3 or t in ("?", "??", "help", "помощь"):
            return (
                "⏱ ARGOS [Timeout]\n\n"
                "AI провайдеры не ответили. Direct-команды:\n"
                "• статус — состояние системы\n"
                "• помощь — список команд\n"
                "• провайдеры — статус AI\n"
                "• + — пинг"
            )
        return random.choice(self._offline_phrases)

    def _try_direct_execute(self, text: str) -> str | None:
        """
        Выполняет файловые и системные команды напрямую через self.admin,
        минуя core.process_logic и ToolCalling полностью.
        Возвращает строку-ответ или None если команда не распознана.
        """
        t = text.lower().strip()
        if self.core and hasattr(self.core, "_looks_like_bulk_text_dump"):
            try:
                if self.core._looks_like_bulk_text_dump(text):
                    return self.core._analyze_bulk_text_dump(text)
            except Exception:
                pass
        if self.core and hasattr(self.core, "_extract_direct_url"):
            try:
                direct_url = self.core._extract_direct_url(text)
            except Exception:
                direct_url = None
            if direct_url:
                try:
                    if getattr(self.core, "web_explorer", None):
                        return self.core.web_explorer.fetch_page(direct_url)
                    return "⚠️ Web Explorer не инициализирован. Не могу прочитать ссылку."
                except Exception as e:
                    return f"❌ Ошибка чтения URL: {e}"
        if not any(t.startswith(cmd) or t == cmd.strip()
                   for cmd in self._FILE_COMMANDS):
            return None

        # ── Desktop Actions — мышь/клавиатура/скриншот ───────────────────
        _desktop_kw = ("мышь", "mouse", "клавиша", "нажми", "key ", "печатай",
                       "type ", "скриншот", "screenshot", "горячие клавиши",
                       "экран статус", "desktop status")
        if any(t.startswith(kw) for kw in _desktop_kw):
            if self.core and self.core.skill_loader:
                try:
                    res = self.core.skill_loader.dispatch(text, core=self.core)
                    if res is not None:
                        return res
                except Exception as _e:
                    return f"❌ desktop_actions: {_e}"
            return "❌ skill_loader не инициализирован"

        _skill_passthrough = (
            "ton ", "тон ", "ton wallet", "тон кошелек", "тон кошелёк",
            "ai спроси", "ask grok", "ask openai", "ai grok", "ai openai",
            "hf ", "huggingface",
            "serp", "серп", "serpapi", "поищи ",
            "автоагент", "agent auto", "анализ файлов", "файлмонитор", "file monitor",
            "headroom", "хедрум", "сжатие контекста",
        )
        if any(t.startswith(kw) for kw in _skill_passthrough):
            if self.core and getattr(self.core, "skill_loader", None):
                try:
                    res = self.core.skill_loader.dispatch(text, core=self.core)
                    if res is not None:
                        return str(res)
                except Exception as _e:
                    return f"❌ skill dispatch: {_e}"

        # ── ESP32-S3 голосовой модуль (Xiaozhi) ──────────────────────────
        import re as _esp_re
        _esp_kw = t.startswith(("esp ", "xiaozhi ", "голос", "voice "))
        if _esp_kw:
            _esp_text = t
            if _esp_text.startswith(("esp status", "xiaozhi status", "статус esp", "статус xiaozhi", "голос статус", "voice status")):
                import httpx as _httpx
                try:
                    with _httpx.Client(timeout=5) as _c:
                        r = _c.get("http://127.0.0.1:8006/xiaozhi/debug/sessions")
                        if r.is_success:
                            d = r.json()
                            ses = d.get("sessions", [])
                            if ses:
                                s = ses[0]
                                if isinstance(s, dict):
                                    sid = str(s.get("session_id", "?"))
                                    mac = s.get("mac", "?")
                                    ip = s.get("ip", "?")
                                    version = s.get("version", "?")
                                else:
                                    sid = str(s)
                                    mac = "?"
                                    ip = "?"
                                    version = "?"
                                return (
                                    f"🎤 ESP Голосовой модуль\n"
                                    f"Подключён: да\n"
                                    f"Сессия: {sid[:12]}...\n"
                                    f"MAC: {mac}\n"
                                    f"IP: {ip}\n"
                                    f"Прошивка: {version}"
                                )
                            return "🎤 ESP Голосовой модуль\nПодключён: нет\nСессий нет — устройство не в сети."
                except Exception as _ex:
                    return f"🎤 ESP сервер недоступен: {_ex}"
            if _esp_text.startswith(("esp diagnose", "xiaozhi diagnose", "esp диагностика", "диагностика esp")):
                import httpx as _httpx
                try:
                    with _httpx.Client(timeout=10) as _c:
                        r = _c.get("http://127.0.0.1:8006/xiaozhi/debug/sd_diagnose")
                        d = r.json()
                        lines = ["🔧 ESP SD Диагностика:"]
                        for k, v in d.items():
                            lines.append(f"  {k}: {v}")
                        return "\n".join(lines)
                except Exception as _ex:
                    return f"❌ Диагностика не удалась: {_ex}"
            if _esp_text.startswith(("esp play", "xiaozhi play", "esp играй", "esp включи", "голос играй")):
                import httpx as _httpx
                m = _esp_re.search(r"(?:play|играй|включи)\s+(.+)", _esp_text)
                path = m.group(1) if m else ""
                try:
                    with _httpx.Client(timeout=30) as _c:
                        url = "http://127.0.0.1:8006/xiaozhi/debug/play_sd"
                        if path:
                            url += f"?path={urllib.parse.quote(path)}"
                        r = _c.post(url)
                        d = r.json()
                        if d.get("status") == "sent" or d.get("status") == "ok":
                            return f"🎵 ESP играет: {d.get('file','?')}"
                        return json.dumps(d, ensure_ascii=False)
                except Exception as _ex:
                    return f"❌ Play не удался: {_ex}"
            if _esp_text.startswith(("esp music list", "xiaozhi music", "esp музыка", "голос музыка", "voice music")):
                import httpx as _httpx
                try:
                    with _httpx.Client(timeout=10) as _c:
                        r = _c.get("http://127.0.0.1:8006/xiaozhi/music/")
                        if r.is_success:
                            d = r.json()
                            files = d.get("sample", [])
                            from pathlib import Path as _P
                            lines = [f"🎵 ESP Музыка ({d.get('files',0)} файлов):"]
                            for f in files[:20]:
                                lines.append(f"  {_P(f).stem}")
                            if len(files) > 20:
                                lines.append(f"  ... и ещё {len(files) - 20}")
                            return "\n".join(lines)
                except Exception as _ex:
                    return f"❌ Список не получен: {_ex}"
            if _esp_text.startswith(("esp firmware", "xiaozhi firmware", "esp прошивка", "голос прошивка")):
                import httpx as _httpx
                info = []
                try:
                    with _httpx.Client(timeout=5) as _c:
                        r = _c.get("http://127.0.0.1:8006/xiaozhi/debug/firmware_sd")
                        d = r.json()
                        info.append(f"📦 Прошивки на SD: {json.dumps(d, ensure_ascii=False)}")
                except Exception as _ex:
                    info.append(f"SD прошивки: {_ex}")
                try:
                    with _httpx.Client(timeout=5) as _c:
                        r = _c.get("http://127.0.0.1:8006/health")
                        d = r.json()
                        info.append(f"Версия сервера: {d.get('version','?')}")
                        info.append(f"Версия ESP: {d.get('device_version','?')}")
                except Exception as _ex:
                    info.append(f"Health: {_ex}")
                return "\n".join(info)
            return "🎤 ESP голосовой модуль\nКоманды: статус, diagnose, play, music, firmware"

        if t in ("интегрируй все", "полная интеграция", "full integration"):
            try:
                return run_full_integration()
            except Exception as e:
                return f"❌ full integration: {e}"

        if t in ("агенты проекта", "помощники проекта", "примени агентов", "примени помощников", "/agents", "agents"):
            try:
                from src.argos_claude_api import ArgosClaudeAPI
                api = ArgosClaudeAPI(self.core)
                if not api.is_ready():
                    return "❌ Интегратор агентов не инициализирован."
                agents = api.list_agents(limit=30)
                if not agents:
                    return "❌ Агентов не найдено."
                lines = ["🤖 Агенты проекта:"]
                for a in agents:
                    lines.append(f"- {a['name']} [{a.get('category','?')}]")
                lines.append("")
                lines.append("Чтобы применить: `используй навык <имя>` или `агент <имя>: <задача>`")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ agents: {e}"

        if any(k in t for k in ("реадме", "ридми", "прочитай реадме", "проверь реадме", "readme", "read me", "прочитай его")):
            from pathlib import Path as _P
            roots = [
                _P.cwd(),
                _P(__file__).resolve().parents[2],  # project root
                _P(__file__).resolve().parents[1],  # src/
                _P(__file__).resolve().parents[0],  # src/connectivity/
            ]
            names = ("README.md", "readme.md", "Readme.md")
            for root in roots:
                for name in names:
                    candidate = root / name
                    try:
                        if candidate.exists():
                            try:
                                return candidate.read_text(encoding="utf-8", errors="ignore")[:12000]
                            except Exception:
                                with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                                    return f.read(12000)
                    except Exception:
                        continue
            # Последняя попытка — рекурсивный поиск по верхнему уровню проекта.
            for root in roots[:2]:
                try:
                    found = sorted(root.glob("**/README.md"))
                except Exception:
                    found = []
                for candidate in found[:3]:
                    try:
                        return candidate.read_text(encoding="utf-8", errors="ignore")[:12000]
                    except Exception:
                        continue
            return "❌ README.md не найден в проекте."

        if t in ("провайдеры", "providers", "/providers", "статус провайдеров"):
            try:
                from src.ai_providers import providers_status
                return providers_status()
            except Exception as e:
                return f"❌ providers: {e}"

        if t in ("список навыков", "навыки", "/skills", "skills", "навыки аргоса", "все навыки"):
            if self.core and getattr(self.core, "skill_loader", None):
                try:
                    return self.core.skill_loader.list_skills()
                except Exception as e:
                    return f"❌ skills: {e}"
            return "❌ skill_loader не инициализирован"

        if t in ("лимиты", "limits", "/limits", "лимиты провайдеров", "лимиты ai"):
            return self._build_limits_report()

        # ── Быстрые команды переключения AI режимов / проверки провайдеров ─
        if self.core:
            if any(k in t for k in ("включи гига чат", "включи гигачат", "режим ии гигачат", "режим gigachat")):
                try:
                    return self.core.set_ai_mode("gigachat")
                except Exception as e:
                    return f"❌ Не удалось переключить на GigaChat: {e}"

            if any(k in t for k in ("включи gemini", "включи гемини", "режим ии gemini", "режим ии гемини")):
                try:
                    return self.core.set_ai_mode("gemini")
                except Exception as e:
                    return f"❌ Не удалось переключить на Gemini: {e}"

            if any(k in t for k in ("включи яндекс", "включи yandex", "включи yandexgpt", "режим ии yandexgpt", "режим ии яндекс")):
                try:
                    return self.core.set_ai_mode("yandexgpt")
                except Exception as e:
                    return f"❌ Не удалось переключить на YandexGPT: {e}"

            if any(k in t for k in ("включи auto", "режим ии auto", "режим авто")):
                try:
                    return self.core.set_ai_mode("auto")
                except Exception as e:
                    return f"❌ Не удалось переключить на Auto: {e}"

            if any(k in t for k in ("проверь доступ к гемини", "проверь gemini", "check gemini")):
                try:
                    from src.ai_providers import providers_status
                    mode = self.core.ai_mode_label() if hasattr(self.core, "ai_mode_label") else "unknown"
                    gem_keys = []
                    for i in range(20):
                        if os.getenv(f"GEMINI_API_KEY_{i}") or os.getenv(f"GEMINI_API_KEY{i}"):
                            gem_keys.append(i)
                    has_fallback = bool(os.getenv("GEMINI_API_KEY"))
                    model_ready = bool(getattr(self.core, "model", None))
                    return (
                        f"{providers_status()}\n\n"
                        f"Текущий AI режим: {mode}\n"
                        f"Gemini ключей в пуле: {len(gem_keys)}"
                        + (" + fallback" if has_fallback else "")
                        + f"\nGemini client инициализирован: {'да' if model_ready else 'нет'}"
                    )
                except Exception as e:
                    return f"❌ Проверка Gemini не удалась: {e}"

            if any(k in t for k in ("запусти aicoder", "включи aicoder")):
                if getattr(self.core, "skill_loader", None):
                    try:
                        if hasattr(self.core.skill_loader, "dispatch_by_name"):
                            res = self.core.skill_loader.dispatch_by_name("ai_coder", text=text, core=self.core)
                            if res is not None:
                                return str(res)
                        for cmd in ("запусти навык ai_coder", "запусти навык aicoder", "используй навык ai_coder"):
                            res = self.core.skill_loader.dispatch(cmd, core=self.core)
                            if res is not None:
                                return str(res)
                    except Exception as e:
                        return f"❌ Ошибка запуска AICoder: {e}"
                return "❌ AICoder/skill_loader недоступен"

        # ── Навыки и система — НЕ требуют admin ──────────────────────────
        import os as _os3
        from pathlib import Path as _P3

        if self.core and (
            any(
                t.startswith(cmd) for cmd in (
                    "осознай систему", "осознай свою систему", "осознай проект",
                    "сканируй систему", "просканируй систему", "проанализируй систему",
                    "сканируй проект", "просканируй проект", "проанализируй проект",
                    "осознай файлы", "осознай свои файлы", "сканируй файлы",
                    "просканируй файлы", "структура проекта", "обзор проекта",
                )
            )
            or (
                any(m in t for m in ("осознай", "сканируй", "просканируй", "проанализируй", "аудит", "обзор"))
                and any(m in t for m in ("систем", "проект", "файл", "структур", "ядр", "навык", "модул"))
            )
        ):
            try:
                if hasattr(self.core, "_system_awareness_report"):
                    return self.core._system_awareness_report(self.admin)
            except Exception as e:
                return f"❌ Self-scan ошибка: {e}"

        if t in ("список навыков", "навыки аргоса", "все навыки", "навыки",
                  "диагностика навыков", "проверь навыки"):
            # Скан src/skills/ напрямую
            for _base3 in [_P3(__file__).resolve().parent,
                           _P3(__file__).resolve().parent.parent,
                           _P3.cwd()]:
                for _sub3 in ("src" + _os3.sep + "skills", "skills"):
                    _sd3 = _base3 / _sub3
                    if _sd3.exists():
                        _pkg_names3 = {f.name for f in _sd3.iterdir()
                                       if f.is_dir() and (f/"__init__.py").exists()}
                        _pkg3 = [f"  📦 {f.name}" for f in sorted(_sd3.iterdir())
                                 if f.is_dir() and (f/"__init__.py").exists()
                                 and not f.name.startswith("_")]
                        _flt3 = [f"  📄 {f.stem}" for f in sorted(_sd3.iterdir())
                                 if f.is_file() and f.suffix == ".py"
                                 and not f.name.startswith("_")
                                 and f.stem not in _pkg_names3]
                        _all3 = _pkg3 + _flt3
                        if _all3:
                            return (f"📚 НАВЫКИ АРГОСА ({len(_all3)}):\n"
                                    + "\n".join(_all3)
                                    + f"\n\nКаталог: {_sd3}")
            # Fallback через core
            if self.core:
                try:
                    if hasattr(self.core, "_skills_diagnostic"):
                        return self.core._skills_diagnostic()
                    if self.core.skill_loader:
                        return self.core.skill_loader.list_skills()
                except Exception:
                    pass
            return "📚 Навыки: укажи `диагностика навыков` для детальной информации"


        # Гарантируем admin
        adm = self.admin
        if adm is None:
            try:
                from src.admin import ArgosAdmin
                adm = ArgosAdmin()
                self.admin = adm
            except Exception as e:
                return f"❌ admin недоступен: {e}"

        try:
            # ── Навыки — абсолютный приоритет ────────────────────────────
            if t in ("список навыков", "навыки аргоса", "все навыки", "навыки"):
                # Прямой скан директории навыков
                import os as _os2
                from pathlib import Path as _P2
                for _base2 in [_P2(__file__).parent.parent,
                                _P2(__file__).parent,
                                _P2.cwd()]:
                    for _sub2 in ("src/skills", "skills"):
                        _sd2 = _base2 / _sub2.replace("/", _os2.sep)
                        if _sd2.exists():
                            _pkg2, _flt2 = [], []
                        _pkg_names2 = set()
                        for _f2 in sorted(_sd2.iterdir()):
                            if _f2.name.startswith("_"):
                                continue
                            if _f2.is_dir() and (_f2 / "__init__.py").exists():
                                _pkg2.append(f"  📦 {_f2.name}")
                                _pkg_names2.add(_f2.name)
                            elif _f2.is_file() and _f2.suffix == ".py":
                                if _f2.stem not in _pkg_names2:
                                    _flt2.append(f"  📄 {_f2.stem}")
                            _all2 = _pkg2 + _flt2
                            if _all2:
                                return (f"📚 НАВЫКИ АРГОСА ({len(_all2)} найдено):\n"
                                        + "\n".join(_all2)
                                        + f"\n\nКаталог: {_sd2}")
                # Если навыков нет — спросить core
                if self.core and getattr(self.core, "skill_loader", None):
                    try:
                        return self.core.skill_loader.list_skills()
                    except Exception:
                        pass
                return "📚 Навыки: src/skills не найден"

            if any(kw in t for kw in ("диагностика навыков", "проверь навыки", "навыки статус")):
                if self.core and hasattr(self.core, "_skills_diagnostic"):
                    return self.core._skills_diagnostic()

            if any(t.startswith(k) for k in ("запусти навык ", "выполни навык ", "используй навык ")):
                if self.core and getattr(self.core, "skill_loader", None):
                    try:
                        res = self.core.skill_loader.dispatch(text, core=self.core)
                        if res is not None:
                            return res
                    except Exception as e:
                        return f"❌ Ошибка запуска навыка: {e}"
                return "❌ skill_loader не инициализирован"

            # ── Создать файл ──────────────────────────────────────────────
            if any(t.startswith(k) for k in ("создай файл", "напиши файл")):
                body = text
                for k in ("создай файл", "напиши файл"):
                    body = body.replace(k, "").replace(k.capitalize(), "")
                body = body.strip()
                parts = body.split(maxsplit=1)
                fname    = parts[0] if parts else "note.txt"
                fcontent = parts[1] if len(parts) > 1 else ""
                return adm.create_file(fname, fcontent)

            # Прочитать файл
            if any(t.startswith(k) for k in ("прочитай файл", "открой файл")):
                path = text
                for k in ("прочитай файл", "открой файл"):
                    path = path.replace(k, "").replace(k.capitalize(), "").strip()
                return adm.read_file(path.strip())

            # Список файлов
            if any(t.startswith(k) for k in ("покажи файлы", "список файлов", "файлы ")):
                path = text
                for k in ("покажи файлы", "список файлов", "файлы"):
                    path = path.replace(k, "").replace(k.capitalize(), "").strip()
                return adm.list_dir(path or ".")

            # Удалить файл
            if any(t.startswith(k) for k in ("удали файл", "удали папку")):
                path = text
                for k in ("удали файл", "удали папку"):
                    path = path.replace(k, "").replace(k.capitalize(), "").strip()
                return adm.delete_item(path.strip())

            # Добавить в файл
            if any(t.startswith(k) for k in ("добавь в файл", "допиши в файл", "дополни файл")):
                tail = text
                for k in ("добавь в файл", "допиши в файл", "дополни файл"):
                    if k in t:
                        tail = text.split(k, 1)[-1].strip()
                        break
                parts = tail.split(maxsplit=1)
                if len(parts) >= 2:
                    return adm.append_file(parts[0], parts[1])
                return "Формат: добавь в файл [путь] [текст]"

            # Скопировать
            if t.startswith("скопируй файл"):
                tail = text.replace("скопируй файл", "").strip()
                parts = tail.split(maxsplit=1)
                if len(parts) == 2:
                    return adm.copy_file(parts[0], parts[1])
                return "Формат: скопируй файл [откуда] [куда]"

            # Переименовать
            if t.startswith("переименуй файл"):
                tail = text.replace("переименуй файл", "").strip()
                parts = tail.split(maxsplit=1)
                if len(parts) == 2:
                    return adm.rename_file(parts[0], parts[1])
                return "Формат: переименуй файл [старое] [новое]"

            # Консоль
            if t.startswith("консоль ") or t.startswith("терминал "):
                cmd = text.split(None, 1)[1].strip() if len(text.split()) > 1 else ""
                if cmd:
                    return adm.run_cmd(cmd, user="telegram")
                return "Формат: консоль [команда]"

            # Список процессов
            if t.startswith("список процессов"):
                return adm.list_processes()

            # Статус системы
            if any(t.startswith(k) for k in ("статус системы", "чек-ап")):
                return adm.get_stats()

            # Убить процесс
            if t.startswith("убей процесс"):
                name = text.replace("убей процесс", "").strip()
                return adm.kill_process(name) if name else "Укажи имя процесса"

            # ── СПИСОК НАВЫКОВ — читаем файловую систему напрямую ──────
            if any(t.strip() == k for k in
                   ("список навыков", "навыки аргоса", "все навыки", "навыки")):
                import os as _os
                from pathlib import Path as _P
                sd = None
                for _b in [_P(__file__).parent, _P.cwd()]:
                    for _sub in ("src/skills", "skills"):
                        _c = _b / _sub.replace("/", _os.sep)
                        if _c.exists():
                            sd = _c
                            break
                    if sd: break
                if sd:
                    pkg  = [f"  📦 {f.name}" for f in sorted(sd.iterdir())
                            if f.is_dir() and not f.name.startswith("_")]
                    flat = [f"  📄 {f.stem}" for f in sorted(sd.glob("*.py"))
                            if not f.stem.startswith("_")]
                    total = len(pkg) + len(flat)
                    lines = [f"📚 НАВЫКИ ({total}):"]  
                    if pkg:  lines += ["  [ПАКЕТЫ]"]  + pkg
                    if flat: lines += ["  [ФАЙЛЫ]"]   + flat
                    lines.append(f"  📂 {sd}")
                    return "\n".join(lines)
                return "❌ src/skills не найден"

            # ── ДИАГНОСТИКА навыков ─────────────────────────────────────
            if any(k in t for k in
                   ("диагностика навыков", "проверь навыки", "навыки статус")):
                core = self.core
                if core and hasattr(core, "_skills_diagnostic"):
                    return core._skills_diagnostic()
                return "❌ _skills_diagnostic недоступен"

        except Exception as e:
            return f"❌ Ошибка выполнения: {e}"

        return None


    async def handle_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        Принимает .py файлы от admin, применяет как патч к кодовой базе.

        Безопасность:
          - Только ADMIN_IDS могут отправлять патчи
          - Синтаксическая проверка перед применением
          - Автоматическая очистка __pycache__ (Windows-совместимо)
          - Резервная копия оригинала перед перезаписью
          - Отчёт о применении
        """
        if not self._is_admin(update):
            await self._deny(update, "Патчи только для администратора.")
            return

        doc = update.message.document if update.message else None
        if not doc:
            return

        fname = doc.file_name or ""
        # Telegram добавляет " (N)" к дублирующимся именам: "core (14).py" → "core.py"
        import re as _re
        fname = _re.sub(r' \(\d+\)(?=\.)', '', fname)
        # Принимаем только .py файлы
        if not fname.endswith(".py"):
            await self._safe_reply_text(
                update.message,
                f"❌ Только .py файлы. Получен: `{fname}`",
                markdown=True,
            )
            return

        await self._safe_reply_text(update.message, f"📥 Получен патч `{fname}`. Применяю...", markdown=True)

        import os, shutil, sys, importlib, tempfile, ast
        from pathlib import Path

        temp_path = None
        try:
            # 1. Скачиваем патч во временный файл
            tg_file = await ctx.bot.get_file(doc.file_id)
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False,
                                             mode='wb') as tmp:
                temp_path = tmp.name
            await tg_file.download_to_drive(custom_path=temp_path)

            # 2. Читаем содержимое
            with open(temp_path, 'r', encoding='utf-8') as f:
                patch_code = f.read()

            # 3. Синтаксическая проверка
            try:
                ast.parse(patch_code)
            except SyntaxError as se:
                await self._safe_reply_text(
                    update.message,
                    f"❌ Синтаксическая ошибка в патче:\n`{se}`",
                    markdown=True,
                )
                return

            # 4. Определяем куда сохранять
            # Имя файла → путь в проекте
            # patch_core.py          → core.py
            # patch_src_agent.py     → src/agent.py
            # src_connectivity_foo.py → src/connectivity/foo.py
            target_path = self._resolve_patch_target(fname)

            if target_path is None:
                # Файл не является патчем — сохраняем как есть рядом с main.py
                target_path = fname.replace("patch_", "")

            target = Path(target_path)

            # 5. Резервная копия если файл существует
            backup_path = None
            if target.exists():
                backup_path = str(target) + ".bak"
                shutil.copy2(str(target), backup_path)

            # 6. Создаём папку если нужно (Windows-совместимо)
            target.parent.mkdir(parents=True, exist_ok=True)

            # 7. Записываем патч
            with open(str(target), 'w', encoding='utf-8') as f:
                f.write(patch_code)

            # 8. Очищаем __pycache__ (Windows: удаляем .pyc для этого файла)
            cache_cleared = self._clear_pyc_cache(target)

            # 9. Горячая перезагрузка модуля если возможно
            hot_reload_msg = self._hot_reload_module(target_path)

            # 10. Отчёт
            lines = [
                f"✅ Патч `{fname}` применён!",
                f"📄 Файл: `{target_path}`",
                f"📦 Размер: {target.stat().st_size} байт",
                f"🗑 Кеш: {cache_cleared}",
            ]
            if backup_path:
                lines.append(f"💾 Резервная копия: `{backup_path}`")
            if hot_reload_msg:
                lines.append(f"🔄 {hot_reload_msg}")
            lines.append("\n⚡ Изменения вступят в силу при следующем запросе.")

            await self._safe_reply_text(update.message, "\n".join(lines), markdown=True)

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка применения патча: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def _resolve_patch_target(self, fname: str) -> str | None:
        """
        Определяет путь назначения по имени файла патча.

        Правила именования:
          patch_core.py            → core.py
          patch_admin.py           → src/admin.py  (если есть в src/)
          patch_src_agent.py       → src/agent.py
          patch_src_skills_foo.py  → src/skills/foo.py
          tool_calling.py          → src/tool_calling.py  (без prefix)
          agent.py                 → src/agent.py
          core.py                  → core.py
        """
        import os
        from pathlib import Path

        # Убираем prefix "patch_"
        name = fname
        if name.startswith("patch_"):
            name = name[6:]  # убрать "patch_"

        # Если имя содержит подчёркивания как разделители путей
        # src_connectivity_foo.py → src/connectivity/foo.py
        if "_" in name and not name.startswith("_"):
            parts = name.replace(".py", "").split("_")
            # Пробуем интерпретировать как путь
            for i in range(len(parts), 0, -1):
                candidate_dir = os.path.join(*parts[:i])
                candidate_file = "_".join(parts[i:]) + ".py"
                candidate = os.path.join(candidate_dir, candidate_file)
                if os.path.exists(candidate):
                    return candidate

        # Прямые совпадения
        known_locations = {
            "core.py":          "core.py",
            "admin.py":         "src/admin.py",
            "agent.py":         "src/agent.py",
            "tool_calling.py":  "src/tool_calling.py",
            "telegram_bot.py":  "src/connectivity/telegram_bot.py",
            "system_health.py": "src/connectivity/system_health.py",
            "orangepi_bridge.py": "src/connectivity/orangepi_bridge.py",
            "argos_logger.py":  "src/argos_logger.py",
            "argos_model.py":   "src/argos_model.py",
            "neural_swarm.py":  "src/neural_swarm.py",
            "sensor_bridge.py": "src/connectivity/sensor_bridge.py",
            "p2p_bridge.py":    "src/connectivity/p2p_bridge.py",
            "ollama_trainer.py": "src/ollama_trainer.py",
        }
        if name in known_locations:
            return known_locations[name]

        # Ищем файл в src/ рекурсивно
        for root, _, files in os.walk("src"):
            if name in files:
                return os.path.join(root, name)

        # Файл не найден — сохраняем рядом с main.py
        return name

    def _clear_pyc_cache(self, target_path) -> str:
        """Удаляет .pyc кеш для файла. Windows-совместимо."""
        import os, glob
        from pathlib import Path

        target = Path(target_path)
        cleared = 0

        # Папка __pycache__ рядом с файлом
        cache_dir = target.parent / "__pycache__"
        if cache_dir.exists():
            stem = target.stem
            for pyc in cache_dir.glob(f"{stem}.*.pyc"):
                try:
                    pyc.unlink()
                    cleared += 1
                except Exception:
                    pass
            # Если папка пуста — удаляем
            try:
                if not any(cache_dir.iterdir()):
                    cache_dir.rmdir()
            except Exception:
                pass

        # Также проверяем корневой __pycache__
        root_cache = Path("__pycache__")
        if root_cache.exists():
            for pyc in root_cache.glob(f"{target.stem}.*.pyc"):
                try:
                    pyc.unlink()
                    cleared += 1
                except Exception:
                    pass

        return f"очищено {cleared} .pyc файлов"

    def _hot_reload_module(self, path: str) -> str:
        """Пытается горячо перезагрузить модуль без перезапуска."""
        import sys, importlib
        from pathlib import Path

        # Конвертируем путь в имя модуля
        # src/agent.py → src.agent
        # core.py → core (не модуль — пропускаем)
        path_obj = Path(path)
        if path_obj.parts[0] == "src":
            module_name = ".".join(path_obj.with_suffix("").parts)
        else:
            return ""  # корневые файлы не перезагружаем горячо

        if module_name in sys.modules:
            try:
                mod = sys.modules[module_name]
                importlib.reload(mod)
                return f"Модуль `{module_name}` перезагружен"
            except Exception as e:
                return f"Горячая перезагрузка не удалась: {e}"

        return ""

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            user = getattr(update, "effective_user", None)
            text = getattr(getattr(update, "message", None), "text", "") or ""
            print(
                f"[TG] message_in uid={getattr(user, 'id', '?')} "
                f"user={getattr(user, 'username', '-') or '-'} "
                f"text={text[:160]!r}",
                flush=True,
            )
        except Exception:
            pass
        role = self._get_role(update)
        raw_text = getattr(getattr(update, "message", None), "text", "") or ""
        if _looks_like_telegram_export(raw_text):
            try:
                user = getattr(update, "effective_user", None)
                print(
                    f"[TG] telegram_export_quarantine uid={getattr(user, 'id', '?')} "
                    f"user={getattr(user, 'username', '-') or '-'}",
                    flush=True,
                )
            except Exception:
                pass
            return
        if self._should_quarantine_council_message(update, raw_text, role):
            try:
                user = getattr(update, "effective_user", None)
                print(
                    f"[TG] council_quarantine uid={getattr(user, 'id', '?')} "
                    f"user={getattr(user, 'username', '-') or '-'}",
                    flush=True,
                )
            except Exception:
                pass
            return
        if role is ROLE_NONE:
            await self._deny(update, "Доступ запрещён. Попытка входа зафиксирована.")
            return

        user_text = raw_text
        if not user_text.strip():
            return

        # Убираем префикс "Имя:" если сообщение переслано или скопировано с именем
        import re as _re
        user_text = _re.sub(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_]{0,30}:\s*", "", user_text).strip()
        if not user_text:
            return
        lt = user_text.lower().strip()
        chat_id = str(getattr(update.effective_chat, "id", "") or "")

        # Настройка HF токенов прямо из сообщения (admin-only для записи).
        hf_token_answer = self._handle_hf_token_message(user_text, role)
        if hf_token_answer is not None:
            await self._safe_reply_text(
                update.message,
                f"👁️ ARGOS [Direct]\n\n{hf_token_answer}",
                markdown=False,
            )
            return

        # Погода: жёсткий direct-роутинг, чтобы не уходить в Analytic-заглушки.
        if self.core and getattr(self.core, "skill_loader", None):
            is_weather_intent = (
                ("погод" in lt)
                or lt.startswith("weather")
                or lt in ("температура", "прогноз")
            )
            if is_weather_intent:
                try:
                    # Если это общий вопрос "погода" без города — запомним, что ждём город.
                    if lt in ("погода", "weather", "текущая погода", "погода сейчас"):
                        self._awaiting_weather_city.add(chat_id)
                        last_q = self._last_weather_query_by_chat.get(chat_id, "").strip()
                        query = last_q if last_q else ""
                    else:
                        self._awaiting_weather_city.discard(chat_id)
                        query = user_text
                    cmd = f"weather {query}".strip()
                    res = self.core.skill_loader.dispatch(cmd, core=self.core)
                    if res is not None:
                        if query:
                            self._last_weather_query_by_chat[chat_id] = query
                        await self._safe_reply_text(
                            update.message,
                            f"👁️ *ARGOS* `[Direct]`\n\n{res}",
                            markdown=True,
                        )
                        return
                except Exception:
                    pass
            elif chat_id in self._awaiting_weather_city and lt and len(lt) <= 120:
                # Пользователь после "погода" прислал только город/локацию.
                try:
                    query = user_text
                    res = self.core.skill_loader.dispatch(f"weather {query}", core=self.core)
                    if res is not None:
                        self._awaiting_weather_city.discard(chat_id)
                        self._last_weather_query_by_chat[chat_id] = query
                        await self._safe_reply_text(
                            update.message,
                            f"👁️ *ARGOS* `[Direct]`\n\n{res}",
                            markdown=True,
                        )
                        return
                except Exception:
                    pass

        # Batch-команды в одном сообщении (по строкам), например hf ...
        if "\n" in user_text:
            parts = [p.strip() for p in user_text.splitlines() if p.strip()]
            if parts and all(
                p.lower().startswith(("hf ", "/providers", "/skills", "/limits", "serp", "серп", "поищи", "найди "))
                for p in parts
            ):
                out_lines = []
                for part in parts:
                    direct = self._try_direct_execute(part)
                    out_lines.append(f"> {part}\n{direct or '❌ Не выполнено'}")
                await self._safe_reply_text(
                    update.message,
                    "👁️ *ARGOS* `[Direct]`\n\n" + "\n\n".join(out_lines),
                    markdown=True,
                )
                return

        # Проверка блокировок для USER
        if role == ROLE_USER and self._check_user_blocked(user_text):
            await self._deny(update, "Эта команда доступна только администратору.")
            return

        # Админ-тоггл режима контент-фетчера: "/content free" или "/content safe"
        if role == ROLE_ADMIN and user_text.lower().startswith(("/content", "контент")):
            parts = user_text.split(maxsplit=1)
            mode = parts[1] if len(parts) > 1 else ""
            msg = _set_content_mode(mode or "info")
            await self._safe_reply_text(update.message, msg, markdown=False)
            return

        # Сначала пробуем системные/файловые direct-команды, чтобы не терять контроль.
        direct_answer = self._try_direct_execute(user_text)
        if direct_answer is not None:
            await self._safe_reply_text(
                update.message,
                f"👁️ *ARGOS* `[Direct]`\n\n{direct_answer}",
                markdown=True,
            )
            return

        # ── Быстрый фото-поиск без захода в ядро ─────────────────────────
        # "фото <запрос>" → шлём картинку по URL (Unsplash source)
        # Быстрый контент-фечер: если в тексте проскакивают расширения или "фото"/"song"/"mp3"/"pdf"
        lt = user_text.lower()
        exts = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".wav", ".flac", ".ogg", ".pdf", ".txt", ".zip"]
        ext_aliases = {"mp3": ".mp3", "wav": ".wav", "flac": ".flac", "ogg": ".ogg"}
        keywords = [
            "фото", "photo", "song", "песня", "песню", "музыка", "муз", "трек", "track",
            "звук", "sound", "audio", "music", "mp3", "wav", "flac", "ogg",
            "нарисуй", "нарисовать", "рисунок", "картинку", "draw", "сгенерируй", "generate image", "создай картинку", "арт"
        ]
        detection_tokens = exts + list(ext_aliases.keys()) + keywords
        # НЕ перехватывать команды устройствам («включи музыку на ESP», «включи свет»)
        # — это управление, а не веб-поиск медиа. Пусть идёт в ядро (_nl_device_control).
        _dev_verb = any(v in lt for v in ("включи", "выключи", "вруби", "выруби", "зажги",
                                          "погаси", "turn on", "turn off", "запусти на"))
        _dev_target = any(d in lt for d in ("esp", "есп", "колонк", "speaker", "устройств",
                                            " свет", "розетк", "лампа", "лампу", "реле",
                                            "switch", " light", "на буке", "оранж", "orange"))
        # ФИКС: словное совпадение, а не подстрока. Короткие токены ("арт","муз","трек")
        # как подстроки ловили "стАРТ", "кАРТа" и любой технический текст → ответ ошибочно
        # уходил картинкой с обрезанным caption ("🖼 ный gro..."). Теперь:
        #  - короткие токены матчатся ТОЛЬКО как отдельные слова;
        #  - явные фразы ("нарисуй", "generate image") — как подстрока (они однозначны).
        _lt_words = set(lt.replace(",", " ").replace(".", " ").replace("/", " ")
                          .replace("!", " ").replace("?", " ").replace(":", " ").split())
        _explicit_phrases = ("нарисуй", "нарисовать", "сгенерируй", "generate image",
                             "создай картинку", "создай изображение", "draw ")
        _media_word_hit = any(tok in _lt_words for tok in detection_tokens
                              if " " not in tok)
        _media_phrase_hit = any(p in lt for p in _explicit_phrases)
        if _dev_verb and _dev_target:
            pass  # пропускаем медиа-фетчер → команда уйдёт в ядро/управление устройствами
        elif _media_word_hit or _media_phrase_hit:
            # выделяем предмет запроса: после первого слова/ключа
            query = user_text
            for key in (
                "фото", "photo", "нарисуй", "нарисовать", "рисунок", "картинку", "draw", "сгенерируй", "generate image", "создай картинку", "арт",
                "song", "песня", "песню", "музыка", "music", "audio", "звук", "sound",
                "трек", "track", "mp3", "wav", "flac", "ogg", "pdf"
            ):
                if key in lt:
                    query = user_text.lower().split(key, 1)[1].strip(" ,./\\")
                    if not query:
                        query = key
                    break
            # определяем примерное расширение
            preferred_ext = ".jpg"
            for ext in exts:
                if ext in lt:
                    preferred_ext = ext
                    break
            if preferred_ext == ".jpg":
                for token, mapped in ext_aliases.items():
                    if token in lt:
                        preferred_ext = mapped
                        break
            if preferred_ext == ".jpg":
                audio_words = {"song", "песня", "песню", "музыка", "муз", "трек", "track", "audio", "sound", "music"}
                if any(w in lt for w in audio_words):
                    preferred_ext = ".mp3"

            tmp_path = None
            candidates = []
            image_candidates = []

            # 1) Поиск изображений только через проверенные открытые API
            translated = _maybe_translate_ru_to_en(query)
            enhanced = _hf_enhance_image_prompt(translated or query)
            q_variants = []
            if enhanced:
                q_variants.append(enhanced[:180])
            if translated and translated != query:
                q_variants.append(translated)
            q_variants.append(query)

            if preferred_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                for qv in q_variants:
                    image_candidates.extend((u, "") for u in _fetch_openverse_images(qv, 10))
                    image_candidates.extend((u, "") for u in _fetch_wikimedia_images(qv, 10))
                    image_candidates.extend((u, "") for u in _fetch_pexels_images(qv, 10))
                    image_candidates.extend((u, "") for u in _fetch_unsplash_api_images(qv, 10))
            else:
                # для документов/аудио оставляем текстовый поиск DDG
                try:
                    try:
                        from ddgs import DDGS
                    except ImportError:
                        from duckduckgo_search import DDGS
                    with DDGS() as ddgs:
                        for qv in q_variants:
                            results = list(ddgs.text(f"{qv} filetype:{preferred_ext.strip('.')}", max_results=5))
                            candidates.extend([r.get("href") for r in results])
                            results2 = list(ddgs.text(f"{qv} {preferred_ext}", max_results=5))
                            candidates.extend([r.get("href") for r in results2])
                except Exception:
                    pass

            # 2) Фоллбэки по типам
            if preferred_ext in (".mp3", ".wav", ".flac", ".ogg"):
                # свободные примерные аудио
                candidates.append("https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Kai_Engel/Irsens_Tale/Kai_Engel_-_03_-_Moonlight_Reprise.mp3")
                candidates.append("https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Komiku/It's_time_for_adventure_/Komiku_-_12_-_Quest_Of_Hero.mp3")
                candidates.append("https://samplefiles.org/wp-content/uploads/2017/11/file_example_WAV_1MG.wav")
                candidates.append("https://samplefiles.org/wp-content/uploads/2017/11/file_example_WAV_10MG.wav")
                candidates.append("https://samplelib.com/lib/preview/ogg/sample-6s.ogg")
            elif preferred_ext in (".pdf", ".txt", ".zip"):
                candidates.append("https://samplefiles.org/wp-content/uploads/2020/09/sample-pdf-file.pdf")
                candidates.append("https://samplefiles.org/wp-content/uploads/2017/10/file-sample_150kB.pdf")
                candidates.append("https://samplefiles.org/wp-content/uploads/2017/10/file-sample_100kB.doc")

            # если это картинки — ранжируем по совпадению слов, иначе как было
            if preferred_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") and image_candidates:
                tokens = set()
                for v in q_variants:
                    tokens.update(t.lower() for t in v.split() if t)
                def score(pair):
                    url, title = pair
                    text = (title or "").lower() + " " + (url or "").lower()
                    return sum(1 for t in tokens if t and t in text)
                ranked = sorted(image_candidates, key=score, reverse=True)
                candidates = [u for (u, _) in ranked]

            # фильтруем по разрешённым доменам, если не свободный режим
            if not CONTENT_ALLOW_ALL:
                candidates = [u for u in candidates if u and _is_allowed(u)]

            # если всё отфильтровалось, подставляем безопасные примеры по типу
            if not candidates:
                if preferred_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    # Для изображений случайные fallback-источники отключены.
                    # Если нет кандидатов из OpenVerse/Wikimedia/Pexels/Unsplash API,
                    # ниже сработает HF-генерация.
                    candidates = []
                elif preferred_ext in (".mp3", ".wav", ".flac", ".ogg"):
                    candidates = [
                        "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Kai_Engel/Irsens_Tale/Kai_Engel_-_03_-_Moonlight_Reprise.mp3",
                        "https://samplefiles.org/wp-content/uploads/2017/11/file_example_WAV_1MG.wav",
                        "https://samplelib.com/lib/preview/ogg/sample-6s.ogg"
                    ]
                else:
                    candidates = [
                        "https://samplefiles.org/wp-content/uploads/2020/09/sample-pdf-file.pdf"
                    ]

            # удаляем дубликаты, сохраняя порядок
            seen = set()
            deduped = []
            for u in candidates:
                if u and u not in seen:
                    deduped.append(u)
                    seen.add(u)

            # Не отправляем тот же URL повторно для того же запроса.
            if preferred_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                last_url = self._last_image_url_by_query.get(query.lower().strip())
                if last_url:
                    filtered = [u for u in deduped if u != last_url]
                else:
                    filtered = deduped
            else:
                filtered = deduped

            # Выбираем лучший URL (первый из неповторяющихся, или просто первый)
            send_pool = filtered if filtered else deduped
            chosen_url = send_pool[0] if send_pool else None

            if preferred_ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                img_bytes = None
                if not chosen_url:
                    # Нет URL из открытых источников — генерируем через HF
                    await update.message.reply_text("🎨 Генерирую изображение...")
                    img_bytes = await asyncio.to_thread(
                        _hf_generate_image_via_spaces, (enhanced or translated or query)
                    )
                    if not img_bytes:
                        img_bytes = await asyncio.to_thread(
                            _hf_generate_image, (enhanced or translated or query)
                               )
                if img_bytes:
                    try:
                        import io as _io
                        await update.message.reply_photo(
                            photo=_io.BytesIO(img_bytes), caption=f"🖼 AI-генерация по запросу: {query[:170]}\n(изображение синтезировано, не проверено на соответствие)"
                        )
                    except Exception as _e:
                        await self._safe_reply_text(
                            update.message,
                            f"❌ Не удалось отправить изображение: {_e}",
                            markdown=False,
                        )
                elif chosen_url:
                    try:
                        self._last_image_url_by_query[query.lower().strip()] = chosen_url
                        await update.message.reply_photo(
                            photo=chosen_url, caption=f"🖼 AI-генерация по запросу: {query[:170]}\n(изображение синтезировано, не проверено на соответствие)"
                        )
                    except Exception:
                        img_bytes = await asyncio.to_thread(
                            _hf_generate_image_via_spaces, (enhanced or translated or query)
                        )
                        if not img_bytes:
                            img_bytes = await asyncio.to_thread(
                                _hf_generate_image, (enhanced or translated or query)
                            )
                        if img_bytes:
                            try:
                                import io as _io
                                await update.message.reply_photo(
                                    photo=_io.BytesIO(img_bytes), caption=f"🖼 AI-генерация по запросу: {query[:170]}\n(изображение синтезировано, не проверено на соответствие)"
                                )
                            except Exception:
                                await self._safe_reply_text(
                                    update.message, f"❌ Изображение недоступно для: {query}", markdown=False
                                )
                        else:
                            await self._safe_reply_text(
                                update.message, f"❌ Изображение не найдено для: {query}", markdown=False
                            )
                else:
                    await self._safe_reply_text(
                        update.message, f"❌ Изображение не найдено для: {query}", markdown=False
                    )

            elif preferred_ext in (".mp3", ".wav", ".flac", ".ogg"):
                if chosen_url:
                    try:
                        await update.message.reply_audio(
                            audio=chosen_url, caption=f"🎵 {query[:200]}"
                        )
                    except Exception:
                        try:
                            await update.message.reply_document(
                                document=chosen_url, caption=f"🎵 {query[:200]}"
                            )
                        except Exception:
                            await self._safe_reply_text(
                                update.message, f"🎵 Аудио: {chosen_url}", markdown=False
                            )
                else:
                    await self._safe_reply_text(
                        update.message, f"❌ Аудиофайл не найден для: {query}", markdown=False
                    )

            else:
                if chosen_url:
                    try:
                        await update.message.reply_document(
                            document=chosen_url, caption=f"📄 {query[:200]}"
                        )
                    except Exception:
                        await self._safe_reply_text(
                            update.message, f"📄 Файл: {chosen_url}", markdown=False
                        )
                else:
                    await self._safe_reply_text(
                        update.message, f"❌ Файл не найден для: {query}", markdown=False
                    )
            return

        # -- Быстрый путь через AIRouter — ТОЛЬКО для вопросов, не команд --------
        # Команды (свет, файлы, запуск, HA) идут через ArgosCore с реальными скиллами
        _CMD_KEYWORDS = (
            "включи", "выключи", "включить", "выключить", "открой", "закрой",
            "запусти", "останови", "перезапусти", "покажи файл", "прочитай",
            "свет", "розетку", "термостат", "температуру установи",
            "отчёт с", "отчет с", "obsidian", "обсидиан",
            "p2p запусти", "сканируй", "обнови прошивку",
            "телефон", "phone", "android",
        )
        _lt = user_text.lower()
        _lt2 = _lt.strip()
        _is_command = any(kw in _lt for kw in _CMD_KEYWORDS)

        
        # Web search (DuckDuckGo)
        _search_kw = ["найди", "поиск", "search", "загугли", "в интернете"]
        if any(k in lt for k in _search_kw):
            try:
                from duckduckgo_search import DDGS
                _q = user_text
                for _k in _search_kw:
                    _q = _q.replace(_k, "").strip()
                with DDGS() as d:
                    _r = list(d.text(_q, max_results=3))
                if _r:
                    _o = []
                    for r in _r:
                        _o.append(r.get("title", ""))
                        _o.append(r.get("body", "")[:150])
                        _o.append(r.get("href", ""))
                        _o.append("")
                    await self._safe_reply_text(update.message, "ARGOS [Search]\n\n" + "\n".join(_o), markdown=False)
                    return
            except Exception:
                pass

        _ai_router_answer = None
        if not _is_command:
            try:
                import asyncio as _aio
                from src.ai_router import AIRouter as _AIR
                _router = _AIR()
                _SYSTEM_RU = (
                    "Ты ARGOS Universal OS — AI-ассистент системы из 5 машин. "
                    "Отвечай только на РУССКОМ языке. Кратко и точно. "
                    "Если вопрос о системе — давай конкретные данные, не выдумывай. "
                    f"Текущее время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
                )
                def _ask_ru(text):
                    return _router.ask(text, system=_SYSTEM_RU)
                _ai_router_answer = await _aio.wait_for(
                    _aio.get_event_loop().run_in_executor(None, _ask_ru, user_text),
                    timeout=12.0
                )
            except Exception:
                pass
        # Web search (DuckDuckGo)
        _search_kw = ["найди", "поиск", "search", "загугли", "в интернете"]
        if any(k in lt for k in _search_kw):
            try:
                from duckduckgo_search import DDGS
                _q = user_text
                for _k in _search_kw:
                    _q = _q.replace(_k, "").strip()
                with DDGS() as d:
                    _r = list(d.text(_q, max_results=3))
                if _r:
                    _o = []
                    for r in _r:
                        _o.append(r.get("title", ""))
                        _o.append(r.get("body", "")[:150])
                        _o.append(r.get("href", ""))
                        _o.append("")
                    await self._safe_reply_text(update.message, "ARGOS [Search]\n\n" + "\n".join(_o), markdown=False)
                    return
            except Exception:
                pass

        _ai_router_answer = None


        # -- Colibri Phone Control --
        if any(_lt2.startswith(p) for p in ("колибри", "colibri ")):
            try:
                import socket as _sk, json as _jj, time as _tt
                _cmd = _lt2.replace("колибри","").replace("colibri","").strip()
                _sock = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
                _sock.setsockopt(_sk.SOL_SOCKET, _sk.SO_BROADCAST, 1)
                _sock.settimeout(8)
                _sock.bind(("0.0.0.0", 5024))
                _msg = json.dumps({"proto":1,"node_id":"tg-bot","type":2,"code":_cmd or "status"}).encode()
                _sock.sendto(_msg, ("192.168.1.149", 5021))
                _ans = "нет ответа"
                try:
                    _d, _ = _sock.recvfrom(8192)
                    _m = _jj.loads(_d.decode())
                    if "result" in _m: _ans = _m["result"]
                    elif "status" in _m: _ans = str(_m["status"])
                except: pass
                _sock.close()
                answer = f"🐦 Colibri → {_cmd or 'status'}:\n{_ans}"
                state = "ARGOS"
                await update.message.reply_text(answer)
                return
            except Exception as _ce:
                pass

        # -- Phone Control (обрабатываем здесь чтобы не ждать core) --
        if any(_lt2.startswith(p) for p in ("телефон", "phone ", "android ")):
            try:
                from src.skills.phone_control import PhoneControl as _PC
                _pc = _PC(self.core)
                _phone_result = _pc.handle(user_text)
                if _phone_result:
                    answer = _phone_result
                    state = "ARGOS"
                    await update.message.reply_text(answer)
                    return
            except Exception as _pe:
                pass

        if _ai_router_answer and not _is_command:
            answer = _ai_router_answer
            state = "ARGOS"
        else:
            # -- Основной путь: ответ от ядра --
            try:
                result = await asyncio.wait_for(
                    self.core.process_logic_async(user_text, self.admin, self.flasher),
                    timeout=25.0
                )
                answer = result["answer"]
                state  = result["state"]
            except asyncio.TimeoutError:
                direct = self._try_direct_execute(user_text)
                if direct:
                    answer = direct
                    state = "Direct"
                else:
                    answer = self._offline_answer(user_text)
                    state = "Timeout"
            except Exception as _exc:
                direct = self._try_direct_execute(user_text)
                if direct:
                    answer = direct
                    state = "Direct"
                else:
                    answer = f"⚠️ Ошибка: {type(_exc).__name__}"
                    state = "Error"

        # Сохраняем пару (user, assistant) в БД для /history и контекста
        try:
            db = getattr(self.core, "db", None) or getattr(getattr(self.core, "context", None), "db", None)
            if db is not None and hasattr(db, "add_message"):
                db.add_message("user", user_text, "tg")
                if answer:
                    db.add_message("argos", str(answer), str(state) if state else "ai")
        except Exception:
            pass

        if answer:
            full_reply = 'ARGOS [' + str(state) + ']\n\n' + str(answer)
            await self._safe_reply_text(update.message, full_reply, markdown=False)
        else:
            await update.message.reply_text('ARGOS: нет ответа от провайдеров.')

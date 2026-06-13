#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py — Сборка проекта ARGOS из исходников
=============================================
Запускает все скрипты восстановления и собирает полную структуру проекта.

Использование:
    python build.py

Что создаётся:
    src/           — исходный код всех модулей
    data/          — папки для данных и моделей
    logs/          — логи
    tests/         — тесты
    assets/        — прошивки и ресурсы
    requirements.txt
    .env           — шаблон переменных окружения
    .gitignore
"""

from __future__ import annotations

import os
import sys
import shutil
import textwrap
from pathlib import Path

# ── Корень проекта — папка, где лежит build.py ────────────────────────────────
ROOT = Path(__file__).parent.resolve()

print("=" * 62)
print("  ARGOS — СБОРКА ПРОЕКТА ИЗ ИСХОДНИКОВ")
print(f"  Рабочая папка: {ROOT}")
print("=" * 62)


def _w(rel_path: str, content: str) -> None:
    """Записывает файл относительно ROOT, создавая директории."""
    full = ROOT / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    size = len(content)
    print(f"  ✅  {rel_path:<55} ({size:>7} б)")


def _run_restore(script_name: str) -> None:
    """
    Выполняет скрипт восстановления, подменяя ROOT/BASE на текущую папку.
    Пропускает git-команды subprocess.
    """
    script_path = ROOT / script_name
    if not script_path.exists():
        print(f"  ⚠️  {script_name} — не найден, пропускаю")
        return

    source = script_path.read_text(encoding="utf-8")

    # Патчим жёстко заданные пути Colab на текущую директорию
    source = source.replace('ROOT = Path("/content/v1-3")', f'ROOT = Path(r"{ROOT}")')
    source = source.replace('BASE = "/content/v1-3"', f'BASE = r"{ROOT}"')
    source = source.replace('"/content/v1-3"', f'r"{ROOT}"')
    source = source.replace("ROOT.mkdir(parents=True, exist_ok=True)", "")
    source = source.replace("os.chdir(ROOT)", "")

    # Нейтрализуем git-команды внутри скриптов
    source = source.replace(
        '"cd /content/v1-3 && git',
        '"echo [skipped] git',
    )

    ns: dict = {"__name__": "__build__", "__file__": str(script_path)}
    try:
        exec(compile(source, script_name, "exec"), ns)  # noqa: S102
    except SystemExit:
        pass
    except Exception as exc:
        print(f"  ⚠️  {script_name}: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 1 — Базовая структура папок
# ══════════════════════════════════════════════════════════════════════════════
print("\n[1/10] Создаю структуру директорий...")

DIRS = [
    "src",
    "src/core",
    "src/quantum",
    "src/security",
    "src/connectivity",
    "src/interface",
    "src/skills",
    "src/skills/evolution",
    "src/modules",
    "src/knowledge",
    "src/nlp",
    "data",
    "data/argos_model",
    "logs",
    "modules",
    "assets",
    "assets/firmware",
    "tests",
    "tests/generated",
    ".github/workflows",
    "examples",
    "examples/scenarios",
]
for d in DIRS:
    p = ROOT / d
    p.mkdir(parents=True, exist_ok=True)
    init = p / "__init__.py"
    if "src" in d.split("/") and not init.exists():
        init.write_text("", encoding="utf-8")

print("  ✅  Структура директорий создана")

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 2 — requirements.txt
# ══════════════════════════════════════════════════════════════════════════════
print("\n[2/10] Создаю requirements.txt...")
_w(
    "requirements.txt",
    textwrap.dedent("""\
    # ── ИИ-ядра
    google-genai>=1.0.0
    ibm-watsonx-ai>=1.3.42,<1.4.0; python_version < "3.11"
    ibm-watsonx-ai>=1.4.2; python_version >= "3.11"
    ollama>=0.4.9
    requests>=2.31.0
    python-dotenv>=1.0.0

    # ── Telegram
    python-telegram-bot>=21.0

    # ── Web UI
    fastapi>=0.115.0
    uvicorn>=0.30.0
    streamlit>=1.40.0

    # ── Системный мониторинг
    psutil>=5.9.0

    # ── Голос (опционально)
    pyttsx3>=2.90
    SpeechRecognition>=3.10.0
    faster-whisper>=1.0.3

    # ── Desktop GUI
    customtkinter>=5.2.0

    # ── IoT
    paho-mqtt>=2.0.0
    pyserial>=3.5

    # ── Безопасность
    cryptography>=42.0.0

    # ── Парсинг
    beautifulsoup4>=4.12.0

    # ── Версионирование
    packaging>=23.0
    networkx>=3.2.1

    # ── ML (собственная модель)
    scikit-learn>=1.4.0
    numpy>=1.26.0
"""),
)

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 3 — .env шаблон и .gitignore
# ══════════════════════════════════════════════════════════════════════════════
print("\n[3/10] Создаю .env и .gitignore...")

env_path = ROOT / ".env"
if not env_path.exists():
    _w(
        ".env",
        textwrap.dedent("""\
        # ARGOS .env — заполни свои ключи
        GEMINI_API_KEY=
        TELEGRAM_BOT_TOKEN=
        USER_ID=
        ARGOS_NETWORK_SECRET=
        ARGOS_MASTER_KEY=
        PYPI_TOKEN=
        PUPI_API_URL=
        PUPI_API_TOKEN=
    """),
    )

_w(
    ".gitignore",
    textwrap.dedent("""\
    .env
    *.db
    *.pyc
    __pycache__/
    *.log
    build/
    dist/
    *.egg-info/
    .venv/
    data/argos_model/*.pkl
    config/master.key
    config/master_auth.hash
    config/node_id
    config/node_birth
"""),
)

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 4 — Ключевые модули ядра (argos_logger, core, quantum)
# ══════════════════════════════════════════════════════════════════════════════
print("\n[4/10] Создаю ключевые модули ядра...")

_w(
    "src/argos_logger.py",
    textwrap.dedent("""\
    \"\"\"argos_logger.py — единый логгер проекта\"\"\"
    import logging
    import os


    def get_logger(name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ))
            logger.addHandler(handler)
        level = os.getenv("ARGOS_LOG_LEVEL", "INFO").upper()
        logger.setLevel(getattr(logging, level, logging.INFO))
        return logger
"""),
)

_w("src/quantum/__init__.py", "")
_w(
    "src/quantum/logic.py",
    textwrap.dedent("""\
    \"\"\"quantum/logic.py — квантовые состояния Аргоса\"\"\"
    import time

    try:
        import psutil
        _PSUTIL = True
    except ImportError:
        _PSUTIL = False

    STATES = {
        "Analytic":   {"creativity": 0.2, "window": 6,  "allow_root": True},
        "Creative":   {"creativity": 0.9, "window": 15, "allow_root": False},
        "Protective": {"creativity": 0.1, "window": 8,  "allow_root": False},
        "Unstable":   {"creativity": 0.5, "window": 4,  "allow_root": False},
        "All-Seeing": {"creativity": 0.7, "window": 20, "allow_root": True},
        "System":     {"creativity": 0.0, "window": 5,  "allow_root": True},
    }


    class QuantumEngine:
        def __init__(self):
            self.current = "Analytic"
            self._ts = time.time()

        def generate_state(self) -> dict:
            self._auto_switch()
            return {"name": self.current, "vector": list(STATES[self.current].values())}

        def _auto_switch(self):
            if not _PSUTIL:
                return
            try:
                cpu = psutil.cpu_percent(interval=0.1)
                ram = psutil.virtual_memory().percent
                if cpu > 85 or ram > 90:
                    self.current = "Protective"
                elif cpu > 70:
                    self.current = "Unstable"
            except Exception:
                pass

        def set_state(self, name: str) -> str:
            if name in STATES:
                self.current = name
                return f"⚛️ Квантовое состояние: {name}"
            return f"❌ Неизвестное состояние: {name}"

        def status(self) -> str:
            s = STATES[self.current]
            return (
                f"⚛️ Состояние: {self.current}\\n"
                f"  Творчество: {s['creativity']}\\n"
                f"  Окно памяти: {s['window']}\\n"
                f"  Root-команды: {s['allow_root']}"
            )


    ArgosQuantum = QuantumEngine
"""),
)

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 5 — Запуск ARGOS_EMERGENCY_RESTORE (src/core.py и основные модули)
# ══════════════════════════════════════════════════════════════════════════════
print("\n[5/10] Восстанавливаю ядро (EMERGENCY_RESTORE)...")
_run_restore("ARGOS_EMERGENCY_RESTORE.py")

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 6 — Запуск скриптов восстановления PART2..PART7
# ══════════════════════════════════════════════════════════════════════════════
print("\n[6/10] Восстанавливаю модули (PART2..PART7)...")
for part in [
    "ARGOS_RESTORE_PART2.py",
    "ARGOS_RESTORE_PART3.py",
    "ARGOS_RESTORE_PART4.py",
    "ARGOS_RESTORE_PART5-1.py",
    "ARGOS_RESTORE_PART6.py",
    "ARGOS_RESTORE_PART7.py",
]:
    print(f"  ▶  {part}")
    _run_restore(part)

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 7 — Дополнительные модули (MODULES, GUI, FINAL)
# ══════════════════════════════════════════════════════════════════════════════
print("\n[7/10] Восстанавливаю дополнительные модули...")
for script in [
    "ARGOS_RESTORE_MODULES.py",
    "ARGOS_RESTORE_GUI.py",
    "ARGOS_RESTORE_FINAL.py",
]:
    print(f"  ▶  {script}")
    _run_restore(script)

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 8 — Копируем argos_model.py и pypi_publisher.py в src/
# ══════════════════════════════════════════════════════════════════════════════
print("\n[8/10] Копирую argos_model.py и pypi_publisher.py в src/...")

for fname in ["argos_model.py", "pypi_publisher.py"]:
    src_file = ROOT / fname
    dst_file = ROOT / "src" / fname
    if src_file.exists() and not dst_file.exists():
        shutil.copy2(src_file, dst_file)
        print(f"  ✅  {fname} → src/{fname}")
    elif dst_file.exists():
        print(f"  ⏭️  src/{fname} уже существует, пропускаю")
    else:
        print(f"  ⚠️  {fname} не найден")

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 9 — pyproject.toml
# ══════════════════════════════════════════════════════════════════════════════
print("\n[9/10] Создаю pyproject.toml...")
_w(
    "pyproject.toml",
    textwrap.dedent("""\
    [build-system]
    requires = ["setuptools>=68", "wheel"]
    build-backend = "setuptools.build_meta"

    [project]
    name = "argos-universalsigtrip"
    version = "2.1.4"
    description = "ARGOS — децентрализованная автономная ИИ-система"
    readme = "README.md"
    license = { text = "MIT" }
    requires-python = ">=3.10"
    dependencies = [
        "google-genai>=1.0.0",
        'ibm-watsonx-ai>=1.3.42,<1.4.0; python_version < "3.11"',
        'ibm-watsonx-ai>=1.4.2; python_version >= "3.11"',
        "ollama>=0.4.9",
        "requests>=2.31.0",
        "beautifulsoup4>=4.12.0",
        "python-dotenv>=1.0.0",
        "python-telegram-bot>=21.0",
        "fastapi>=0.115.0",
        "uvicorn>=0.30.0",
        "streamlit>=1.40.0",
        "psutil>=5.9.0",
        "cryptography>=42.0.0",
        "py7zr>=0.22.0",
        "packaging>=23.0",
        "networkx>=3.2.1",
        "scikit-learn>=1.4.0",
        "numpy>=1.26.0",
        "pyttsx3>=2.90",
        "SpeechRecognition>=3.10.0",
        "faster-whisper>=1.0.3",
        "customtkinter>=5.2.0",
        "pyserial>=3.5",
        "paho-mqtt>=2.0.0",
    ]

    [project.scripts]
    argos = "main:main"

    [tool.setuptools.packages.find]
    where = ["."]
    include = ["src*"]
"""),
)

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 10 — Итог
# ══════════════════════════════════════════════════════════════════════════════
print("\n[10/10] Проверка результата...")

# Подсчёт созданных файлов
created = list((ROOT / "src").rglob("*.py"))
print(f"\n  Файлов в src/: {len(created)}")

critical = [
    "src/argos_logger.py",
    "src/core.py",
    "src/quantum/logic.py",
    "src/argos_model.py",
    "src/pypi_publisher.py",
    "requirements.txt",
    "pyproject.toml",
]
missing = [f for f in critical if not (ROOT / f).exists()]
ok = len(critical) - len(missing)

print(f"  Ключевые файлы: {ok}/{len(critical)}")
if missing:
    print("  ❌ Отсутствуют:")
    for f in missing:
        print(f"     - {f}")
else:
    print("  ✅ Все ключевые файлы на месте")

print()
print("=" * 62)
print("  ARGOS — СБОРКА ЗАВЕРШЕНА")
print()
print("  Следующие шаги:")
print("    pip install -r requirements.txt")
print("    python main.py --no-gui")
print("=" * 62)

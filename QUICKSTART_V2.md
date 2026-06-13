# 🚀 ARGOS v2.0.0 — Быстрый старт

> Полная настройка с нуля за **5 минут**.  
> Выбери свой путь: [Desktop](#1-desktop) · [Docker](#2-docker) · [Telegram-only](#3-telegram-only) · [Android APK](#4-android-apk)

---

## Требования

| | Минимум | Рекомендуется |
|---|---|---|
| **Python** | 3.10 | 3.12 |
| **RAM** | 512 MB | 2 GB+ |
| **Диск** | 1 GB | 4 GB |
| **ОС** | Windows 10 / Ubuntu 20.04 / macOS 12 | любая |

---

## 1. Desktop

### Шаг 1 — Клонировать

```bash
git clone https://github.com/labuaqlysnecy/Argos.git
cd SiGtRiP
```

### Шаг 2 — Установить зависимости

```bash
pip install -r requirements.txt

# Linux — если нужен голос:
sudo apt-get install portaudio19-dev
pip install PyAudio

# Windows — если PyAudio не ставится:
pip install pipwin && pipwin install pyaudio
```

### Шаг 3 — Настроить .env

```bash
cp .env.example .env
```

Открой `.env` и заполни **минимум один** AI-ключ:

```env
# AI — минимум один:
GEMINI_API_KEY=твой_ключ_с_ai.google.dev      # бесплатно
# OPENAI_API_KEY=sk-...
# WATSONX_API_KEY=...

# Telegram (необязательно, но рекомендуется):
TELEGRAM_BOT_TOKEN=токен_от_@BotFather
USER_ID=твой_telegram_id_из_@userinfobot

# Безопасность REST API (рекомендуется):
ARGOS_REMOTE_TOKEN=придумай_секретный_токен
```

### Шаг 4 — Инициализировать и запустить

```bash
python genesis.py           # создаёт структуру папок и БД (один раз)
python startup_validator.py # ✅ проверить окружение
python main.py              # запустить с Desktop GUI
```

**Готово!** ARGOS открылся с графическим интерфейсом.

---

## 2. Docker

Самый быстрый способ — без Python на хосте.

```bash
git clone https://github.com/labuaqlysnecy/Argos.git
cd SiGtRiP
cp .env.example .env
# Отредактировать .env
nano .env

docker-compose up -d
docker-compose logs -f argos_node
```

Веб-панель доступна на [http://localhost:8080](http://localhost:8080).

**Или через GHCR (готовый образ):**

```bash
docker pull ghcr.io/labuaqlysnecy/Argos:2.0.0
docker run -d \
  --name argos \
  --env-file .env \
  -p 8080:8080 \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  ghcr.io/labuaqlysnecy/Argos:2.0.0
```

---

## 3. Telegram-only

Минимальная настройка только для Telegram-бота без GUI:

```bash
# .env — достаточно только этих переменных:
GEMINI_API_KEY=твой_ключ
TELEGRAM_BOT_TOKEN=токен_бота
USER_ID=твой_telegram_id

python main.py --no-gui
```

Теперь пишешь боту в Telegram:
- `/status` — статус системы
- `/crypto` — крипто-котировки
- `/memory` — что запомнил Аргос
- Любой текст — команда ядру

---

## 4. Android APK

1. Скачай APK из [последнего релиза](https://github.com/labuaqlysnecy/Argos/releases/latest)
   или из [Actions → Android APK](https://github.com/labuaqlysnecy/Argos/actions)
2. Установи, разреши «Установку из неизвестных источников»
3. В настройках приложения укажи:
   - **Server URL**: адрес твоего Аргоса (или Google Colab туннель)
   - **Bearer Token**: значение `ARGOS_REMOTE_TOKEN` из `.env`
4. Нажми **Обновить** на вкладке Dashboard

**Нет сервера?** Запусти в Google Colab за 3 минуты →
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/labuaqlysnecy/Argos/blob/main/colab/ARGOS_Colab_Launch.ipynb)

---

## Режимы запуска

```bash
python main.py                      # Desktop GUI (по умолчанию)
python main.py --no-gui             # Headless сервер
python main.py --dashboard          # + Веб-панель :8080
python main.py --full               # GUI + Dashboard + Wake Word
python main.py --shell              # REPL-оболочка
python main.py --no-gui --dashboard # Сервер + веб-панель (продакшн)
```

---

## Первые команды

После запуска попробуй:

```
статус              → состояние системы, CPU/RAM
крипто              → курсы BTC/ETH
помощь              → полный список команд
запомни имя: Вася   → сохранить факт в памяти
что ты знаешь       → показать всю память
в 09:00 крипто      → запланировать задачу
статус сети         → P2P ноды
```

---

## Проверка здоровья системы

```bash
python health_check.py              # проверить все 88 модулей
python src/startup_validator.py     # проверить .env и зависимости
```

Ожидаемый вывод:
```
─────────────────────────────────────────────────────
  🔱 ARGOS v2.0.0 — Проверка окружения
─────────────────────────────────────────────────────
  ✅  Python 3.12
  ✅  .env файл найден и загружен
  ✅  GEMINI_API_KEY — Gemini AI
  ✅  TELEGRAM_BOT_TOKEN — Telegram-бот
  ✅  [req] fastapi
  ✅  [req] psutil
  ...
  Готов к запуску.
```

---

## REST API (v2)

Запусти с `--dashboard`, затем:

```bash
# Проверка здоровья (без токена)
curl http://localhost:8080/api/health

# Выполнить команду
curl -X POST http://localhost:8080/api/v2/command \
     -H "Authorization: Bearer $ARGOS_REMOTE_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"cmd": "статус"}'

# Последние события
curl "http://localhost:8080/api/v2/events?limit=10" \
     -H "Authorization: Bearer $ARGOS_REMOTE_TOKEN"

# Статус очереди задач
curl http://localhost:8080/api/v2/queue \
     -H "Authorization: Bearer $ARGOS_REMOTE_TOKEN"
```

---

## Устранение проблем

### Python не найден / старая версия
```bash
python --version   # нужен >= 3.10
# Ubuntu:
sudo apt install python3.12 python3.12-venv
```

### `ModuleNotFoundError: No module named 'fastapi'`
```bash
pip install -r requirements.txt
# Если в venv:
python -m pip install -r requirements.txt
```

### Gemini API возвращает 403
- Проверь, что ключ активен: [ai.google.dev](https://ai.google.dev)
- Попробуй другой провайдер: `режим ии ollama` (требует Ollama)

### Ollama не отвечает
```bash
ollama serve          # запустить в отдельном терминале
ollama pull llama3    # скачать модель
```

### Telegram бот не отвечает
```bash
# Проверить токен:
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
# Должен вернуть JSON с именем бота
```

### Docker: `permission denied` на volumes
```bash
sudo chown -R 1000:1000 ./logs ./data ./config
```

---

## Следующие шаги

- 📖 [Полная документация](docs/index.md)
- 🔌 [Подключение IoT устройств](docs/iot.md)
- 🧠 [Создание кастомных навыков](docs/skills.md)
- 🌐 [Настройка P2P сети нод](docs/p2p.md)
- 🔒 [Безопасность в production](docs/security.md)
- 📊 [Мониторинг и метрики](docs/observability.md)

---

*ARGOS v2.0.0 · Apache 2.0 · Всеволод, 2026*

# Memory

## Me
Всеволод (Seva / АvA / SiG) — разработчик, автор проекта ARGOS.

## Projects
| Name | What |
|------|------|
| **ARGOS** | Argos Universal OS v2.1.3 — самовоспроизводящаяся кроссплатформенная AI-экосистема (Desktop / Android / Docker / Telegram) |

## Teams / Owners
Всё один человек (Сева). Роли — контекст задачи, не реальные люди.
| Tag | Контекст |
|-----|---------|
| `infra` | Инфраструктура (Llama.cpp, Ollama, RAM-диск) |
| `platform` | Платформа (Redis, aiohttp, PostgreSQL, Cloudflare) |
| `sre` | Надёжность (circuit breaker, watchtower, ArgoCD) |
| `app` | Приложение (Docker-изоляция, логирование) |

## Key Terms
| Term | Meaning |
|------|---------|
| `web_learn` | Модуль поиска через DuckDuckGo |
| `Llama.cpp` | Локальный оффлайн LLM-движок |
| `Ollama` | Запускатель LLM-моделей |
| `ArgoCD` | GitOps-инструмент синхронизации конфигураций |
| `watchtower` | Авто-обновлятор Docker-контейнеров |
| `circuit breaker` | Паттерн отказоустойчивости (3 неудачи → fallback) |
| `AWA-Core` | Центральный координатор модулей ARGOS |
| `ColibriAsmEngine` | Ассемблер/дизассемблер микрокода в реальном времени |
| `npm_manager` | Навык управления npm пакетами (install, audit, outdated, npx). Интегрирован в SkillLoader, доступен через команды: `npm install`, `npm list`, `npm audit`, `npm run`, `npm info` |
| `porphyry` | Философский модуль "Триада Порфирия" — симуляция коллективного мышления через три аспекта: трезвый (аналитик), эмоциональный (ироник), интуитивный (озаритель). Режим консилиума объединяет все три. Команды: `порфирий аналитик/ироник/озаритель/консилиум`, `порфирий глубина 1-3`, `порфирий <тема>`. Честно признаётся в симуляции — без претензий на реальное метасознание |
| `orangepi_gadget` | Управление USB Gadget Orange Pi One (serial/ethernet/storage). Python-модуль `src/connectivity/orangepi_gadget.py` + shell-скрипт `deploy/usb_gadget_setup.sh`. Интегрирован в MCP API и Telegram команды `opi_gadget status/setup/stop/diagnostics` |
| `orangepi_bridge` | Аппаратный мост Orange Pi One: GPIO, I2C, UART, SPI, 1-Wire, RS-485, Modbus RTU. Python-модуль `src/connectivity/orangepi_bridge.py`. Поддерживает датчики BMP280, DS18B20, OLED, ADS1115. MCP tool `orangepi_bridge` + Telegram команды `opi status/gpio_out/gpio_in/i2c_scan/bmp280/1wire/uart_send/scan_all` |
| `ollama_vision` | Ollama Vision — анализ изображений через локальную Ollama (multimodal). Python-модуль `src/connectivity/ollama_vision_bridge.py`. Поддерживает описание изображений, OCR, анализ скриншотов. MCP tool `ollama_vision` + Telegram команды `vision status/describe/ocr/screenshot` |
| `pi_bridge` | Pi Coding Agent — интеграция внешнего агента программирования. Python-модуль `src/connectivity/pi_bridge.py`. Поддерживает выполнение задач по написанию кода, рефакторингу, оптимизации. MCP tool `pi_bridge` + Telegram команды `pi status/models/run/async` |

## AI Configuration
- **GPU Cluster**: 3 x AMD GPU (RX 580 4GB, Vega 11 2GB, RX 560 4GB)
- **AI Mode**: `auto` — использует всех доступных провайдеров
- **AI Priority**: `local-gpu,vm-cluster,azure,ollama,kimi,claude,gemini,openai,groq,deepseek,pi,yandexgpt`
- **Fallback**: Включен — при недоступности одного провайдера переключается на следующий
- **Parallel**: Включен — использует несколько провайдеров одновременно где возможно
- **Ollama**: Включена с GPU ускорением (`OLLAMA_GPU_ENABLED=true`)
- **Providers**: Kimi, Ollama, Claude, Gemini, OpenAI, Groq, DeepSeek, Pi, YandexGPT, Azure

## Preferences
- Язык задач: русский
- Приоритеты: P1 (критичные) → P2 (важные) → P3 (низкий приоритет)

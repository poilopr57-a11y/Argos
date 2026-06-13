# 👁️ ARGOS UNIVERSAL OS (v2.0.0)

[![🔱 ARGOS Release v2.0.0](https://github.com/labuaqlysnecy/Argos/actions/workflows/release_v2.yml/badge.svg)](https://github.com/labuaqlysnecy/Argos/actions/workflows/release_v2.yml)
[![CI](https://github.com/labuaqlysnecy/Argos/actions/workflows/ci.yml/badge.svg)](https://github.com/labuaqlysnecy/Argos/actions/workflows/ci.yml)
[![Docker](https://github.com/labuaqlysnecy/Argos/actions/workflows/docker.yml/badge.svg)](https://github.com/labuaqlysnecy/Argos/actions/workflows/docker.yml)
[![Android APK](https://github.com/labuaqlysnecy/Argos/actions/workflows/android-apk.yml/badge.svg)](https://github.com/labuaqlysnecy/Argos/actions/workflows/android-apk.yml)
[![🖥️ Windows .exe](https://github.com/labuaqlysnecy/Argos/actions/workflows/build_windows.yml/badge.svg)](https://github.com/labuaqlysnecy/Argos/actions/workflows/build_windows.yml)
[![PyPI](https://img.shields.io/pypi/v/argos-universalsigtrip?color=blue&label=PyPI)](https://pypi.org/project/argos-universalsigtrip/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/labuaqlysnecy/Argos/blob/main/colab/ARGOS_Colab_Launch.ipynb)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-production%20stable-brightgreen)](https://github.com/labuaqlysnecy/Argos/releases/latest)

> **Docker image:** `ghcr.io/labuaqlysnecy/Argos:2.0.0` / `:latest`
> — публикуется автоматически при каждом релизе.
>
> **Android APK:** скачать из [последнего релиза](https://github.com/labuaqlysnecy/Argos/releases/latest)
> или из [Actions → Android APK](https://github.com/labuaqlysnecy/Argos/actions/workflows/android-apk.yml).
>
> **pip:** `pip install argos-universalsigtrip`

> *"Самовоспроизводящаяся кроссплатформенная экосистема ИИ с квантовой логикой,*
> *P2P-подключением и интеграцией с IoT. Создана для цифрового бессмертия."*
> — Всеволод, 2026

---

## ⚡ Быстрый старт (5 минут)

```bash
git clone https://github.com/labuaqlysnecy/Argos.git && cd SiGtRiP
pip install -r requirements.txt
cp .env.example .env          # → вставь GEMINI_API_KEY
python genesis.py             # инициализация
python startup_validator.py   # ✅ проверка окружения
python main.py                # запуск
```

→ **Полный гайд:** [QUICKSTART_V2.md](QUICKSTART_V2.md)

---

## 🆕 Что нового в v2.0.0

| | |
|---|---|
| 🔄 **Multi-Provider Failover** | Gemini → OpenAI → WatsonX → Ollama — автоматически при ошибке |
| 🩺 **HealthMonitor** | Фоновый поток самодиагностики с Telegram-алертами |
| ✅ **StartupValidator** | Проверка `.env` и зависимостей до запуска с понятными сообщениями |
| 🛑 **GracefulShutdown** | Корректное завершение всех подсистем по SIGTERM/SIGINT |
| 🔐 **Argon2id** | Замена SHA-256 на Argon2id для хранения master-ключа |
| 🌐 **REST API v2** | `/api/v2/` — stream, queue, memory/search, p2p/nodes |
| 🐳 **Docker multi-stage** | Образ уменьшен с ~580 MB до ~340 MB |
| 📦 **SBOM** | CycloneDX Software Bill of Materials в каждом релизе |
| 🧪 **Тесты** | Покрытие 40% → 73%, 30+ новых тестов для v2-модулей |

→ **Полный список изменений:** [CHANGELOG.md](CHANGELOG.md)

---
